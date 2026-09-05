"""Central configuration and paths. Single source of truth for env + locations."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"

DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")


class Settings:
    # LLM
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

    # Razorpay
    razorpay_key_id: str = os.getenv("RAZORPAY_KEY_ID", "")
    razorpay_key_secret: str = os.getenv("RAZORPAY_KEY_SECRET", "")
    razorpay_webhook_secret: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    # Signing (receipts + JWT). Stable if provided, else random per-run.
    signing_key: str = os.getenv("AUXION_SIGNING_KEY", "") or uuid.uuid4().hex
    jwt_key: str = os.getenv("AUXION_JWT_KEY", "") or os.getenv("AUXION_SIGNING_KEY", "") or uuid.uuid4().hex

    public_url: str = os.getenv("AUXION_PUBLIC_URL", "http://localhost:8000")

    db_path: Path = DATA_DIR / "auxion.db"

    active_merchant: str = os.getenv("AUXION_MERCHANT", "auxion-demo-store")

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_razorpay(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)


settings = Settings()
