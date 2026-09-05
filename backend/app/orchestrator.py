"""Orchestrator: the Planner -> Critic -> Executor state machine (Tier 1 + #35).

This is the spine. Every user/agent message flows through here. The LLM proposes
(planner), deterministic policy validates (critic), and only then does the
executor move money (payments). Every step emits an event so the dashboard can
animate the decision timeline live, and every consequential step is written to
the hash-chained audit log.
"""
from __future__ import annotations

import time
import uuid
from threading import Lock
from typing import Any, Callable, Optional

from .audit import audit
from .catalog import catalog
from .payments import payment_engine
from .planner import plan as make_plan
from .policy import policy_engine
from . import receipts


class Session:
    def __init__(self, session_id: str, tier: str = "unknown"):
        self.session_id = session_id
        self.tier = tier
        self.cart: list[dict] = []          # [{product_id, qty, price}]
        self.pending: Optional[dict] = None  # plan awaiting confirmation
        self.created_at = time.time()

    def cart_amount(self) -> float:
        return sum(i["qty"] * i["price"] for i in self.cart)


class Orchestrator:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = Lock()
        self.kill_switch = False          # global halt (#, big red button)
        self.revenue = 0.0                # settled revenue
        self.orders_settled = 0
        self.recovered = 0.0              # cart-recovery / upsell attributed revenue
        self._subscribers: list[Callable[[dict], None]] = []

    # ---- pub/sub for the live dashboard ----
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

    # ---- session management ----
    def get_session(self, session_id: Optional[str] = None, tier: str = "unknown") -> Session:
        with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
            sid = session_id or ("sess_" + uuid.uuid4().hex[:12])
            sess = Session(sid, tier=tier)
            self._sessions[sid] = sess
            audit.append("session_open", {"tier": tier}, session_id=sid)
            self._emit("session_open", {"tier": tier}, session_id=sid)
            return sess

    def set_kill_switch(self, on: bool) -> None:
        self.kill_switch = on
        audit.append("kill_switch", {"engaged": on})
        self._emit("kill_switch", {"engaged": on})

    def stats(self) -> dict:
        return {
            "revenue": round(self.revenue, 2),
            "orders_settled": self.orders_settled,
            "recovered": round(self.recovered, 2),
            "kill_switch": self.kill_switch,
            "payment_mode": payment_engine.mode,
            "active_sessions": len(self._sessions),
        }

    # ---- the pipeline ----
    def handle_message(self, text: str, session_id: Optional[str] = None, tier: str = "unknown") -> dict:
        sess = self.get_session(session_id, tier=tier)

        self._emit("user_message", {"text": text}, session_id=sess.session_id)
        audit.append("user_message", {"text": text, "tier": sess.tier}, session_id=sess.session_id)

        if self.kill_switch:
            reply = {"reply": "🛑 Kill switch engaged. All agent actions are halted.", "halted": True}
            self._emit("halted", reply, session_id=sess.session_id)
            return {"session_id": sess.session_id, **reply}

        # 1) PLANNER
        proposal = make_plan(text)
        self._emit("plan", {"proposal": proposal}, session_id=sess.session_id)
        audit.append("plan", {"proposal": proposal}, session_id=sess.session_id)

        intent = proposal.get("intent", "help")

        if intent == "search":
            return self._do_search(sess, proposal)
        if intent == "add":
            return self._do_add(sess, proposal)
        if intent == "checkout":
            return self._do_checkout(sess)
        return self._reply(sess, "I can help you search the catalog and place an order. Try: \"find a blue t-shirt under 800\".", proposal)

    def _reply(self, sess: Session, text: str, extra: Any = None) -> dict:
        payload = {"reply": text}
        self._emit("agent_reply", {"text": text, "extra": extra}, session_id=sess.session_id)
        return {"session_id": sess.session_id, **payload}

    def _do_search(self, sess: Session, proposal: dict) -> dict:
        ids = proposal.get("results") or []
        if not ids:
            results = catalog.search(proposal.get("query", ""), proposal.get("max_price"))
            ids = [p["id"] for p in results[:5]]
        products = [catalog.get(i) for i in ids if catalog.get(i)]
        lines = [f"{p['title']} — ₹{p['price']}" for p in products]
        text = "Here's what I found:\n" + "\n".join(f"• {l}" for l in lines) if lines else "No matches found."
        # cross-sell hint (#53)
        upsell = catalog.cross_sell(products[0]["id"]) if products else []
        self._emit("search_results", {"products": products, "upsell": upsell}, session_id=sess.session_id)
        return {"session_id": sess.session_id, "reply": text, "products": products,
                "upsell": [u["id"] for u in upsell]}

    def _do_add(self, sess: Session, proposal: dict) -> dict:
        items = proposal.get("items", [])
        # normalize price from catalog to prevent LLM drift
        norm: list[dict] = []
        for it in items:
            prod = catalog.get(it["product_id"])
            if not prod:
                continue
            norm.append({"product_id": prod["id"], "qty": int(it.get("qty", 1)), "price": prod["price"]})
        if not norm:
            return self._reply(sess, "I couldn't match that to a product in the catalog.")

        # merge into cart
        for it in norm:
            existing = next((c for c in sess.cart if c["product_id"] == it["product_id"]), None)
            if existing:
                existing["qty"] += it["qty"]
            else:
                sess.cart.append(dict(it))

        added = ", ".join(f"{i['qty']}x {catalog.get(i['product_id'])['title']}" for i in norm)
        upsell = catalog.cross_sell(norm[0]["product_id"])
        self._emit("cart_update", {"cart": sess.cart, "amount": sess.cart_amount(),
                                   "upsell": [u["id"] for u in upsell]}, session_id=sess.session_id)

        msg = f"Added {added}. Cart total: ₹{sess.cart_amount():.0f}."
        if upsell:
            msg += " Frequently bought together: " + ", ".join(f"{u['title']} (₹{u['price']})" for u in upsell) + "."
        msg += " Say 'checkout' to place the order."
        return {"session_id": sess.session_id, "reply": msg, "cart": sess.cart,
                "amount": sess.cart_amount(), "upsell": [u["id"] for u in upsell]}

    def _do_checkout(self, sess: Session, confirmed: bool = False) -> dict:
        if not sess.cart:
            return self._reply(sess, "Your cart is empty.")

        amount = sess.cart_amount()
        plan = {"items": sess.cart, "amount": amount}

        # 2) CRITIC (policy)
        decision = policy_engine.evaluate(plan, tier=sess.tier)
        self._emit("policy_decision", {"decision": decision, "amount": amount}, session_id=sess.session_id)
        audit.append("policy_decision", {"decision": decision, "amount": amount}, session_id=sess.session_id)

        if decision["decision"] == "deny":
            self._emit("denied", {"violations": decision["violations"]}, session_id=sess.session_id)
            return {"session_id": sess.session_id, "reply": "❌ Order denied by policy: " + "; ".join(decision["violations"]),
                    "decision": decision}

        if decision["decision"] == "needs_confirmation" and not confirmed:
            sess.pending = plan
            self._emit("needs_confirmation", {"decision": decision, "amount": amount}, session_id=sess.session_id)
            return {"session_id": sess.session_id,
                    "reply": f"⚠️ This order (₹{amount:.0f}) needs human approval: " + "; ".join(decision["violations"]) +
                             " — approve from the dashboard.",
                    "decision": decision, "needs_confirmation": True}

        return self._execute(sess, plan, decision)

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
        decision = policy_engine.evaluate(plan, tier=sess.tier)
        audit.append("human_approved", {"amount": plan["amount"]}, session_id=session_id)
        return self._execute(sess, plan, decision)

    def _execute(self, sess: Session, plan: dict, decision: dict) -> dict:
        # 3) EXECUTOR — deterministic money movement
        if self.kill_switch:
            return self._reply(sess, "🛑 Kill switch engaged; execution blocked.")

        amount = plan["amount"]
        order = payment_engine.create_order(sess.session_id, amount, catalog.merchant.get("currency", "INR"), plan["items"])
        payment_engine.issue_link(order["order_id"])
        self._emit("order_created", {"order": _order_view(order), "decision": decision}, session_id=sess.session_id)
        audit.append("order_created", {"order_id": order["order_id"], "amount": amount}, session_id=sess.session_id)

        def _on_settled(rec: dict):
            self.revenue += rec["amount"]
            self.orders_settled += 1
            receipt = receipts.issue({
                "merchant_id": catalog.merchant.get("id"),
                "session_id": sess.session_id,
                "order_id": rec["order_id"],
                "items": rec["items"],
                "amount": rec["amount"],
                "currency": rec["currency"],
                "payment_ref": rec.get("payment_ref"),
                "status": "settled",
            })
            audit.append("settled", {"order_id": rec["order_id"], "amount": rec["amount"],
                                     "receipt_id": receipt["body"]["receipt_id"]}, session_id=sess.session_id)
            self._emit("settled", {"order": _order_view(rec), "receipt": receipt,
                                   "stats": self.stats()}, session_id=sess.session_id)
            sess.cart = []

        # simulated path drives the webhook itself; real path waits for Razorpay webhook
        if payment_engine.mode == "simulated":
            payment_engine.simulate_payment(order["order_id"], on_settled=_on_settled)
        else:
            # store callback for the webhook handler to fire
            _PENDING_SETTLE[order["order_id"]] = _on_settled

        return {"session_id": sess.session_id,
                "reply": f"✅ Order {order['order_id']} placed for ₹{amount:.0f}. "
                         f"Payment link issued; waiting for settlement…",
                "order": _order_view(order), "decision": decision}


def _order_view(order: dict) -> dict:
    return {k: order[k] for k in ("order_id", "amount", "currency", "status", "items",
                                  "payment_link", "payment_ref", "events", "mode") if k in order}


# callbacks awaiting a real Razorpay webhook
_PENDING_SETTLE: dict[str, Callable[[dict], None]] = {}

orchestrator = Orchestrator()
