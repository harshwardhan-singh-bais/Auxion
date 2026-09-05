"""Planner: natural language -> structured action proposal.

Uses Claude via tool-calling if ANTHROPIC_API_KEY is set; otherwise falls back
to a deterministic keyword parser so the whole system runs with zero API keys.

CRITICAL DESIGN RULE: the planner only PROPOSES structured intent. It never
executes, never moves money, never bypasses the policy engine. Its output is
data that the Critic (policy) validates and the Executor acts on.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from .catalog import catalog

# Intents the planner can emit.
# search  -> find products
# add     -> propose a purchase plan (items)
# checkout-> finalize current cart into an order
# help    -> fallback / smalltalk

PRICE_RE = re.compile(r"(?:under|below|less than|<|upto|up to|max)\s*₹?\s*(\d+)", re.I)
QTY_RE = re.compile(r"(\d+)\s*(?:x|units?|pcs?|pieces?)?\s+", re.I)


def _extract_max_price(text: str) -> Optional[float]:
    m = PRICE_RE.search(text)
    return float(m.group(1)) if m else None


def _deterministic_plan(text: str) -> dict:
    t = text.lower().strip()
    max_price = _extract_max_price(t)

    if any(w in t for w in ["checkout", "buy it", "confirm", "pay now", "place order", "purchase now"]):
        return {"intent": "checkout", "reasoning": "User asked to finalize the order."}

    if any(w in t for w in ["show", "find", "search", "looking for", "do you have", "list", "browse"]):
        results = catalog.search(t, max_price)
        return {
            "intent": "search",
            "query": t,
            "max_price": max_price,
            "results": [p["id"] for p in results[:5]],
            "reasoning": f"Search over catalog (max_price={max_price}).",
        }

    # Try to resolve a product to add/buy.
    if any(w in t for w in ["buy", "add", "get me", "want", "order", "i'll take", "purchase"]):
        results = catalog.search(t, max_price)
        if results:
            prod = results[0]
            qty = 1
            m = QTY_RE.search(t)
            if m:
                qty = max(1, int(m.group(1)))
            return {
                "intent": "add",
                "items": [{"product_id": prod["id"], "qty": qty, "price": prod["price"]}],
                "reasoning": f"Matched '{prod['title']}' to the request; proposing qty {qty}.",
            }
        return {"intent": "help", "reasoning": "Wanted to buy but no product matched."}

    # Default: treat as a search to be helpful.
    results = catalog.search(t, max_price)
    if results:
        return {
            "intent": "search",
            "query": t,
            "max_price": max_price,
            "results": [p["id"] for p in results[:5]],
            "reasoning": "Interpreted as a product search.",
        }
    return {"intent": "help", "reasoning": "Could not parse a shopping intent."}


def _llm_plan(text: str) -> Optional[dict]:
    """Use Claude tool-calling to extract structured intent. Returns None on any failure."""
    try:
        import json

        import httpx

        api_key = os.environ["ANTHROPIC_API_KEY"]
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

        product_lines = "\n".join(
            f"- {p['id']}: {p['title']} (₹{p['price']}, {p['category']}, stock {p['stock']})"
            for p in catalog.products
        )
        tool = {
            "name": "propose_action",
            "description": "Propose a structured shopping action. You never execute; you only propose.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": ["search", "add", "checkout", "help"]},
                    "query": {"type": "string"},
                    "max_price": {"type": ["number", "null"]},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string"},
                                "qty": {"type": "integer"},
                                "price": {"type": "number"},
                            },
                            "required": ["product_id", "qty", "price"],
                        },
                    },
                    "reasoning": {"type": "string"},
                },
                "required": ["intent", "reasoning"],
            },
        }
        system = (
            "You are the Planner for a merchant shopping agent. Convert the user message "
            "into ONE structured action using the propose_action tool. Only reference product_ids "
            "from this catalog:\n" + product_lines +
            "\nRules: for 'add', include items with exact product_id and current price. "
            "For 'checkout', emit intent=checkout. Never invent products or prices."
        )
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "system": system,
                "tools": [tool],
                "tool_choice": {"type": "tool", "name": "propose_action"},
                "messages": [{"role": "user", "content": text}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "propose_action":
                plan = block["input"]
                plan["_source"] = "llm"
                return plan
        return None
    except Exception:
        return None


def plan(text: str) -> dict:
    """Public entrypoint. Try LLM, fall back to deterministic parser."""
    if os.getenv("ANTHROPIC_API_KEY"):
        result = _llm_plan(text)
        if result is not None:
            return result
    p = _deterministic_plan(text)
    p["_source"] = "deterministic"
    return p
