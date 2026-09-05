"""Payment lifecycle: order -> payment link -> webhook confirmation -> settlement (#56).

Simulated end-to-end if Razorpay keys are absent, so the full loop demos with
zero setup. If RAZORPAY_KEY_ID/SECRET are present, real test-mode orders are
created via the SDK (link + webhook still delivered by Razorpay in that path).

Deterministic state machine — the LLM is nowhere near this code.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Callable, Optional

# order lifecycle states
CREATED = "created"
LINK_ISSUED = "link_issued"
PAID = "paid"
SETTLED = "settled"
FAILED = "failed"


class PaymentEngine:
    def __init__(self):
        self._orders: dict[str, dict] = {}
        self._use_real = bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET"))
        self._client = None
        if self._use_real:
            try:
                import razorpay  # type: ignore

                self._client = razorpay.Client(
                    auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"])
                )
            except Exception:
                self._use_real = False

    @property
    def mode(self) -> str:
        return "razorpay_test" if self._use_real else "simulated"

    def create_order(self, session_id: str, amount: float, currency: str, items: list[dict]) -> dict:
        order_id = "order_" + uuid.uuid4().hex[:14]
        record = {
            "order_id": order_id,
            "session_id": session_id,
            "amount": amount,
            "currency": currency,
            "items": items,
            "status": CREATED,
            "created_at": time.time(),
            "mode": self.mode,
            "events": [{"state": CREATED, "ts": time.time()}],
        }
        if self._use_real and self._client:
            try:
                rp = self._client.order.create({
                    "amount": int(amount * 100),  # paise
                    "currency": currency,
                    "notes": {"session_id": session_id},
                })
                record["provider_order_id"] = rp.get("id")
            except Exception as e:
                record["provider_error"] = str(e)
        self._orders[order_id] = record
        return record

    def issue_link(self, order_id: str) -> dict:
        rec = self._orders[order_id]
        base = os.getenv("AUXION_PUBLIC_URL", "http://localhost:8000")
        rec["payment_link"] = f"{base}/pay/{order_id}"
        rec["status"] = LINK_ISSUED
        rec["events"].append({"state": LINK_ISSUED, "ts": time.time()})
        return rec

    def simulate_payment(self, order_id: str, on_settled: Optional[Callable[[dict], None]] = None, delay: float = 1.5) -> None:
        """Kick off async 'customer paid' -> webhook -> settlement (simulated path)."""
        def _run():
            time.sleep(delay)
            rec = self._orders.get(order_id)
            if not rec or rec["status"] in (SETTLED, FAILED):
                return
            rec["status"] = PAID
            rec["payment_ref"] = "pay_" + uuid.uuid4().hex[:14]
            rec["events"].append({"state": PAID, "ts": time.time()})
            time.sleep(0.6)
            rec["status"] = SETTLED
            rec["settled_at"] = time.time()
            rec["events"].append({"state": SETTLED, "ts": time.time()})
            if on_settled:
                on_settled(rec)

        threading.Thread(target=_run, daemon=True).start()

    def mark_paid_from_webhook(self, order_id: str, payment_ref: str, on_settled: Optional[Callable[[dict], None]] = None) -> dict:
        """Real webhook path: Razorpay calls us; we advance the state machine."""
        rec = self._orders[order_id]
        rec["status"] = PAID
        rec["payment_ref"] = payment_ref
        rec["events"].append({"state": PAID, "ts": time.time()})
        rec["status"] = SETTLED
        rec["settled_at"] = time.time()
        rec["events"].append({"state": SETTLED, "ts": time.time()})
        if on_settled:
            on_settled(rec)
        return rec

    def get(self, order_id: str) -> Optional[dict]:
        return self._orders.get(order_id)


payment_engine = PaymentEngine()
