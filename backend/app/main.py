"""Auxion merchant-agent API + live control-room dashboard.

Run: uvicorn app.main:app --reload  (from the backend/ directory)
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from .audit import audit
from .catalog import catalog
from .orchestrator import orchestrator, _PENDING_SETTLE
from .payments import payment_engine
from .policy import policy_engine
from . import receipts

app = FastAPI(title="Auxion Agentic Commerce", version="1.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ---------- live event fan-out to WebSocket clients ----------
class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, event: dict):
        # called from orchestrator (possibly a worker thread) -> schedule on loop
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(event), self.loop)

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


# ---------- request models ----------
class ChatIn(BaseModel):
    text: str
    session_id: str | None = None
    tier: str = "unknown"


class ConfirmIn(BaseModel):
    session_id: str
    approve: bool


class KillIn(BaseModel):
    engaged: bool


# ---------- core endpoints ----------
@app.post("/api/chat")
def chat(body: ChatIn):
    return orchestrator.handle_message(body.text, session_id=body.session_id, tier=body.tier)


@app.post("/api/confirm")
def confirm(body: ConfirmIn):
    return orchestrator.confirm_pending(body.session_id, body.approve)


@app.post("/api/kill")
def kill(body: KillIn):
    orchestrator.set_kill_switch(body.engaged)
    return {"kill_switch": orchestrator.kill_switch}


@app.get("/api/stats")
def stats():
    return orchestrator.stats()


@app.get("/api/catalog")
def get_catalog():
    return {"merchant": catalog.merchant, "products": catalog.products}


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


# ---------- policy ----------
@app.get("/api/policy")
def get_policy():
    return policy_engine.config


@app.get("/api/policy/raw", response_class=PlainTextResponse)
def get_policy_raw():
    return (Path(policy_engine.path)).read_text(encoding="utf-8")


class PolicyIn(BaseModel):
    yaml: str


@app.post("/api/policy")
def set_policy(body: PolicyIn):
    import yaml as _yaml

    # validate before writing (#13 safe hot-swap)
    try:
        _yaml.safe_load(body.yaml)
    except Exception as e:
        return JSONResponse({"error": f"invalid YAML: {e}"}, status_code=400)
    Path(policy_engine.path).write_text(body.yaml, encoding="utf-8")
    policy_engine.reload(force=True)
    audit.append("policy_update", {"version": policy_engine.config.get("version")})
    hub.publish({"type": "policy_update", "version": policy_engine.config.get("version")})
    return {"ok": True, "version": policy_engine.config.get("version")}


# ---------- receipts ----------
class ReceiptIn(BaseModel):
    receipt: dict


@app.post("/api/receipt/verify")
def verify_receipt(body: ReceiptIn):
    return receipts.verify(body.receipt)


# ---------- discoverable catalog for external agents (#19) ----------
@app.get("/.well-known/agent-commerce.json")
def well_known(request: Request):
    base = os.getenv("AUXION_PUBLIC_URL", str(request.base_url).rstrip("/"))
    return {
        "protocol": "auxion-agent-commerce/0.1",
        "merchant": catalog.merchant,
        "capabilities": ["search", "quote", "order", "webhook_settlement", "signed_receipts"],
        "endpoints": {
            "chat": f"{base}/api/chat",
            "confirm": f"{base}/api/confirm",
            "catalog": f"{base}/api/catalog",
            "receipt_verify": f"{base}/api/receipt/verify",
        },
        "auth": {"type": "jwt-scope", "note": "optional; unknown tier if absent"},
        "products_sample": [p["id"] for p in catalog.products[:5]],
    }


# ---------- payment webhook (real Razorpay path) ----------
@app.post("/webhook/razorpay")
async def razorpay_webhook(request: Request):
    import hmac, hashlib, json

    raw = await request.body()
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    sig = request.headers.get("x-razorpay-signature", "")
    if secret:
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return JSONResponse({"error": "bad signature"}, status_code=400)
    payload = json.loads(raw or b"{}")
    # Expect payload to map to one of our orders; demo-tolerant extraction.
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    notes = entity.get("notes", {})
    order_id = payload.get("order_id") or notes.get("order_id")
    payment_ref = entity.get("id", "pay_webhook")
    if order_id and order_id in _PENDING_SETTLE:
        cb = _PENDING_SETTLE.pop(order_id)
        payment_engine.mark_paid_from_webhook(order_id, payment_ref, on_settled=cb)
        return {"ok": True}
    return {"ok": True, "note": "no matching pending order"}


# simulated payment page (the 'link' opens here in simulated mode)
@app.get("/pay/{order_id}", response_class=HTMLResponse)
def pay_page(order_id: str):
    order = payment_engine.get(order_id)
    if not order:
        return HTMLResponse("<h3>Unknown order</h3>", status_code=404)
    return HTMLResponse(
        f"<html><body style='font-family:sans-serif;padding:40px'>"
        f"<h2>Auxion Payment (simulated)</h2><p>Order <b>{order_id}</b> — "
        f"₹{order['amount']:.0f} {order['currency']}</p>"
        f"<p>Status: <b>{order['status']}</b></p>"
        f"<p>In simulated mode the settlement fires automatically. This page is what a "
        f"real Razorpay payment link would replace.</p></body></html>"
    )


# ---------- websocket for live dashboard ----------
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    hub.clients.add(websocket)
    # send an initial snapshot
    await websocket.send_json({"type": "snapshot", "stats": orchestrator.stats(),
                               "audit_verify": audit.verify()})
    try:
        while True:
            await websocket.receive_text()  # keepalive; we don't expect client msgs
    except WebSocketDisconnect:
        hub.clients.discard(websocket)
    except Exception:
        hub.clients.discard(websocket)


# ---------- dashboard ----------
@app.get("/", response_class=HTMLResponse)
def index():
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Auxion</h1><p>Dashboard not built.</p>")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
