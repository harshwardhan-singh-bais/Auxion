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
from .orchestrator import orchestrator, _PENDING_SETTLE
from .llm import llm_chain
from .payments import payment_engine
from .policy import policy_engine
from .seed import seed_demo
from .tracing import tracer

db.init_db()

app = FastAPI(title="Auxion Agentic Commerce", version="2.1")


# ---------- optional admin auth ----------
@app.middleware("http")
async def _admin_auth(request: Request, call_next):
    tok = settings.admin_token
    if tok and request.url.path.startswith("/api/"):
        open_paths = ("/api/token", "/api/llm/status", "/api/stats")
        if not request.url.path.startswith(open_paths):
            if request.headers.get("x-auxion-token") != tok:
                return JSONResponse({"error": "unauthorized: set X-Auxion-Token header"}, status_code=401)
    return await call_next(request)


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


class PaymentModeIn(BaseModel):
    simulated: bool


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


class BundleAcceptIn(BaseModel):
    session_id: str | None = None
    tier: str = "unknown"
    token: str | None = None


class CartRemoveIn(BaseModel):
    session_id: str
    product_id: str


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


@app.post("/api/payment-mode")
def payment_mode(body: PaymentModeIn):
    payment_engine.set_forced_simulated(body.simulated)
    audit.append("payment_mode", {"mode": payment_engine.mode, "forced_simulated": body.simulated})
    hub.publish({"type": "payment_mode", "mode": payment_engine.mode})
    return {"payment_mode": payment_engine.mode}


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


@app.post("/api/cart/remove")
def cart_remove(body: CartRemoveIn):
    return orchestrator.remove_from_cart(body.session_id, body.product_id)


@app.post("/api/bundle/accept")
def bundle_accept(body: BundleAcceptIn):
    return orchestrator.accept_bundle(session_id=body.session_id, tier=body.tier, token=body.token)


# ---------- LLM provider chain ----------
@app.get("/api/llm/status")
def llm_status():
    return llm_chain.status()


@app.post("/api/llm/ping")
def llm_ping():
    return llm_chain.ping_all()


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


@app.get("/mcp/client.json")
def mcp_client_config():
    """Ready-to-paste client config pointing an external MCP-capable agent at this store."""
    base = settings.public_url.rstrip("/")
    return {
        "mcpServers": {
            "auxion-merchant": {
                "type": "http",
                "tools": f"{base}/mcp/tools",
                "call": f"{base}/mcp/call",
                "manifest": f"{base}/.well-known/agent-commerce.json",
            }
        },
        "note": "Copy this block into your MCP client config. All calls pass through the same policy-gated pipeline.",
    }


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


@app.post("/api/orders/{order_id}/simulate-payment")
def simulate_payment(order_id: str):
    """Demo helper: settle a real-mode order locally (no webhook needed)."""
    cb = _PENDING_SETTLE.get(order_id)
    payment_engine.simulate_payment(order_id, on_settled=cb, on_failed=None)
    return {"ok": True}


@app.get("/pay/{order_id}", response_class=HTMLResponse)
def pay_page(order_id: str):
    order = payment_engine.get(order_id)
    if not order:
        return HTMLResponse("<h3>Unknown order</h3>", status_code=404)
    real = payment_engine.mode == "razorpay_test"
    pay_btn = ""
    if real and order.get("provider_order_id"):
        key_id = settings.razorpay_key_id
        pay_btn = (
            '<script src="https://checkout.razorpay.com/v1/checkout.js"></' + 'script>'
            '<button class="rp" onclick="doPay()">Pay with Razorpay (test mode)</button>'
            '<span id="rpNote" style="margin-top:12px;font-size:12px;color:#8a97ab">'
            f"Settlement arrives via your Razorpay webhook ({settings.public_url}/webhook/razorpay). "
            "No webhook set up? Use the button below to settle the demo locally.</span>"
            "<script>"
            f"function doPay() {{ var rzp = new Razorpay({{ key: '{key_id}', order_id: '{order['provider_order_id']}', "
            f"amount: {int(order['amount'] * 100)}, currency: '{order['currency']}', name: 'Auxion Merchant', "
            f"description: 'Order {order_id} (test mode)', handler: function (res) {{"
            "document.getElementById('rpNote').textContent = 'Payment ' + res.razorpay_payment_id + ' captured. Waiting for webhook settlement...';}} }}); rzp.open(); }}"
            "</" + "script>"
        )
    return HTMLResponse(
        "<html><head><meta charset='utf-8'><title>Auxion Payment</title>"
        "<style>body{font-family:ui-monospace,Menlo,monospace;background:#0b0d12;color:#e9edf5;padding:56px}"
        ".box{max-width:440px;border:1px solid #232a3a;border-radius:8px;padding:28px;margin:0 auto}"
        "h2{font-size:16px;letter-spacing:2px;margin:0 0 14px}.k{color:#8291a8;font-size:11px}"
        ".v{font-size:22px;margin:2px 0 18px}.rp{background:#5b8cff;color:#fff;border:none;padding:12px 18px;"
        "border-radius:6px;font-family:inherit;font-size:13px;cursor:pointer;width:100%;margin-bottom:10px}"
        ".sim{background:transparent;border:1px solid #232a3a;color:#8291a8;padding:10px;border-radius:6px;"
        "font-family:inherit;font-size:12px;cursor:pointer;width:100%}</style></head>"
        "<body><div class='box'>"
        "<h2>AUXION · PAYMENT</h2>"
        f"<div class='k'>ORDER</div><div class='v'>{order_id}</div>"
        f"<div class='k'>AMOUNT</div><div class='v'>\u20b9{order['amount']:.0f} {order['currency']}</div>"
        f"<div class='k'>STATUS</div><div class='v' style='font-size:14px'>{order['status']}</div>"
        f"{pay_btn}"
        "<button class='sim' onclick='settle()'>Settle locally (demo)</button>"
        f"<script>async function settle() {{ await fetch('/api/orders/{order_id}/simulate-payment', {{method:'POST'}}); location.reload(); }}</" + "script>"
        "</div></body></html>"
    )


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
