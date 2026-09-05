"""Payment lifecycle state machine: order -> link -> webhook -> settlement (#56),
plus refunds and graceful failure handling (the-bar requirement).

Simulated end-to-end with zero setup; real Razorpay test-mode when keys present.
A failure-injection flag lets the demo show one failure handled gracefully
(payment fails -> order marked failed -> compensating action, no money lost).

Deterministic. The LLM is nowhere near this code.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Optional

from . import db
from .config import settings

CREATED = "created"
LINK_ISSUED = "link_issued"
PAID = "paid"
SETTLED = "settled"
FAILED = "failed"
REFUNDED = "refunded"


class PaymentEngine:
    def __init__(self):
        self._orders: dict[str, dict] = {}
        self._use_real = settings.has_razorpay
        self._client = None
        self.fail_next = False          # inject one failure for the graceful-failure demo
        if self._use_real:
            try:
                import razorpay  # type: ignore
                self._client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
            except Exception:
                self._use_real = False

    @property
    def mode(self) -> str:
        return "razorpay_test" if self._use_real else "simulated"

    def create_order(self, session_id: str, amount: float, currency: str, items: list[dict], merchant: str) -> dict:
        order_id = "order_" + uuid.uuid4().hex[:14]
        record = {
            "order_id": order_id, "session_id": session_id, "merchant": merchant,
            "amount": amount, "currency": currency, "items": items,
            "status": CREATED, "created_at": time.time(), "mode": self.mode,
            "events": [{"state": CREATED, "ts": time.time()}],
        }
        if self._use_real and self._client:
            try:
                rp = self._client.order.create({
                    "amount": int(amount * 100), "currency": currency,
                    "notes": {"session_id": session_id, "order_id": order_id},
                })
                record["provider_order_id"] = rp.get("id")
            except Exception as e:
                record["provider_error"] = str(e)
        self._orders[order_id] = record
        db.save_order(record)
        return record

    def issue_link(self, order_id: str) -> dict:
        rec = self._orders[order_id]
        rec["payment_link"] = f"{settings.public_url}/pay/{order_id}"
        rec["status"] = LINK_ISSUED
        rec["events"].append({"state": LINK_ISSUED, "ts": time.time()})
        db.save_order(rec)
        return rec

    def simulate_payment(self, order_id: str,
                         on_settled: Optional[Callable[[dict], None]] = None,
                         on_failed: Optional[Callable[[dict], None]] = None,
                         delay: float = 1.4) -> None:
        """Async 'customer paid' -> webhook -> settlement (simulated)."""
        def _run():
            time.sleep(delay)
            rec = self._orders.get(order_id)
            if not rec or rec["status"] in (SETTLED, FAILED, REFUNDED):
                return
            # graceful failure path
            if self.fail_next:
                self.fail_next = False
                rec["status"] = FAILED
                rec["failure_reason"] = "gateway declined (injected)"
                rec["events"].append({"state": FAILED, "ts": time.time()})
                db.save_order(rec)
                if on_failed:
                    on_failed(rec)
                return
            rec["status"] = PAID
            rec["payment_ref"] = "pay_" + uuid.uuid4().hex[:14]
            rec["events"].append({"state": PAID, "ts": time.time()})
            db.save_order(rec)
            time.sleep(0.5)
            rec["status"] = SETTLED
            rec["settled_at"] = time.time()
            rec["events"].append({"state": SETTLED, "ts": time.time()})
            db.save_order(rec)
            if on_settled:
                on_settled(rec)
        threading.Thread(target=_run, daemon=True).start()

    def mark_paid_from_webhook(self, order_id: str, payment_ref: str,
                               on_settled: Optional[Callable[[dict], None]] = None) -> dict:
        rec = self._orders[order_id]
        rec["status"] = PAID
        rec["payment_ref"] = payment_ref
        rec["events"].append({"state": PAID, "ts": time.time()})
        rec["status"] = SETTLED
        rec["settled_at"] = time.time()
        rec["events"].append({"state": SETTLED, "ts": time.time()})
        db.save_order(rec)
        if on_settled:
            on_settled(rec)
        return rec

    def refund(self, order_id: str) -> dict:
        rec = self._orders.get(order_id) or db.get_order(order_id)
        if not rec:
            return {"error": "unknown order"}
        rec["status"] = REFUNDED
        rec["refund_ref"] = "rfnd_" + uuid.uuid4().hex[:12]
        rec.setdefault("events", []).append({"state": REFUNDED, "ts": time.time()})
        if self._use_real and self._client and rec.get("payment_ref"):
            try:
                self._client.payment.refund(rec["payment_ref"], {"amount": int(rec["amount"] * 100)})
            except Exception as e:
                rec["refund_error"] = str(e)
        db.save_order(rec)
        return rec

    def get(self, order_id: str) -> Optional[dict]:
        return self._orders.get(order_id) or db.get_order(order_id)


payment_engine = PaymentEngine()
