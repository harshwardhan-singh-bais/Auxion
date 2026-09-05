"""JWT-style scoped session tokens (Tier auth/scoping).

Self-contained HS256 implementation (no external dep) so it runs anywhere.
A token scopes: tier, max amount ceiling, allowed actions, and expiry. The
orchestrator honours the token's tier/scope; absent/invalid token => 'unknown'.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from .config import settings


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(tier: str = "trusted", max_amount: float = 15000,
                actions: Optional[list[str]] = None, ttl: int = 3600,
                subject: str = "agent") -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": subject,
        "tier": tier,
        "max_amount": max_amount,
        "actions": actions or ["search", "add", "checkout"],
        "iat": now,
        "exp": now + ttl,
    }
    seg = _b64(json.dumps(header, separators=(",", ":")).encode()) + "." + \
          _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(settings.jwt_key.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64(sig)


def verify_token(token: str) -> Optional[dict]:
    try:
        seg_h, seg_p, seg_s = token.split(".")
        seg = seg_h + "." + seg_p
        expected = hmac.new(settings.jwt_key.encode(), seg.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(seg_s)):
            return None
        payload = json.loads(_b64d(seg_p))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None
