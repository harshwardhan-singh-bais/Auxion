# Auxion — Agentic Commerce Control Room

A merchant AI agent that shops on behalf of customers **and** other AI agents, with a
deterministic safety spine between the LLM and any money movement. Every proposed action
flows **Planner → Critic/Policy → Executor**, is risk-scored, policy-gated, hash-chained
into an append-only audit log, and settled with a signed, verifiable receipt.

Runs with **zero API keys** (deterministic planner + simulated payments). Add keys to
upgrade to real Claude reasoning and Razorpay test-mode payments.

## What's built (the spine — Tier 1 + key differentiators)

- **Planner → Critic → Executor pipeline** (`orchestrator.py`) — LLM only *proposes*; deterministic code decides and executes.
- **Hot-swappable YAML policy engine** with **trust tiers** and **risk scoring** (`policy.py`, `config/policy.yaml`).
- **Hash-chained append-only audit log** with tamper detection + CSV/JSON export (`audit.py`).
- **Signed, verifiable receipts** via HMAC (`receipts.py`).
- **Payment lifecycle state machine**: order → link → webhook → settlement (`payments.py`), simulated or real Razorpay.
- **Discoverable catalog** at `/.well-known/agent-commerce.json` (#19).
- **Live control-room dashboard** — conversation, animated decision timeline, risk gauge, revenue, audit feed, policy editor, kill switch (`static/index.html`).
- **Independent AI buyer agent** CLI (`buyer_agent.py`) — the split-screen "agent-to-agent" demo.

## Run it

```bash
cd backend
# Windows:
run.bat
# macOS/Linux / Git Bash:
bash run.sh
```

Then open http://localhost:8000

First run creates a virtualenv and installs deps automatically.

### Manual start
```bash
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # mac/linux
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

## Demo script (run this exactly)

1. Open http://localhost:8000 — dashboard, empty state.
2. In the conversation panel type: `find a blue t-shirt under 800` → results + cross-sell appear.
3. Type `buy a blue t-shirt` → cart updates, upsell suggested.
4. Type `checkout` → watch the **Planner → Critic → Executor** timeline light up, risk gauge fill, order settle, revenue tick up, signed receipt logged.
5. Open a terminal split-screen and run the **independent buyer agent**:
   ```bash
   cd backend
   ./.venv/Scripts/python.exe buyer_agent.py "buy white sneakers under 3000" --tier trusted
   ```
   Left = human chatting with merchant; right = autonomous AI buyer doing the full loop, zero humans.
6. Show the **Policy** tab: lower `unknown.max_transaction_amount` to `500`, Save & Reload, then try to buy jeans → **denied live**.
7. Show the **Audit** tab: hash-chain "chain valid" badge, export CSV.
8. Press the big red **KILL SWITCH** → all actions halt instantly.

## Verify the spine without a browser

```bash
cd backend
./.venv/Scripts/python.exe smoke_test.py
```

## Upgrade paths (optional)

Copy `.env.example` → `.env` and fill in:
- `ANTHROPIC_API_KEY` → Planner uses Claude tool-calling for intent extraction.
- `RAZORPAY_KEY_ID` / `_SECRET` / `_WEBHOOK_SECRET` → real test-mode orders; point Razorpay webhook at `/(ngrok)/webhook/razorpay`.
- `AUXION_SIGNING_KEY` → stable receipt signature verification across runs.
- `AUXION_PUBLIC_URL` → your ngrok URL for webhook delivery.

## Architecture

```
User / AI buyer
      │  natural language
      ▼
 ┌─────────┐   proposes    ┌─────────┐   validates   ┌──────────┐
 │ PLANNER │ ────────────▶ │ CRITIC  │ ────────────▶ │ EXECUTOR │
 │ (LLM /  │  structured   │ policy  │  allow/deny/  │ payments │
 │  rules) │   intent      │ + risk  │   confirm     │  (money) │
 └─────────┘               └─────────┘               └──────────┘
      │                         │                         │
      └─────────── every step ──┴──── append-only ────────┘
                       hash-chained audit log
                       + signed receipts + live WS dashboard
```

The LLM never touches money. Policy and execution are deterministic Python.

## Feature-tier mapping

Spine implemented here: Tier 1 (#1 audit, kill switch, timeline, gauges, revenue),
Tier 2 (#12 signed receipts, #13 hot-swap policy), Tier 3 (#18 buyer agent, #19 discovery),
Tier 5 (#35 Planner/Critic/Executor), Tier 9 (#53 cross-sell, #56 settlement loop),
Tier 11 (#65 trust tiers), Tier 12 (#69 config-driven store, #70 export, #71 dark/light).
Remaining tiers slot onto this spine.
