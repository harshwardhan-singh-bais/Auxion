"""Signed, verifiable receipts (#12).

HMAC-SHA256 over a canonical JSON body using the configured signing key. Anyone
with the key can verify a receipt was issued by this merchant and not altered.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

from .config import settings


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _sign(body: dict) -> str:
    return hmac.new(settings.signing_key.encode(), _canonical(body), hashlib.sha256).hexdigest()


def issue(order: dict) -> dict:
    body = {
        "receipt_id": "rcpt_" + uuid.uuid4().hex[:16],
        "issued_at": time.time(),
        "merchant_id": order.get("merchant_id") or order.get("merchant"),
        "session_id": order.get("session_id"),
        "order_id": order.get("order_id"),
        "items": order.get("items", []),
        "amount": order.get("amount"),
        "currency": order.get("currency", "INR"),
        "payment_ref": order.get("payment_ref"),
        "status": order.get("status", "settled"),
    }
    return {"body": body, "signature": _sign(body), "alg": "HMAC-SHA256"}


def verify(receipt: dict) -> dict:
    body = receipt.get("body", {})
    sig = receipt.get("signature", "")
    expected = _sign(body)
    ok = hmac.compare_digest(expected, sig)
    return {"valid": ok, "receipt_id": body.get("receipt_id"), "amount": body.get("amount"),
            "order_id": body.get("order_id")}
