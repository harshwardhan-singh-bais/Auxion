"""Hot-swappable policy engine (#13) — the deterministic Critic.

Trust tiers (#65), risk scoring, velocity/rate guard, blocked categories, stock
guard, confirmation thresholds, and a counterfactual simulator (#66) that shows
what WOULD have happened with a given gate removed — proving gates aren't theater.

The LLM never decides whether money moves. This does.
"""
from __future__ import annotations

import copy
import time
from collections import defaultdict, deque
from pathlib import Path

import yaml

from .catalog import catalog
from .config import CONFIG_DIR

POLICY_PATH = CONFIG_DIR / "policy.yaml"


class PolicyEngine:
    def __init__(self, path: Path = POLICY_PATH):
        self.path = path
        self._cfg: dict = {}
        self._mtime: float = 0.0
        # session_id -> deque[timestamps] for velocity guard
        self._velocity: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
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
        tiers = self.config.get("tiers", {})
        return tiers.get(tier, tiers.get("unknown", {}))

    def record_action(self, session_id: str) -> None:
        self._velocity[session_id].append(time.time())

    def _velocity_count(self, session_id: str, window: float = 60.0) -> int:
        now = time.time()
        return sum(1 for t in self._velocity.get(session_id, []) if now - t <= window)

    def score_risk(self, items: list[dict], amount: float, tier: str) -> dict:
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

    def evaluate(self, plan: dict, tier: str = "unknown", session_id: str = "", token_max: float | None = None) -> dict:
        """Decide allow / deny / needs_confirmation. Pure function of config + plan."""
        cfg = self.config
        g = cfg.get("global", {})
        tier_cfg = self.tier_config(tier)
        items = plan.get("items", [])
        amount = float(plan.get("amount", 0))

        violations: list[str] = []
        gates: list[dict] = []   # every gate evaluated, for explainability + counterfactual
        decision = "allow"

        def gate(name: str, passed: bool, detail: str, fatal: bool = True):
            nonlocal decision
            gates.append({"gate": name, "passed": passed, "detail": detail})
            if not passed:
                violations.append(detail)
                if fatal:
                    decision = "deny"

        # Global hard ceiling
        gate("global_ceiling", amount <= float(g.get("max_transaction_amount", 1e18)),
             f"amount {amount} exceeds global ceiling {g.get('max_transaction_amount')}")

        # Token scope ceiling (JWT)
        if token_max is not None:
            gate("token_scope", amount <= float(token_max),
                 f"amount {amount} exceeds token-scoped max {token_max}")

        # Blocked categories
        blocked = set(g.get("blocked_categories", []))
        bad_cat = next((catalog.get(it["product_id"]) for it in items
                        if catalog.get(it["product_id"]) and catalog.get(it["product_id"]).get("category") in blocked), None)
        gate("blocked_category", bad_cat is None,
             f"category '{bad_cat['category']}' is blocked" if bad_cat else "no blocked categories")

        # Tier ceiling
        gate("tier_ceiling", amount <= float(tier_cfg.get("max_transaction_amount", 1e18)),
             f"amount {amount} exceeds tier '{tier}' ceiling {tier_cfg.get('max_transaction_amount')}")

        # Item count
        total_qty = sum(it.get("qty", 1) for it in items)
        gate("item_count", total_qty <= int(tier_cfg.get("max_items_per_order", 10**9)),
             f"item count {total_qty} exceeds tier limit {tier_cfg.get('max_items_per_order')}")

        # Stock guard
        stock_ok = True
        stock_detail = "stock available"
        for it in items:
            prod = catalog.get(it["product_id"])
            if not prod:
                stock_ok = False; stock_detail = f"unknown product {it['product_id']}"; break
            if it.get("qty", 1) > prod.get("stock", 0):
                stock_ok = False; stock_detail = f"insufficient stock for {it['product_id']}"; break
        gate("stock", stock_ok, stock_detail)

        # Velocity / rate guard
        vmax = int(g.get("velocity_max_orders_per_min", 10**9))
        vcount = self._velocity_count(session_id) if session_id else 0
        gate("velocity", vcount < vmax,
             f"velocity {vcount}/min exceeds limit {vmax}")

        risk = self.score_risk(items, amount, tier)

        # Confirmation gates — always RECORD their status (so counterfactual can
        # reason about them even when a fatal gate already denied), but only let
        # them flip an otherwise-allowed decision to needs_confirmation.
        confirm_needed = amount > float(g.get("require_confirmation_above", 1e18))
        risk_exceeds = risk["score"] > float(tier_cfg.get("risk_tolerance", 1.0))
        manual_tier = not tier_cfg.get("allow_auto_execute", True)

        gates.append({"gate": "confirm_threshold", "passed": not confirm_needed,
                      "detail": f"amount above confirmation threshold {g.get('require_confirmation_above')}"
                      if confirm_needed else "below confirmation threshold"})
        gates.append({"gate": "risk_tolerance", "passed": not risk_exceeds,
                      "detail": f"risk {risk['score']} exceeds tier tolerance {tier_cfg.get('risk_tolerance')}"
                      if risk_exceeds else f"risk {risk['score']} within tolerance"})

        if decision != "deny":
            if confirm_needed:
                decision = "needs_confirmation"
                violations.append(f"amount above confirmation threshold {g.get('require_confirmation_above')}")
            elif risk_exceeds:
                decision = "needs_confirmation"
                violations.append(f"risk {risk['score']} exceeds tier tolerance {tier_cfg.get('risk_tolerance')}")
            elif manual_tier:
                decision = "needs_confirmation"
                violations.append(f"tier '{tier}' requires human confirmation")

        return {
            "decision": decision,
            "tier": tier,
            "risk": risk,
            "confidence": round(1.0 - risk["score"], 3),
            "violations": violations,
            "gates": gates,
            "policy_version": cfg.get("version"),
            "amount": amount,
        }

    def counterfactual(self, plan: dict, tier: str, remove_gate: str, session_id: str = "") -> dict:
        """#66: re-evaluate as if `remove_gate` didn't exist. Shows the gate's real effect."""
        base = self.evaluate(plan, tier, session_id)
        # Simulate removal: filter that gate's violation and recompute decision naively.
        kept_gates = [gt for gt in base["gates"] if gt["gate"] != remove_gate]
        fatal_failed = any(not gt["passed"] for gt in kept_gates
                           if gt["gate"] in {"global_ceiling", "token_scope", "blocked_category",
                                             "tier_ceiling", "item_count", "stock", "velocity"})
        would = "deny" if fatal_failed else ("needs_confirmation"
                 if any(gt["gate"] in {"confirm_threshold", "risk_tolerance"} and not gt["passed"] for gt in kept_gates)
                 else "allow")
        return {
            "with_gate": base["decision"],
            "without_gate": would,
            "removed_gate": remove_gate,
            "changed": base["decision"] != would,
            "explanation": f"With '{remove_gate}' active the order is '{base['decision']}'. "
                           f"Remove it and the order would be '{would}'.",
        }


policy_engine = PolicyEngine()
