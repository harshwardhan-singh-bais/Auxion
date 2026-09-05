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
    # --- LLM fallback chain (provider API keys; models/base URLs overridable) ---
    llm_keys: dict[str, str] = {
        "GROQ_API_KEY": os.getenv("GROQ_API_KEY", "").strip(),
        "SAMBANOVA_API_KEY": os.getenv("SAMBANOVA_API_KEY", "").strip(),
        "MISTRAL_API_KEY": os.getenv("MISTRAL_API_KEY", "").strip(),
        "CEREBRAS_API_KEY": os.getenv("CEREBRAS_API_KEY", "").strip(),
        "NVIDIA_API_KEY": os.getenv("NVIDIA_API_KEY", "").strip(),
        "NEMOTRON_API_KEY": os.getenv("NEMOTRON_API_KEY", "").strip(),
        "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY", "").strip(),
    }
    llm_models: dict[str, str] = {
        "GROQ_MODEL": os.getenv("GROQ_MODEL", ""),
        "SAMBANOVA_MODEL": os.getenv("SAMBANOVA_MODEL", ""),
        "MISTRAL_MODEL": os.getenv("MISTRAL_MODEL", ""),
        "CEREBRAS_MODEL": os.getenv("CEREBRAS_MODEL", ""),
        "NVIDIA_MODEL": os.getenv("NVIDIA_MODEL", ""),
        "NEMOTRON_MODEL": os.getenv("NEMOTRON_MODEL", ""),
        "OPENROUTER_MODEL": os.getenv("OPENROUTER_MODEL", ""),
        "OLLAMA_MODEL": os.getenv("OLLAMA_MODEL", ""),
    }
    llm_bases: dict[str, str] = {
        "ollama": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip(),
    }
    ollama_enabled: bool = os.getenv("OLLAMA_ENABLED", "1") not in ("0", "false", "False")
    # AUXION_FORCE_DETERMINISTIC=1 -> never call an LLM (used by the smoke test
    # for reproducibility; the LLM path gets its own live check).
    force_deterministic: bool = os.getenv("AUXION_FORCE_DETERMINISTIC", "") == "1"

    # Razorpay
    razorpay_key_id: str = os.getenv("RAZORPAY_KEY_ID", "").strip()
    razorpay_key_secret: str = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    razorpay_webhook_secret: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()

    # Signing (receipts + JWT). Stable if provided, else random per-run.
    signing_key: str = os.getenv("AUXION_SIGNING_KEY", "") or uuid.uuid4().hex
    jwt_key: str = os.getenv("AUXION_JWT_KEY", "") or os.getenv("AUXION_SIGNING_KEY", "") or uuid.uuid4().hex

    public_url: str = os.getenv("AUXION_PUBLIC_URL", "http://localhost:8000")

    # Optional admin auth. If set, /api/* (except token+status) require X-Auxion-Token.
    admin_token: str = os.getenv("AUXION_ADMIN_TOKEN", "").strip()

    db_path: Path = DATA_DIR / "auxion.db"

    active_merchant: str = os.getenv("AUXION_MERCHANT", "auxion-demo-store")

    @property
    def has_llm(self) -> bool:
        if self.force_deterministic:
            return False
        return any(self.llm_keys.values()) or (self.ollama_enabled and self.llm_bases["ollama"])

    @property
    def has_razorpay(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)


settings = Settings()
