"""Independent AI buyer agent (#18).

A SEPARATE process from the merchant. Given a goal, it:
  1. Discovers the merchant via /.well-known/agent-commerce.json
  2. Searches, picks a product within budget
  3. Places an order and waits for settlement
  4. Prints its own reasoning log the whole way

Run (merchant must be up on :8000):
  python buyer_agent.py "buy a blue t-shirt under 800"
  python buyer_agent.py "get me white sneakers" --tier trusted --url http://localhost:8000

No human in the loop. This is the right half of the split-screen demo.
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx

C = {"g": "\033[92m", "y": "\033[93m", "c": "\033[96m", "r": "\033[91m", "b": "\033[1m", "x": "\033[0m"}


def log(tag: str, msg: str, color: str = "c"):
    print(f"{C[color]}{C['b']}[{tag}]{C['x']} {msg}")
    sys.stdout.flush()


def run(goal: str, base_url: str, tier: str, budget: float | None):
    log("BUYER", f"Goal: {goal!r}  (tier={tier})", "y")

    # 1) discovery
    log("DISCOVER", f"Fetching {base_url}/.well-known/agent-commerce.json")
    disco = httpx.get(f"{base_url}/.well-known/agent-commerce.json", timeout=15).json()
    log("DISCOVER", f"Found merchant '{disco['merchant'].get('name')}' with capabilities {disco['capabilities']}", "g")
    chat_url = disco["endpoints"]["chat"]

    session_id = None

    # 2) search
    log("REASON", "I'll search the merchant catalog for something matching my goal.")
    r = httpx.post(chat_url, json={"text": goal, "session_id": session_id, "tier": tier}, timeout=30).json()
    session_id = r["session_id"]
    products = r.get("products", [])
    if not products:
        # maybe the planner already interpreted it as an 'add'; check cart
        if r.get("cart"):
            log("REASON", "Merchant already added a matching item to the cart.", "g")
        else:
            log("BUYER", f"No products matched. Merchant said: {r.get('reply')}", "r")
            return
    else:
        log("OBSERVE", "Candidates: " + ", ".join(f"{p['title']} (₹{p['price']})" for p in products))
        pick = None
        for p in products:
            if budget is None or p["price"] <= budget:
                pick = p
                break
        if not pick:
            log("BUYER", f"Nothing within budget ₹{budget}.", "r")
            return
        log("DECIDE", f"Choosing {pick['title']} at ₹{pick['price']}.", "g")

        # 3) add to cart
        r = httpx.post(chat_url, json={"text": f"buy {pick['title']}", "session_id": session_id, "tier": tier}, timeout=30).json()
        log("MERCHANT", r.get("reply", ""))

    # 4) checkout
    log("REASON", "Placing the order and waiting for settlement confirmation.")
    r = httpx.post(chat_url, json={"text": "checkout", "session_id": session_id, "tier": tier}, timeout=30).json()
    log("MERCHANT", r.get("reply", ""))

    if r.get("needs_confirmation"):
        log("BUYER", "Merchant requires human approval for this order. Halting (as designed).", "y")
        return
    if r.get("decision", {}).get("decision") == "deny":
        log("BUYER", "Order was denied by merchant policy. Respecting that.", "r")
        return

    order = r.get("order", {})
    order_id = order.get("order_id")
    if not order_id:
        log("BUYER", "No order created.", "r")
        return

    # 5) poll settlement via well-known? We poll the pay page/stats indirectly by re-checking.
    log("WAIT", f"Order {order_id} placed. Awaiting settlement webhook…", "y")
    settled = False
    for _ in range(20):
        time.sleep(0.5)
        # merchant exposes order state via the pay page; simplest is a stats poll
        stats = httpx.get(f"{base_url}/api/stats", timeout=10).json()
        if stats.get("orders_settled", 0) >= 1:
            settled = True
            break
    if settled:
        log("DONE", f"✅ Settlement confirmed. Merchant revenue now ₹{stats['revenue']:.0f}. Transaction complete, zero humans involved.", "g")
    else:
        log("DONE", "Order placed; settlement still pending.", "y")


def main():
    ap = argparse.ArgumentParser(description="Auxion independent AI buyer agent")
    ap.add_argument("goal", help='e.g. "buy a blue t-shirt under 800"')
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--tier", default="unknown", choices=["unknown", "trusted", "premium"])
    ap.add_argument("--budget", type=float, default=None)
    args = ap.parse_args()

    budget = args.budget
    if budget is None:
        import re
        m = re.search(r"(?:under|below|less than|<|upto|up to|max)\s*₹?\s*(\d+)", args.goal, re.I)
        if m:
            budget = float(m.group(1))

    try:
        run(args.goal, args.url.rstrip("/"), args.tier, budget)
    except httpx.ConnectError:
        log("ERROR", f"Could not reach merchant at {args.url}. Is the backend running?", "r")


if __name__ == "__main__":
    main()
