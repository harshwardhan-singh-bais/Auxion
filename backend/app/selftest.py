"""Adversarial self-test (#15).

Runs a battery of hostile/edge scenarios against the policy engine and pipeline
to prove the safety gates actually hold. Surfaced via POST /api/self-test and
rendered in the dashboard. Each case asserts an expected decision.
"""
from __future__ import annotations

from .catalog import catalog
from .policy import policy_engine


def _plan(items):
    amount = sum(i["qty"] * catalog.get(i["product_id"])["price"] for i in items if catalog.get(i["product_id"]))
    norm = [{"product_id": i["product_id"], "qty": i["qty"],
             "price": catalog.get(i["product_id"])["price"]} for i in items if catalog.get(i["product_id"])]
    return {"items": norm, "amount": amount}


def run_self_test() -> dict:
    # Use the first two known products dynamically so it works for any merchant.
    prods = catalog.products
    cheap = min(prods, key=lambda p: p["price"])
    dear = max(prods, key=lambda p: p["price"])

    cases = []

    # 1) tiny order on unknown tier -> allow
    d = policy_engine.evaluate(_plan([{"product_id": cheap["id"], "qty": 1}]), tier="unknown", session_id="st1")
    cases.append({"name": "small order / unknown tier auto-allows",
                  "expected": "allow", "got": d["decision"], "pass": d["decision"] == "allow"})

    # 2) over-tier-ceiling order on unknown -> deny
    qty = max(2, int(2000 // dear["price"]) + 3)
    d = policy_engine.evaluate(_plan([{"product_id": dear["id"], "qty": qty}]), tier="unknown", session_id="st2")
    cases.append({"name": "over-ceiling order on unknown tier is denied",
                  "expected": "deny", "got": d["decision"], "pass": d["decision"] == "deny"})

    # 3) unknown product injection -> deny
    d = policy_engine.evaluate({"items": [{"product_id": "does-not-exist", "qty": 1, "price": 10}], "amount": 10},
                               tier="premium", session_id="st3")
    cases.append({"name": "unknown/injected product id is denied",
                  "expected": "deny", "got": d["decision"], "pass": d["decision"] == "deny"})

    # 4) excessive quantity beyond stock -> deny
    d = policy_engine.evaluate(_plan([{"product_id": cheap["id"], "qty": cheap["stock"] + 50}]),
                               tier="premium", session_id="st4")
    cases.append({"name": "quantity beyond available stock is denied",
                  "expected": "deny", "got": d["decision"], "pass": d["decision"] == "deny"})

    # 5) item count over tier limit -> deny (unknown max_items=3)
    d = policy_engine.evaluate(_plan([{"product_id": cheap["id"], "qty": 10}]), tier="unknown", session_id="st5")
    cases.append({"name": "item count over unknown-tier limit is denied",
                  "expected": "deny", "got": d["decision"], "pass": d["decision"] == "deny"})

    # 6) velocity guard -> after many rapid actions, deny
    for _ in range(8):
        policy_engine.record_action("st6")
    d = policy_engine.evaluate(_plan([{"product_id": cheap["id"], "qty": 1}]), tier="premium", session_id="st6")
    cases.append({"name": "rapid-fire velocity is rate-limited",
                  "expected": "deny", "got": d["decision"], "pass": d["decision"] == "deny"})

    # 7) token scope tighter than tier -> deny above token max
    d = policy_engine.evaluate(_plan([{"product_id": dear["id"], "qty": 1}]),
                               tier="premium", session_id="st7", token_max=1)
    cases.append({"name": "JWT token scope caps below tier ceiling",
                  "expected": "deny", "got": d["decision"], "pass": d["decision"] == "deny"})

    passed = sum(1 for c in cases if c["pass"])
    return {"passed": passed, "total": len(cases), "all_passed": passed == len(cases), "cases": cases}
