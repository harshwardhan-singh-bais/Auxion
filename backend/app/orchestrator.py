"""Orchestrator: the Planner -> Critic -> Executor state machine.

The spine, fully wired: tracing spans per stage, JWT scope enforcement, trust
tiers, campaign hooks (cart recovery + upsell), graceful failure + refund, kill
switch, persisted revenue, and live event fan-out for the dashboard. Every
consequential step is hash-chained into the audit log.

The LLM proposes (planner); deterministic policy validates (critic); only then
does the executor move money (payments).
"""
from __future__ import annotations

import time
import uuid
from threading import Lock
from typing import Any, Callable, Optional

from . import db, receipts
from .audit import audit
from .auth import verify_token
from .campaigns import campaign_engine
from .catalog import catalog
from .payments import payment_engine
from .planner import plan as make_plan
from .policy import policy_engine
from .tracing import tracer


class Session:
    def __init__(self, session_id: str, tier: str = "unknown", token_max: Optional[float] = None):
        self.session_id = session_id
        self.tier = tier
        self.token_max = token_max
        self.cart: list[dict] = []
        self.pending: Optional[dict] = None
        self.recovery_armed = False
        self.created_at = time.time()

    def cart_amount(self) -> float:
        return sum(i["qty"] * i["price"] for i in self.cart)


