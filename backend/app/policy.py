"""Hot-swappable policy engine (#13) with trust tiers (#65) and risk scoring.

The Critic step of the pipeline. Pure Python rules over YAML config. Reloads the
YAML on every evaluation so edits take effect live during a demo. Deterministic:
the LLM never decides whether money moves — this does.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .catalog import catalog

POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "policy.yaml"


class PolicyEngine:
    def __init__(self, path: Path = POLICY_PATH):
        self.path = path
        self._cfg: dict = {}
        self._mtime: float = 0.0
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        mtime = self.path.stat().st_mtime
        if force or mtime != self._mtime:
            with open(self.path, "r", encoding="utf-8") as f:
                self._cfg = yaml.safe_load(f) or {}
            self._mtime = mtime

    @property
    def config(self) -> dict:
        self.reload()
        return self._cfg

    def tier_config(self, tier: str) -> dict:
        return self.config.get("tiers", {}).get(tier, self.config.get("tiers", {}).get("unknown", {}))

    def score_risk(self, items: list[dict], amount: float, tier: str) -> dict:
        """Return risk score in 0..1 with a human-readable breakdown."""
        cfg = self.config
        weights = cfg.get("risk_weights", {})
        tier_cfg = self.tier_config(tier)
        ceiling = float(tier_cfg.get("max_transaction_amount", 1) or 1)

        factors: list[dict] = []
        score = 0.0

        ratio = amount / ceiling if ceiling else 1.0
        if ratio > 0.6:
            contrib = weights.get("high_value", 0) * min(ratio, 1.0)
            score += contrib
            factors.append({"factor": "high_value", "detail": f"{ratio:.0%} of tier ceiling", "contribution": round(contrib, 3)})

        if tier == "unknown":
            contrib = weights.get("new_agent", 0)
            score += contrib
            factors.append({"factor": "new_agent", "detail": "unknown trust tier", "contribution": round(contrib, 3)})

        for it in items:
            if it.get("qty", 1) >= 5:
                contrib = weights.get("unusual_quantity", 0)
                score += contrib
                factors.append({"factor": "unusual_quantity", "detail": f"{it['qty']}x {it['product_id']}", "contribution": round(contrib, 3)})
                break

        for it in items:
            prod = catalog.get(it["product_id"])
            if prod and it.get("qty", 1) > 0.75 * prod.get("stock", 1e9):
                contrib = weights.get("low_stock", 0)
                score += contrib
                factors.append({"factor": "low_stock", "detail": f"near stock limit for {it['product_id']}", "contribution": round(contrib, 3)})
                break

        return {"score": round(min(score, 1.0), 3), "factors": factors}

    def evaluate(self, plan: dict, tier: str = "unknown") -> dict:
        """Decide allow / deny / needs_confirmation for a proposed plan.

        plan = {"items": [{"product_id","qty","price"}...], "amount": float}
        Returns a structured decision the Executor and UI consume.
        """
        cfg = self.config
        g = cfg.get("global", {})
        tier_cfg = self.tier_config(tier)
        items = plan.get("items", [])
        amount = float(plan.get("amount", 0))

        violations: list[str] = []
        decision = "allow"

        # Global hard ceiling
        if amount > float(g.get("max_transaction_amount", 1e18)):
            violations.append(f"amount {amount} exceeds global ceiling {g['max_transaction_amount']}")
            decision = "deny"

        # Blocked categories
        blocked = set(g.get("blocked_categories", []))
        for it in items:
            prod = catalog.get(it["product_id"])
            if prod and prod.get("category") in blocked:
                violations.append(f"category '{prod['category']}' is blocked")
                decision = "deny"

        # Tier ceiling
        if amount > float(tier_cfg.get("max_transaction_amount", 1e18)):
            violations.append(
                f"amount {amount} exceeds tier '{tier}' ceiling {tier_cfg['max_transaction_amount']}"
            )
            decision = "deny"

        # Item count
        total_qty = sum(it.get("qty", 1) for it in items)
        if total_qty > int(tier_cfg.get("max_items_per_order", 10**9)):
            violations.append(f"item count {total_qty} exceeds tier limit {tier_cfg['max_items_per_order']}")
            decision = "deny"

        # Stock availability (deterministic guard)
        for it in items:
            prod = catalog.get(it["product_id"])
            if not prod:
                violations.append(f"unknown product {it['product_id']}")
                decision = "deny"
            elif it.get("qty", 1) > prod.get("stock", 0):
                violations.append(f"insufficient stock for {it['product_id']}")
                decision = "deny"

        risk = self.score_risk(items, amount, tier)

        # Confirmation thresholds (only matter if not already denied)
        if decision != "deny":
            if amount > float(g.get("require_confirmation_above", 1e18)):
                decision = "needs_confirmation"
                violations.append(f"amount above global confirmation threshold {g['require_confirmation_above']}")
            elif risk["score"] > float(tier_cfg.get("risk_tolerance", 1.0)):
                decision = "needs_confirmation"
                violations.append(
                    f"risk {risk['score']} exceeds tier tolerance {tier_cfg['risk_tolerance']}"
                )
            elif not tier_cfg.get("allow_auto_execute", True):
                decision = "needs_confirmation"
                violations.append(f"tier '{tier}' requires human confirmation")

        return {
            "decision": decision,
            "tier": tier,
            "risk": risk,
            "confidence": round(1.0 - risk["score"], 3),
            "violations": violations,
            "policy_version": cfg.get("version"),
        }


policy_engine = PolicyEngine()
