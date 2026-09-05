"""In-process smoke test — no server, no network. Verifies the full spine.

Run:  ./.venv/Scripts/python.exe smoke_test.py

Exercises: search -> add -> policy allow -> execute -> settle -> receipt verify,
policy DENY path, audit-chain integrity + tamper detection, adversarial self-test,
counterfactual, JWT scope, campaign attribution, and graceful payment failure.
"""
from __future__ import annotations

import os
import time

# Keep the core e2e checks deterministic and reproducible; the LLM fallback
# chain gets its own live check (13/14) below.
os.environ["AUXION_FORCE_DETERMINISTIC"] = "1"

from app import db
from app.audit import audit
from app.auth import issue_token, verify_token
from app.config import settings
from app.orchestrator import orchestrator
from app.payments import payment_engine
from app.policy import policy_engine
from app.selftest import run_self_test
from app import receipts
from app.llm import llm_chain
from app.planner import plan as plan_direct, _PLANNER_TOOL, _planner_system


def wait(cond, timeout=6.0):
    start = time.time()
    while time.time() - start < timeout:
        if cond():
            return True
        time.sleep(0.1)
    return False


def main():
    db.init_db()
    start_orders = orchestrator.orders_settled

    # 1) search
    r = orchestrator.handle_message("find a blue t-shirt under 800")
    sid = r["session_id"]
    assert r.get("products"), "search returned no products"
    print(f"[1] search ok -> {[p['id'] for p in r['products']]}")

    # 2) add
    r = orchestrator.handle_message("buy a blue t-shirt", session_id=sid)
    assert r.get("cart"), "add failed"
    print(f"[2] add ok -> ₹{r['amount']:.0f}, upsell {r.get('upsell')}, bundle={bool(r.get('bundle'))}")

    # 3) checkout -> allow -> execute
    r = orchestrator.handle_message("checkout", session_id=sid)
    assert r.get("order"), f"checkout did not create order: {r}"
    oid = r["order"]["order_id"]
    print(f"[3] checkout ok -> {oid} status {r['order']['status']}")

    # 4) settlement (async)
    assert wait(lambda: orchestrator.orders_settled > start_orders), "order did not settle"
    print(f"[4] settled ok -> revenue ₹{orchestrator.revenue:.0f}, orders {orchestrator.orders_settled}")

    # 5) receipt persisted + verify + tamper
    order = db.get_order(oid)
    assert order and order.get("receipt"), "receipt not persisted"
    v = receipts.verify(order["receipt"])
    assert v["valid"], "persisted receipt failed verification"
    tampered = {"body": dict(order["receipt"]["body"]), "signature": order["receipt"]["signature"]}
    tampered["body"]["amount"] = 1
    assert not receipts.verify(tampered)["valid"], "tampered receipt passed"
    print("[5] receipt persist + verify + tamper-detect ok")

    # 6) policy DENY path — over unknown-tier ceiling
    sid2 = orchestrator.handle_message("hi")["session_id"]
    orchestrator.handle_message("buy 3 slim fit jeans", session_id=sid2)
    r = orchestrator.handle_message("checkout", session_id=sid2)
    assert r.get("decision", {}).get("decision") == "deny", f"expected deny, got {r.get('decision')}"
    print(f"[6] policy deny ok -> {r['decision']['violations'][:1]}")

    # 7) JWT scope: mint trusted token, lifts tier
    tok = issue_token(tier="trusted", max_amount=15000)
    claims = verify_token(tok)
    assert claims and claims["tier"] == "trusted", "token verify failed"
    sid3 = orchestrator.handle_message("hi", token=tok)["session_id"]
    assert orchestrator._sessions[sid3].tier == "trusted", "token did not lift tier"
    print("[7] JWT scope ok -> session upgraded to trusted")

    # 8) audit chain integrity + tamper
    v = audit.verify()
    assert v["valid"], f"audit chain invalid: {v}"
    saved = audit._entries[0]["data"]
    audit._entries[0]["data"] = {"tampered": True}
    broken = audit.verify()
    audit._entries[0]["data"] = saved
    assert not broken["valid"], "tamper not detected"
    print(f"[8] audit chain valid ({v['count']}) + tamper-detect ok")

    # 9) adversarial self-test
    st = run_self_test()
    assert st["all_passed"], f"self-test failures: {[c for c in st['cases'] if not c['pass']]}"
    print(f"[9] adversarial self-test ok -> {st['passed']}/{st['total']} gates held")

    # 10) counterfactual
    cf = policy_engine.counterfactual({"items": [{"product_id": "jeans-slim", "qty": 3, "price": 1899}], "amount": 5697},
                                      "unknown", "tier_ceiling", "cf1")
    assert cf["changed"], "counterfactual should change decision when removing tier_ceiling"
    print(f"[10] counterfactual ok -> with={cf['with_gate']} without={cf['without_gate']}")

    # 11) graceful payment failure
    before_fail = orchestrator.orders_settled
    payment_engine.fail_next = True
    sid4 = orchestrator.handle_message("hi")["session_id"]
    orchestrator.handle_message("buy a navy baseball cap", session_id=sid4)
    r = orchestrator.handle_message("checkout", session_id=sid4)
    foid = r["order"]["order_id"]
    assert wait(lambda: (db.get_order(foid) or {}).get("status") == "failed"), "failure path did not mark failed"
    assert orchestrator.orders_settled == before_fail, "failed order wrongly counted as settled"
    print("[11] graceful payment failure ok -> order failed, no revenue booked")

    # 12) one-click bundle accept: add 1 bundle item, accept, discount applied
    sid5 = orchestrator.handle_message("buy a blue t-shirt")["session_id"]
    r = orchestrator.accept_bundle(session_id=sid5)
    assert r.get("cart") and len(r["cart"]) == 3, f"bundle accept did not complete cart: {r}"
    assert r.get("bundle_discount", 0) > 0, "bundle discount not applied"
    btitle = (r.get("bundle") or {}).get("title", "")
    print(f"[12] bundle accept ok -> 3 items, saving {chr(8377)}{r['bundle_discount']:.0f} ('{btitle}')")

    # 13) LLM fallback chain (LIVE when providers configured)
    settings.force_deterministic = False
    try:
        plan = plan_direct("find a blue t-shirt under 800")
        assert plan.get("intent") in ("search", "add", "help", "recommend", "checkout", "remove", "accept_bundle"), \
            f"live LLM plan had bad intent: {plan.get('intent')}"
        note = f" (llm note: {plan['_llm_error'][:80]})" if plan.get("_llm_error") else ""
        print(f"[13] LLM fallback chain LIVE ok -> intent={plan['intent']} via {plan.get('_source')}{note}")
    finally:
        settings.force_deterministic = True

    # 14) LLM failover error surfacing: every layer fails -> clean report
    saved_keys = dict(settings.llm_keys)
    saved_ollama = settings.llm_bases["ollama"]
    settings.llm_keys = {k: "sk-invalid-key-for-test" for k in settings.llm_keys}
    settings.llm_bases["ollama"] = "http://127.0.0.1:9"
    settings.force_deterministic = False
    try:
        bad_plan, report = llm_chain.propose_action(_planner_system(), "hi", _PLANNER_TOOL, "propose_action")
        assert bad_plan is None, "expected no plan when all providers fail"
        assert report["error"] and "deterministic" in report["error"], f"bad failover report: {report}"
        print(f"[14] LLM failover errors ok -> {len(report['attempts'])} attempts, surfaced: {report['error'][:90]}...")
    finally:
        settings.llm_keys = saved_keys
        settings.llm_bases["ollama"] = saved_ollama
        settings.force_deterministic = True

    print("\nALL SMOKE TESTS PASSED ✅")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\n❌ SMOKE TEST FAILED: {e}")
        raise