class Orchestrator:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = Lock()
        self.kill_switch = False
        self.demo_mode = False           # #68 rate-limited safe demo toggle
        self.revenue = db.get_metric("revenue", 0.0)
        self.orders_settled = int(db.get_metric("orders_settled", 0))
        self.recovered = db.get_metric("recovered", 0.0)
        self.upsell_revenue = db.get_metric("upsell_revenue", 0.0)
        self._subscribers: list[Callable[[dict], None]] = []
        campaign_engine.bind(self._campaign_emit)

    # ---- pub/sub ----
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subscribers.append(fn)

    def unsubscribe(self, fn: Callable[[dict], None]) -> None:
        if fn in self._subscribers:
            self._subscribers.remove(fn)

    def _emit(self, event_type: str, payload: dict, session_id: Optional[str] = None) -> None:
        evt = {"type": event_type, "ts": time.time(), "session_id": session_id, **payload}
        for fn in list(self._subscribers):
            try:
                fn(evt)
            except Exception:
                pass

    def _campaign_emit(self, etype: str, payload: dict, session_id: Optional[str]) -> None:
        audit.append(etype, payload, session_id=session_id)
        self._emit(etype, payload, session_id=session_id)

    # ---- persistence of metrics ----
    def _persist_metrics(self) -> None:
        db.set_metric("revenue", self.revenue)
        db.set_metric("orders_settled", self.orders_settled)
        db.set_metric("recovered", self.recovered)
        db.set_metric("upsell_revenue", self.upsell_revenue)

    # ---- sessions ----
    def get_session(self, session_id: Optional[str] = None, tier: str = "unknown",
                    token: Optional[str] = None) -> Session:
        token_max = None
        if token:
            claims = verify_token(token)
            if claims:
                tier = claims.get("tier", tier)
                token_max = claims.get("max_amount")
        with self._lock:
            if session_id and session_id in self._sessions:
                s = self._sessions[session_id]
                if token_max is not None:
                    s.token_max = token_max
                    s.tier = tier
                return s
            sid = session_id or ("sess_" + uuid.uuid4().hex[:12])
            sess = Session(sid, tier=tier, token_max=token_max)
            self._sessions[sid] = sess
            db.upsert_session(sid, tier, catalog.active, sess.created_at, {"token_scoped": token_max is not None})
            audit.append("session_open", {"tier": tier, "token_scoped": token_max is not None}, session_id=sid)
            self._emit("session_open", {"tier": tier}, session_id=sid)
            return sess

    def set_kill_switch(self, on: bool) -> None:
        self.kill_switch = on
        audit.append("kill_switch", {"engaged": on})
        self._emit("kill_switch", {"engaged": on})

    def set_demo_mode(self, on: bool) -> None:
        self.demo_mode = on
        self._emit("demo_mode", {"enabled": on})

    def stats(self) -> dict:
        return {
            "revenue": round(self.revenue, 2),
            "orders_settled": self.orders_settled,
            "recovered": round(self.recovered, 2),
            "upsell_revenue": round(self.upsell_revenue, 2),
            "kill_switch": self.kill_switch,
            "demo_mode": self.demo_mode,
            "payment_mode": payment_engine.mode,
            "active_sessions": len(self._sessions),
            "merchant": catalog.merchant,
            "campaigns": campaign_engine.stats(),
        }

    # ---- pipeline ----
    def handle_message(self, text: str, session_id: Optional[str] = None,
                       tier: str = "unknown", token: Optional[str] = None) -> dict:
        sess = self.get_session(session_id, tier=tier, token=token)
        trace = tracer.start_trace("handle_message", sess.session_id)

        self._emit("user_message", {"text": text}, session_id=sess.session_id)
        audit.append("user_message", {"text": text, "tier": sess.tier}, session_id=sess.session_id)

        if self.kill_switch:
            tracer.end_trace(trace)
            reply = {"reply": "🛑 Kill switch engaged. All agent actions are halted.", "halted": True}
            self._emit("halted", reply, session_id=sess.session_id)
            return {"session_id": sess.session_id, **reply}

        # 1) PLANNER
        with tracer.span(trace, "planner", {"source": "llm" if False else "auto"}) as span:
            proposal = make_plan(text)
            span["attributes"]["intent"] = proposal.get("intent")
            span["attributes"]["source"] = proposal.get("_source")
        self._emit("plan", {"proposal": proposal, "trace_id": trace.trace_id}, session_id=sess.session_id)
        audit.append("plan", {"proposal": proposal}, session_id=sess.session_id)

        intent = proposal.get("intent", "help")
        try:
            if intent == "search":
                return self._do_search(sess, proposal, trace)
            if intent == "recommend":
                return self._do_recommend(sess, proposal, trace)
            if intent == "add":
                return self._do_add(sess, proposal, trace)
            if intent == "remove":
                return self._do_remove(sess, proposal, trace)
            if intent == "checkout":
                return self._do_checkout(sess, trace=trace)
            return self._reply(sess, "I can help you search the catalog, build a cart, and place an order. "
                                     "Try: \"find a blue t-shirt under 800\", then \"checkout\".", trace)
        finally:
            tracer.end_trace(trace)

    def _reply(self, sess: Session, text: str, trace=None, extra: Any = None) -> dict:
        self._emit("agent_reply", {"text": text, "extra": extra,
                                   "trace_id": trace.trace_id if trace else None}, session_id=sess.session_id)
        return {"session_id": sess.session_id, "reply": text,
                "trace_id": trace.trace_id if trace else None}

    def _do_search(self, sess: Session, proposal: dict, trace) -> dict:
        with tracer.span(trace, "catalog_search"):
            ids = proposal.get("results") or []
            if not ids:
                ids = [p["id"] for p in catalog.search(proposal.get("query", ""), proposal.get("max_price"))[:6]]
            products = [catalog.get(i) for i in ids if catalog.get(i)]
        lines = [f"{p['title']} — ₹{p['price']}" for p in products]
        text = ("Here's what I found:\n" + "\n".join(f"• {l}" for l in lines)) if lines else \
               "No matches found. Try another search."
        upsell = catalog.cross_sell(products[0]["id"]) if products else []
        self._emit("search_results", {"products": products, "upsell": upsell,
                                      "trace_id": trace.trace_id}, session_id=sess.session_id)
        return {"session_id": sess.session_id, "reply": text, "products": products,
                "upsell": [u["id"] for u in upsell], "trace_id": trace.trace_id}

    def _do_recommend(self, sess: Session, proposal: dict, trace) -> dict:
        pid = proposal.get("product_id") or (sess.cart[-1]["product_id"] if sess.cart else None)
        recs = campaign_engine.cross_sell(pid) if pid else []
        if not recs and sess.cart:
            recs = campaign_engine.cross_sell(sess.cart[0]["product_id"])
        text = ("You might also like: " + ", ".join(f"{r['title']} (₹{r['price']})" for r in recs)) if recs \
               else "I don't have a specific recommendation yet — add something to your cart first."
        self._emit("search_results", {"products": recs, "upsell": [], "trace_id": trace.trace_id},
                   session_id=sess.session_id)
        return {"session_id": sess.session_id, "reply": text, "products": recs, "trace_id": trace.trace_id}

    def _do_add(self, sess: Session, proposal: dict, trace) -> dict:
        with tracer.span(trace, "cart_add"):
            items = proposal.get("items", [])
            norm: list[dict] = []
            for it in items:
                prod = catalog.get(it["product_id"])
                if not prod:
                    continue
                norm.append({"product_id": prod["id"], "qty": int(it.get("qty", 1)), "price": prod["price"]})
            if not norm:
                return self._reply(sess, "I couldn't match that to a product in the catalog.", trace)
            for it in norm:
                existing = next((c for c in sess.cart if c["product_id"] == it["product_id"]), None)
                if existing:
                    existing["qty"] += it["qty"]
                else:
                    sess.cart.append(dict(it))

        added = ", ".join(f"{i['qty']}x {catalog.get(i['product_id'])['title']}" for i in norm)
        upsell = catalog.cross_sell(norm[0]["product_id"])
        bundle = campaign_engine.bundle_offer(sess.cart)

        # arm cart-recovery campaign
        sess.recovery_armed = True
        campaign_engine.schedule_recovery(sess.session_id, sess.cart)

        self._emit("cart_update", {"cart": sess.cart, "amount": sess.cart_amount(),
                                   "upsell": [u["id"] for u in upsell], "bundle": bundle,
                                   "trace_id": trace.trace_id}, session_id=sess.session_id)

        msg = f"Added {added}. Cart total: ₹{sess.cart_amount():.0f}."
        if upsell:
            msg += " Frequently bought together: " + ", ".join(f"{u['title']} (₹{u['price']})" for u in upsell) + "."
        if bundle:
            msg += (f" 💡 Bundle offer: add {', '.join(bundle['add'])} for the '{bundle['title']}' "
                    f"at ₹{bundle['bundle_price']} (save ₹{bundle['save']}).")
        msg += " Say 'checkout' to place the order."
        return {"session_id": sess.session_id, "reply": msg, "cart": sess.cart,
                "amount": sess.cart_amount(), "upsell": [u["id"] for u in upsell],
                "bundle": bundle, "trace_id": trace.trace_id}

    def _do_remove(self, sess: Session, proposal: dict, trace) -> dict:
        pid = proposal.get("product_id")
        before = len(sess.cart)
        sess.cart = [c for c in sess.cart if c["product_id"] != pid]
        self._emit("cart_update", {"cart": sess.cart, "amount": sess.cart_amount(),
                                   "trace_id": trace.trace_id}, session_id=sess.session_id)
        if len(sess.cart) < before:
            return {"session_id": sess.session_id, "reply": f"Removed {pid}. Cart total: ₹{sess.cart_amount():.0f}.",
                    "cart": sess.cart, "amount": sess.cart_amount(), "trace_id": trace.trace_id}
        return self._reply(sess, "That item wasn't in your cart.", trace)

    def _do_checkout(self, sess: Session, confirmed: bool = False, trace=None) -> dict:
        if not sess.cart:
            return self._reply(sess, "Your cart is empty.", trace)

        amount = sess.cart_amount()
        plan = {"items": list(sess.cart), "amount": amount}

        # 2) CRITIC (policy) — record action for velocity guard first
        policy_engine.record_action(sess.session_id)
        with (tracer.span(trace, "policy_evaluate") if trace else _null()):
            decision = policy_engine.evaluate(plan, tier=sess.tier, session_id=sess.session_id,
                                              token_max=sess.token_max)
        self._emit("policy_decision", {"decision": decision, "amount": amount,
                                       "trace_id": trace.trace_id if trace else None}, session_id=sess.session_id)
        audit.append("policy_decision", {"decision": decision, "amount": amount}, session_id=sess.session_id)

        if decision["decision"] == "deny":
            self._emit("denied", {"violations": decision["violations"]}, session_id=sess.session_id)
            return {"session_id": sess.session_id,
                    "reply": "❌ Order denied by policy: " + "; ".join(decision["violations"]),
                    "decision": decision, "trace_id": trace.trace_id if trace else None}

        if decision["decision"] == "needs_confirmation" and not confirmed:
            sess.pending = plan
            self._emit("needs_confirmation", {"decision": decision, "amount": amount}, session_id=sess.session_id)
            return {"session_id": sess.session_id,
                    "reply": f"⚠️ This order (₹{amount:.0f}) needs human approval: "
                             + "; ".join(decision["violations"]) + " — approve from the dashboard.",
                    "decision": decision, "needs_confirmation": True,
                    "trace_id": trace.trace_id if trace else None}

        return self._execute(sess, plan, decision, trace)

    def confirm_pending(self, session_id: str, approve: bool) -> dict:
        sess = self.get_session(session_id)
        if not sess.pending:
            return {"session_id": session_id, "reply": "Nothing pending approval."}
        plan = sess.pending
        sess.pending = None
        if not approve:
            audit.append("human_denied", {"amount": plan["amount"]}, session_id=session_id)
            self._emit("denied", {"violations": ["human rejected"]}, session_id=session_id)
            return {"session_id": session_id, "reply": "Order rejected by human reviewer."}
        decision = policy_engine.evaluate(plan, tier=sess.tier, session_id=session_id, token_max=sess.token_max)
        audit.append("human_approved", {"amount": plan["amount"]}, session_id=session_id)
        return self._execute(sess, plan, decision, None)

    def _execute(self, sess: Session, plan: dict, decision: dict, trace) -> dict:
        # 3) EXECUTOR — deterministic money movement
        if self.kill_switch:
            return self._reply(sess, "🛑 Kill switch engaged; execution blocked.", trace)

        amount = plan["amount"]
        recovery_attributed = campaign_engine.mark_converted(sess.session_id, amount)

        with (tracer.span(trace, "executor_create_order") if trace else _null()):
            order = payment_engine.create_order(sess.session_id, amount,
                                                catalog.merchant.get("currency", "INR"),
                                                plan["items"], catalog.active)
            payment_engine.issue_link(order["order_id"])
        self._emit("order_created", {"order": _order_view(order), "decision": decision,
                                     "trace_id": trace.trace_id if trace else None}, session_id=sess.session_id)
        audit.append("order_created", {"order_id": order["order_id"], "amount": amount}, session_id=sess.session_id)

        # attribute upsell revenue: value of cross-sell items present in the cart
        upsell_amt = 0.0
        for it in plan["items"]:
            for other in plan["items"]:
                if it is not other and catalog.get(other["product_id"]) in catalog.cross_sell(it["product_id"]):
                    upsell_amt += other["qty"] * other["price"]

        def _on_settled(rec: dict):
            self.revenue += rec["amount"]
            self.orders_settled += 1
            if recovery_attributed:
                self.recovered += recovery_attributed
            if upsell_amt:
                self.upsell_revenue += upsell_amt
            self._persist_metrics()
            receipt = receipts.issue({
                "merchant": catalog.active, "session_id": sess.session_id, "order_id": rec["order_id"],
                "items": rec["items"], "amount": rec["amount"], "currency": rec["currency"],
                "payment_ref": rec.get("payment_ref"), "status": "settled",
            })
            rec["receipt"] = receipt
            db.save_order(rec)
            audit.append("settled", {"order_id": rec["order_id"], "amount": rec["amount"],
                                     "receipt_id": receipt["body"]["receipt_id"],
                                     "recovered": recovery_attributed, "upsell": upsell_amt},
                         session_id=sess.session_id)
            self._emit("settled", {"order": _order_view(rec), "receipt": receipt,
                                   "stats": self.stats()}, session_id=sess.session_id)
            sess.cart = []

        def _on_failed(rec: dict):
            # graceful failure: mark failed, notify, keep cart so user can retry — no money lost
            audit.append("payment_failed", {"order_id": rec["order_id"], "reason": rec.get("failure_reason")},
                         session_id=sess.session_id)
            self._emit("payment_failed", {"order": _order_view(rec), "reason": rec.get("failure_reason"),
                                          "recovery": "cart preserved; customer can retry, no charge captured"},
                       session_id=sess.session_id)

        if payment_engine.mode == "simulated":
            payment_engine.simulate_payment(order["order_id"], on_settled=_on_settled, on_failed=_on_failed)
        else:
            _PENDING_SETTLE[order["order_id"]] = _on_settled

        return {"session_id": sess.session_id,
                "reply": f"✅ Order {order['order_id']} placed for ₹{amount:.0f}. "
                         f"Payment link issued; awaiting settlement…",
                "order": _order_view(order), "decision": decision,
                "trace_id": trace.trace_id if trace else None}

    # ---- refund / graceful ops ----
    def refund_order(self, order_id: str) -> dict:
        rec = payment_engine.refund(order_id)
        if "error" not in rec:
            self.revenue = max(0.0, self.revenue - rec.get("amount", 0))
            self._persist_metrics()
            audit.append("refund", {"order_id": order_id, "amount": rec.get("amount")},
                         session_id=rec.get("session_id"))
            self._emit("refund", {"order": _order_view(rec), "stats": self.stats()},
                       session_id=rec.get("session_id"))
        return rec

    def arm_failure(self) -> None:
        """Prime the next payment to fail, for the graceful-failure demo."""
        payment_engine.fail_next = True
        audit.append("failure_armed", {"note": "next payment will fail (demo)"})
        self._emit("failure_armed", {})


class _null:
    def __enter__(self):
        return {}
    def __exit__(self, *a):
        return False


def _order_view(order: dict) -> dict:
    keys = ("order_id", "amount", "currency", "status", "items", "payment_link",
            "payment_ref", "events", "mode", "failure_reason", "refund_ref", "session_id", "receipt")
    return {k: order[k] for k in keys if k in order}


_PENDING_SETTLE: dict[str, Callable[[dict], None]] = {}

orchestrator = Orchestrator()
