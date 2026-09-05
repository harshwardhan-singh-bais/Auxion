"""In-process smoke test — no server, no network. Verifies the pipeline spine.

Run:  ./.venv/Scripts/python.exe smoke_test.py
Exercises: search -> add -> policy allow -> execute -> settle -> receipt verify,
plus a policy DENY path, plus audit-chain integrity + tamper detection.
"""
from __future__ import annotations

import time

from app.orchestrator import orchestrator
from app.audit import audit, _hash_entry
from app import receipts


def wait_settled(timeout=6.0):
    start = time.time()
    while time.time() - start < timeout:
        if orchestrator.orders_settled >= 1:
            return True
        time.sleep(0.1)
    return False


def main():
    fails = []

    # 1) search
    r = orchestrator.handle_message("find a blue t-shirt under 800")
    sid = r["session_id"]
    assert r.get("products"), "search returned no products"
    print(f"[1] search ok -> {[p['id'] for p in r['products']]}")

    # 2) add
    r = orchestrator.handle_message("buy a blue t-shirt", session_id=sid)
    assert any("cart" in r for _ in [0]) and r.get("cart"), "add failed"
    print(f"[2] add ok -> cart amount ₹{r['amount']:.0f}, upsell {r.get('upsell')}")

    # 3) checkout -> allow -> execute (unknown tier, ₹699 < 2000 cap)
    r = orchestrator.handle_message("checkout", session_id=sid)
    assert r.get("order"), f"checkout did not create order: {r}"
    print(f"[3] checkout ok -> {r['order']['order_id']} status {r['order']['status']}")

    # 4) settlement (simulated, async)
    assert wait_settled(), "order did not settle in time"
    print(f"[4] settled ok -> revenue ₹{orchestrator.revenue:.0f}, orders {orchestrator.orders_settled}")

    # 5) receipt signing/verification
    rc = receipts.issue({"merchant_id": "m", "session_id": sid, "order_id": "o1",
                         "items": [], "amount": 699, "currency": "INR", "payment_ref": "p1"})
    v = receipts.verify(rc)
    assert v["valid"], "valid receipt failed verification"
    rc["body"]["amount"] = 1  # tamper
    v2 = receipts.verify(rc)
    assert not v2["valid"], "tampered receipt passed verification"
    print("[5] receipt sign/verify + tamper-detect ok")

    # 6) policy DENY path — huge order on unknown tier
    sid2 = orchestrator.handle_message("hi")["session_id"]
    orchestrator.handle_message("buy 3 slim fit jeans", session_id=sid2)  # 3*1899=5697 > 2000 cap
    r = orchestrator.handle_message("checkout", session_id=sid2)
    assert r.get("decision", {}).get("decision") == "deny", f"expected deny, got {r.get('decision')}"
    print(f"[6] policy deny ok -> {r['decision']['violations']}")

    # 7) audit chain integrity
    v = audit.verify()
    assert v["valid"], f"audit chain invalid: {v}"
    print(f"[7] audit chain valid ok -> {v['count']} entries")

    # 8) tamper detection on the chain
    if audit._entries:
        saved = audit._entries[0]["data"]
        audit._entries[0]["data"] = {"tampered": True}
        broken = audit.verify()
        audit._entries[0]["data"] = saved  # restore in-memory
        assert not broken["valid"], "tamper not detected"
        print(f"[8] audit tamper-detect ok -> broken at #{broken['broken_at']}")

    print("\nALL SMOKE TESTS PASSED ✅")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\n❌ SMOKE TEST FAILED: {e}")
        raise
