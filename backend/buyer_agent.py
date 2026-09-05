"""Independent AI buyer agent (#18) — the split-screen "agent-to-agent" demo.

A SEPARATE process from the merchant. Given a goal it:
  1. Discovers the merchant via /.well-known/agent-commerce.json
  2. Optionally mints/uses a scoped JWT (--token) to raise its trust tier
  3. Searches, picks a product within budget, adds to cart
  4. Places the order, then polls the order endpoint for settlement
  5. Prints its own reasoning log the whole way — zero humans

Usage (merchant must be running on :8000):
  python buyer_agent.py "buy a blue t-shirt under 800"
  python buyer_agent.py "get me white sneakers" --tier trusted --budget 3000
  python buyer_agent.py "buy earbuds" --token <jwt>
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import httpx

C = {"g": "\033[92m", "y": "\033[93m", "c": "\033[96m", "r": "\033[91m",
     "b": "\033[1m", "d": "\033[90m", "x": "\033[0m"}


def log(tag, msg, color="c"):
    print(f"{C[color]}{C['b']}[{tag}]{C['x']} {msg}"); sys.stdout.flush()


def find_settlement(base, order_id, timeout=15.0):
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(0.5)
        try:
            orders = httpx.get(f"{base}/api/orders?limit=50", timeout=10).json().get("orders", [])
            for o in orders:
                if o["order_id"] == order_id:
                    if o["status"] == "settled":
                        return o
                    if o["status"] == "failed":
                        return o
        except Exception:
            pass
    return None


def run(goal, base, tier, budget, token):
    log("BUYER", f"Goal: {goal!r}  (tier={tier}{' +token' if token else ''})", "y")

    log("DISCOVER", f"Fetching {base}/.well-known/agent-commerce.json")
    disco = httpx.get(f"{base}/.well-known/agent-commerce.json", timeout=15).json()
    log("DISCOVER", f"Merchant '{disco['merchant']['name']}' · caps {disco['capabilities']}", "g")
    chat_url = disco["endpoints"]["chat"]

    # optional: mint a scoped token to lift tier
    if token == "AUTO":
        tok = httpx.post(f"{base}/api/token", json={"tier": tier, "max_amount": budget or 15000}).json()
        token = tok["token"]
        log("AUTH", f"Minted scoped token (tier={tier}, max=₹{budget or 15000}).", "d")

    session_id = None

    def say(text):
        return httpx.post(chat_url, json={"text": text, "session_id": session_id,
                                          "tier": tier, "token": token}, timeout=30).json()

    log("REASON", "Searching the merchant catalog for a match to my goal.")
    r = say(goal); session_id = r["session_id"]
    products = r.get("products", [])

    if products:
        log("OBSERVE", "Candidates: " + ", ".join(f"{p['title']} (₹{p['price']})" for p in products))
        pick = next((p for p in products if budget is None or p["price"] <= budget), None)
        if not pick:
            log("BUYER", f"Nothing within budget ₹{budget}. Walking away.", "r"); return
        log("DECIDE", f"Choosing {pick['title']} at ₹{pick['price']}.", "g")
        r = say(f"buy {pick['title']}"); session_id = r["session_id"]
        log("MERCHANT", r.get("reply", ""))
    elif r.get("cart"):
        log("REASON", "Merchant matched and carted an item directly.", "g")
        log("MERCHANT", r.get("reply", ""))
    else:
        log("BUYER", f"No match. Merchant said: {r.get('reply')}", "r"); return

    log("REASON", "Placing the order and awaiting settlement confirmation.")
    r = say("checkout"); log("MERCHANT", r.get("reply", ""))

    if r.get("needs_confirmation"):
        log("BUYER", "Merchant requires human approval — halting, as designed.", "y"); return
    if r.get("decision", {}).get("decision") == "deny":
        log("BUYER", "Order denied by merchant policy. Respecting the gate.", "r"); return

    order = r.get("order", {}); order_id = order.get("order_id")
    if not order_id:
        log("BUYER", "No order created.", "r"); return

    log("WAIT", f"Order {order_id} placed. Polling for settlement webhook…", "y")
    final = find_settlement(base, order_id)
    if final and final["status"] == "settled":
        rc = final.get("receipt", {})
        rid = rc.get("body", {}).get("receipt_id", "n/a") if rc else "n/a"
        log("VERIFY", f"Settled. Signed receipt {rid}.", "g")
        if rc:
            v = httpx.post(f"{base}/api/receipt/verify", json={"receipt": rc}, timeout=10).json()
            log("VERIFY", f"Receipt signature valid: {v['valid']}.", "g" if v["valid"] else "r")
        log("DONE", "✅ Transaction complete end-to-end. Zero humans involved.", "g")
    elif final and final["status"] == "failed":
        log("DONE", f"Payment failed gracefully ({final.get('meta',{})}). No charge captured; cart preserved.", "y")
    else:
        log("DONE", "Settlement still pending after timeout.", "y")


def main():
    ap = argparse.ArgumentParser(description="Auxion independent AI buyer agent")
    ap.add_argument("goal")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--tier", default="unknown", choices=["unknown", "trusted", "premium"])
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--token", default=None, help="scoped JWT, or 'AUTO' to mint one for --tier")
    args = ap.parse_args()

    budget = args.budget
    if budget is None:
        m = re.search(r"(?:under|below|less than|<|upto|up to|max|budget)\s*₹?\s*(\d+)", args.goal, re.I)
        if m:
            budget = float(m.group(1))

    try:
        run(args.goal, args.url.rstrip("/"), args.tier, budget, args.token)
    except httpx.ConnectError:
        log("ERROR", f"Could not reach merchant at {args.url}. Is the backend running?", "r")


if __name__ == "__main__":
    main()
