"""Seeded, reproducible demo data (#72).

Resets metrics + tables and plays a deterministic scenario so the live demo
always starts from a known, populated state (some settled revenue, a recovered
cart, an upsell). Safe to call repeatedly.
"""
from __future__ import annotations

import time

from . import db


def seed_demo() -> None:
    from .orchestrator import orchestrator
    from .catalog import catalog

    db.reset_all()
    orchestrator.revenue = 0.0
    orchestrator.orders_settled = 0
    orchestrator.recovered = 0.0
    orchestrator.upsell_revenue = 0.0
    orchestrator._sessions.clear()
    orchestrator._persist_metrics()

    # Pre-populate a couple of settled orders directly in the DB so charts aren't empty.
    prods = catalog.products
    now = time.time()
    demo_orders = [
        {"order_id": "order_seed0001", "session_id": "seed_a", "merchant": catalog.active,
         "amount": prods[0]["price"], "currency": catalog.merchant.get("currency", "INR"),
         "status": "settled", "items": [{"product_id": prods[0]["id"], "qty": 1, "price": prods[0]["price"]}],
         "payment_ref": "pay_seed0001", "created_at": now - 300},
        {"order_id": "order_seed0002", "session_id": "seed_b", "merchant": catalog.active,
         "amount": prods[1]["price"] + prods[3]["price"], "currency": catalog.merchant.get("currency", "INR"),
         "status": "settled",
         "items": [{"product_id": prods[1]["id"], "qty": 1, "price": prods[1]["price"]},
                   {"product_id": prods[3]["id"], "qty": 1, "price": prods[3]["price"]}],
         "payment_ref": "pay_seed0002", "created_at": now - 120},
    ]
    total = 0.0
    for o in demo_orders:
        db.save_order(o)
        total += o["amount"]

    orchestrator.revenue = total
    orchestrator.orders_settled = len(demo_orders)
    orchestrator.upsell_revenue = prods[3]["price"]  # the cap attributed as an upsell
    orchestrator._persist_metrics()
