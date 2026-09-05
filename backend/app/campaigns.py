"""Campaign orchestrator (Tier 9): cart recovery + upsell/cross-sell revenue engine.

Watches sessions. If a cart sits idle past a threshold without checkout, it fires
a 'cart_recovery' campaign (a nudge message + optional incentive). On add-to-cart
it computes cross-sell suggestions. Recovered/upsell revenue is attributed so the
revenue dashboard can show the agent *growing* merchant revenue (track goal).

Deterministic scheduling; persisted to DB so campaigns survive restarts.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Optional

from . import db
from .catalog import catalog

PENDING = "pending"
FIRED = "fired"
CONVERTED = "converted"
CANCELLED = "cancelled"


class CampaignEngine:
    def __init__(self, recovery_delay: float = 8.0):
        self.recovery_delay = recovery_delay      # seconds idle before nudge (short for demo)
        self._campaigns: dict[str, dict] = {}
        self._emit: Optional[Callable[[str, dict, Optional[str]], None]] = None
        self._timers: dict[str, threading.Timer] = {}

    def bind(self, emit: Callable[[str, dict, Optional[str]], None]) -> None:
        self._emit = emit

    def _publish(self, etype: str, payload: dict, session_id: Optional[str]) -> None:
        if self._emit:
            self._emit(etype, payload, session_id)

    # ---- cross-sell / upsell ----
    def cross_sell(self, product_id: str) -> list[dict]:
        return catalog.cross_sell(product_id)

    def bundle_offer(self, cart: list[dict]) -> Optional[dict]:
        """If cart items are a subset of a bundle, offer the discounted bundle."""
        cart_ids = {c["product_id"] for c in cart}
        for b in catalog.bundles:
            bid = set(b["product_ids"])
            if cart_ids and cart_ids.issubset(bid) and cart_ids != bid:
                missing = [catalog.get(i) for i in bid - cart_ids]
                full_price = sum(catalog.get(i)["price"] for i in bid)
                discounted = round(full_price * (1 - b["discount_pct"] / 100))
                return {"bundle_id": b["id"], "title": b["title"], "add": [m["id"] for m in missing],
                        "full_price": full_price, "bundle_price": discounted,
                        "save": full_price - discounted, "discount_pct": b["discount_pct"]}
        return None

    # ---- cart recovery ----
    def schedule_recovery(self, session_id: str, cart: list[dict]) -> None:
        self.cancel_recovery(session_id)
        if not cart:
            return
        cid = "camp_" + uuid.uuid4().hex[:10]
        amount = sum(c["qty"] * c["price"] for c in cart)
        camp = {"id": cid, "session_id": session_id, "kind": "cart_recovery",
                "status": PENDING, "payload": {"cart": cart, "amount": amount},
                "created_at": time.time(), "fired_at": None}
        self._campaigns[cid] = camp
        db.save_campaign(camp)

        def _fire():
            c = self._campaigns.get(cid)
            if not c or c["status"] != PENDING:
                return
            c["status"] = FIRED
            c["fired_at"] = time.time()
            db.save_campaign(c)
            incentive = 5 if amount < 1000 else 10
            msg = (f"👋 You left {len(cart)} item(s) worth ₹{amount:.0f} in your cart. "
                   f"Here's {incentive}% off if you complete checkout now.")
            self._publish("campaign_fired",
                          {"campaign_id": cid, "kind": "cart_recovery", "message": msg,
                           "incentive_pct": incentive, "amount": amount}, session_id)

        t = threading.Timer(self.recovery_delay, _fire)
        t.daemon = True
        t.start()
        self._timers[session_id] = t

    def cancel_recovery(self, session_id: str, converted: bool = False) -> None:
        t = self._timers.pop(session_id, None)
        if t:
            t.cancel()
        for c in self._campaigns.values():
            if c["session_id"] == session_id and c["status"] in (PENDING, FIRED):
                c["status"] = CONVERTED if converted else CANCELLED
                db.save_campaign(c)

    def mark_converted(self, session_id: str, recovered_amount: float) -> float:
        """Called when a session checks out after a recovery fired. Returns attributed amount."""
        attributed = 0.0
        for c in self._campaigns.values():
            if c["session_id"] == session_id and c["status"] == FIRED and c["kind"] == "cart_recovery":
                c["status"] = CONVERTED
                db.save_campaign(c)
                attributed += recovered_amount
        self.cancel_recovery(session_id, converted=True)
        return attributed

    def stats(self) -> dict:
        camps = list(self._campaigns.values())
        return {
            "total": len(camps),
            "fired": sum(1 for c in camps if c["status"] in (FIRED, CONVERTED)),
            "converted": sum(1 for c in camps if c["status"] == CONVERTED),
        }


campaign_engine = CampaignEngine()
