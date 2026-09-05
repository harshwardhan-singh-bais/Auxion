"""Auxion merchant-agent API + live control-room dashboard.

Run from backend/:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, receipts
from .audit import audit
from .auth import issue_token, verify_token
from .campaigns import campaign_engine
from .catalog import catalog
from .config import STATIC_DIR, settings
from .mcp_server import call_tool, list_tools
from .orchestrator import orchestrator, _PENDING_SETTLE, _order_view
from .payments import payment_engine
from .policy import policy_engine
from .seed import seed_demo
from .tracing import tracer

db.init_db()

app = FastAPI(title="Auxion Agentic Commerce", version="2.0")


# ---------- live event fan-out ----------
class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, event: dict):
        if self.loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(event), self.loop)
        except Exception:
            pass

    async def _broadcast(self, event: dict):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub()
orchestrator.subscribe(hub.publish)


@app.on_event("startup")
async def _startup():
    hub.loop = asyncio.get_running_loop()


# ---------- models ----------
class ChatIn(BaseModel):
    text: str
    session_id: str | None = None
    tier: str = "unknown"
    token: str | None = None


class ConfirmIn(BaseModel):
    session_id: str
    approve: bool


class KillIn(BaseModel):
    engaged: bool


class DemoModeIn(BaseModel):
    enabled: bool


class PolicyIn(BaseModel):
    yaml: str


class ReceiptIn(BaseModel):
    receipt: dict


class TokenIn(BaseModel):
    tier: str = "trusted"
    max_amount: float = 15000
    ttl: int = 3600


class CounterfactualIn(BaseModel):
    session_id: str | None = None
    items: list[dict]
    tier: str = "unknown"
    remove_gate: str


class MerchantIn(BaseModel):
    merchant_id: str


class McpCallIn(BaseModel):
    name: str
    arguments: dict = {}


# ---------- core chat ----------
@app.post("/api/chat")
def chat(body: ChatIn):
    return orchestrator.handle_message(body.text, session_id=body.session_id, tier=body.tier, token=body.token)


@app.post("/api/confirm")
def confirm(body: ConfirmIn):
    return orchestrator.confirm_pending(body.session_id, body.approve)


@app.post("/api/kill")
def kill(body: KillIn):
    orchestrator.set_kill_switch(body.engaged)
    return {"kill_switch": orchestrator.kill_switch}


@app.post("/api/demo-mode")
def demo_mode(body: DemoModeIn):
    orchestrator.set_demo_mode(body.enabled)
    return {"demo_mode": orchestrator.demo_mode}


@app.get("/api/stats")
def stats():
    return orchestrator.stats()


@app.get("/api/catalog")
def get_catalog():
    return {"merchant": catalog.merchant, "products": catalog.products, "bundles": catalog.bundles}


@app.get("/api/merchants")
def merchants():
    return {"active": catalog.active, "merchants": catalog.merchants()}


@app.post("/api/merchant")
def set_merchant(body: MerchantIn):
    ok = catalog.set_active(body.merchant_id)
    if ok:
        audit.append("merchant_switch", {"merchant": body.merchant_id})
        hub.publish({"type": "merchant_switch", "merchant": catalog.merchant})
    return {"ok": ok, "active": catalog.active, "merchant": catalog.merchant}


# ---------- orders / refunds / failure ----------
@app.get("/api/orders")
def orders(limit: int = 100):
    return {"orders": db.all_orders(limit)}


@app.post("/api/orders/{order_id}/refund")
def refund(order_id: str):
    return orchestrator.refund_order(order_id)


@app.post("/api/arm-failure")
def arm_failure():
    orchestrator.arm_failure()
    return {"armed": True, "note": "next payment will fail to demo graceful handling"}


# ---------- audit ----------
@app.get("/api/audit")
def get_audit(limit: int = 200):
    return {"entries": audit.entries(limit), "verify": audit.verify()}


@app.get("/api/audit/verify")
def verify_audit():
    return audit.verify()


@app.get("/api/audit/export")
def export_audit(fmt: str = "json"):
    if fmt == "csv":
        return PlainTextResponse(audit.export_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=audit.csv"})
    return PlainTextResponse(audit.export_json(), media_type="application/json",
                             headers={"Content-Disposition": "attachment; filename=audit.json"})


# ---------- tracing ----------
@app.get("/api/traces")
def traces(limit: int = 20):
    return {"traces": tracer.recent(limit)}


@app.get("/api/traces/{trace_id}")
def trace_detail(trace_id: str):
    t = tracer.get(trace_id)
    return t or JSONResponse({"error": "not found"}, status_code=404)


# ---------- policy ----------
@app.get("/api/policy")
def get_policy():
    return policy_engine.config


@app.get("/api/policy/raw", response_class=PlainTextResponse)
def get_policy_raw():
    return policy_engine.path.read_text(encoding="utf-8")


@app.post("/api/policy")
def set_policy(body: PolicyIn):
    import yaml as _yaml
    try:
        _yaml.safe_load(body.yaml)
    except Exception as e:
        return JSONResponse({"error": f"invalid YAML: {e}"}, status_code=400)
    policy_engine.path.write_text(body.yaml, encoding="utf-8")
    policy_engine.reload(force=True)
    audit.append("policy_update", {"version": policy_engine.config.get("version")})
    hub.publish({"type": "policy_update", "version": policy_engine.config.get("version")})
    return {"ok": True, "version": policy_engine.config.get("version")}


@app.post("/api/policy/counterfactual")
def counterfactual(body: CounterfactualIn):
    amount = sum(i.get("qty", 1) * i.get("price", 0) for i in body.items)
    plan = {"items": body.items, "amount": amount}
    return policy_engine.counterfactual(plan, body.tier, body.remove_gate, body.session_id or "")


# ---------- receipts ----------
@app.post("/api/receipt/verify")
def verify_receipt(body: ReceiptIn):
    return receipts.verify(body.receipt)


# ---------- auth tokens ----------
@app.post("/api/token")
def make_token(body: TokenIn):
    tok = issue_token(tier=body.tier, max_amount=body.max_amount, ttl=body.ttl)
    return {"token": tok, "tier": body.tier, "max_amount": body.max_amount}


@app.get("/api/token/inspect")
def inspect_token(token: str):
    claims = verify_token(token)
    return {"valid": claims is not None, "claims": claims}


# ---------- adversarial self-test (#15) ----------
@app.post("/api/self-test")
def self_test():
    from .selftest import run_self_test
    result = run_self_test()
    audit.append("self_test", {"passed": result["passed"], "total": result["total"]})
    hub.publish({"type": "self_test", **result})
    return result


# ---------- seed / reset (#72) ----------
@app.post("/api/seed")
def seed():
    seed_demo()
    hub.publish({"type": "seeded", "stats": orchestrator.stats()})
    return {"ok": True, "stats": orchestrator.stats()}


# ---------- discovery (#19) ----------
@app.get("/.well-known/agent-commerce.json")
def well_known(request: Request):
    base = settings.public_url or str(request.base_url).rstrip("/")
    return {
        "protocol": "auxion-agent-commerce/0.1",
        "merchant": catalog.merchant,
        "capabilities": ["search", "quote", "order", "webhook_settlement", "signed_receipts",
                         "jwt_scope", "mcp"],
        "endpoints": {
            "chat": f"{base}/api/chat",
            "confirm": f"{base}/api/confirm",
            "catalog": f"{base}/api/catalog",
            "receipt_verify": f"{base}/api/receipt/verify",
            "token": f"{base}/api/token",
            "mcp": f"{base}/mcp/tools",
        },
        "auth": {"type": "jwt-scope", "note": "optional; unknown tier if absent"},
        "products_sample": [p["id"] for p in catalog.products[:5]],
    }


# ---------- MCP shim ----------
@app.get("/mcp/tools")
def mcp_tools():
    return list_tools()


@app.post("/mcp/call")
def mcp_call(body: McpCallIn):
    return call_tool(body.name, body.arguments)


# ---------- payment webhook (real Razorpay path) ----------
@app.post("/webhook/razorpay")
async def razorpay_webhook(request: Request):
    raw = await request.body()
    secret = settings.razorpay_webhook_secret
    sig = request.headers.get("x-razorpay-signature", "")
    if secret:
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return JSONResponse({"error": "bad signature"}, status_code=400)
    payload = json.loads(raw or b"{}")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    notes = entity.get("notes", {})
    order_id = payload.get("order_id") or notes.get("order_id")
    payment_ref = entity.get("id", "pay_webhook")
    if order_id and order_id in _PENDING_SETTLE:
        cb = _PENDING_SETTLE.pop(order_id)
        payment_engine.mark_paid_from_webhook(order_id, payment_ref, on_settled=cb)
        return {"ok": True}
    return {"ok": True, "note": "no matching pending order"}


@app.get("/pay/{order_id}", response_class=HTMLResponse)
def pay_page(order_id: str):
    order = payment_engine.get(order_id)
    if not order:
        return HTMLResponse("<h3>Unknown order</h3>", status_code=404)
    return HTMLResponse(
        f"<html><body style='font-family:sans-serif;padding:40px;background:#0b0e14;color:#e6ebf2'>"
        f"<h2>Auxion Payment (simulated)</h2><p>Order <b>{order_id}</b> — "
        f"₹{order['amount']:.0f} {order['currency']}</p><p>Status: <b>{order['status']}</b></p>"
        f"<p>In simulated mode settlement fires automatically. A real Razorpay payment link "
        f"would replace this page.</p></body></html>")


# ---------- websocket ----------
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    hub.clients.add(websocket)
    await websocket.send_json({"type": "snapshot", "stats": orchestrator.stats(),
                               "audit_verify": audit.verify()})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.clients.discard(websocket)
    except Exception:
        hub.clients.discard(websocket)


# ---------- pages ----------
@app.get("/", response_class=HTMLResponse)
def landing():
    p = STATIC_DIR / "landing.html"
    return HTMLResponse(p.read_text(encoding="utf-8")) if p.exists() else HTMLResponse("<h1>Auxion</h1>")


@app.get("/app", response_class=HTMLResponse)
def dashboard():
    p = STATIC_DIR / "index.html"
    return HTMLResponse(p.read_text(encoding="utf-8")) if p.exists() else HTMLResponse("<h1>Dashboard missing</h1>")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
