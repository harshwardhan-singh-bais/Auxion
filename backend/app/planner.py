"""Planner: natural language -> structured action proposal.

Claude tool-calling when ANTHROPIC_API_KEY is set; deterministic keyword parser
otherwise, so the whole system runs with zero API keys.

The planner ONLY proposes structured intent. It never executes, never moves
money, never bypasses the policy engine. Output is data the Critic validates.
"""
from __future__ import annotations

import re
from typing import Optional

from .catalog import catalog
from .config import settings

PRICE_RE = re.compile(r"(?:under|below|less than|<|upto|up to|max|budget)\s*₹?\s*(\d+)", re.I)
QTY_RE = re.compile(r"(\d+)\s*(?:x|units?|pcs?|pieces?)\b", re.I)

INTENTS = ("search", "add", "remove", "checkout", "recommend", "help")


def _extract_max_price(text: str) -> Optional[float]:
    m = PRICE_RE.search(text)
    return float(m.group(1)) if m else None


def _extract_qty(text: str) -> int:
    # Remove any price phrase ("under 800", "max 1500") so its number is never
    # mistaken for a quantity.
    cleaned = PRICE_RE.sub(" ", text)
    # explicit unit form: "3x", "2 units"
    m = QTY_RE.search(cleaned)
    if m:
        return max(1, int(m.group(1)))
    # bare leading quantity: "buy 3 slim fit jeans", "3 t-shirts"
    m = re.search(r"\b(\d+)\s+[a-z]", cleaned)
    if m:
        return max(1, int(m.group(1)))
    words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    for w, n in words.items():
        if re.search(rf"\b{w}\b", cleaned):
            return n
    return 1


def _deterministic_plan(text: str) -> dict:
    t = text.lower().strip()
    max_price = _extract_max_price(t)

    if any(w in t for w in ["checkout", "buy it", "confirm order", "pay now", "place order", "check out"]):
        return {"intent": "checkout", "reasoning": "User asked to finalize the order."}

    if any(w in t for w in ["recommend", "suggest", "what goes with", "add-on", "goes well"]):
        results = catalog.search(t, max_price)
        base = results[0]["id"] if results else None
        return {"intent": "recommend", "product_id": base, "reasoning": "User asked for recommendations."}

    if any(w in t for w in ["remove", "delete", "drop"]):
        results = catalog.search(t, None)
        if results:
            return {"intent": "remove", "product_id": results[0]["id"], "reasoning": "Remove item from cart."}

    if any(w in t for w in ["show", "find", "search", "looking for", "do you have", "list", "browse", "any"]):
        results = catalog.search(t, max_price)
        return {"intent": "search", "query": t, "max_price": max_price,
                "results": [p["id"] for p in results[:6]],
                "reasoning": f"Search over catalog (max_price={max_price})."}

    if any(w in t for w in ["buy", "add", "get me", "i want", "order", "i'll take", "purchase", "grab"]):
        results = catalog.search(t, max_price)
        if results:
            prod = results[0]
            return {"intent": "add",
                    "items": [{"product_id": prod["id"], "qty": _extract_qty(t), "price": prod["price"]}],
                    "reasoning": f"Matched '{prod['title']}'; proposing qty {_extract_qty(t)}."}
        return {"intent": "help", "reasoning": "Wanted to buy but no product matched."}

    results = catalog.search(t, max_price)
    if results:
        return {"intent": "search", "query": t, "max_price": max_price,
                "results": [p["id"] for p in results[:6]], "reasoning": "Interpreted as a product search."}
    return {"intent": "help", "reasoning": "Could not parse a shopping intent."}


def _llm_plan(text: str) -> Optional[dict]:
    try:
        import httpx

        product_lines = "\n".join(
            f"- {p['id']}: {p['title']} (₹{p['price']}, {p['category']}, stock {p['stock']})"
            for p in catalog.products
        )
        tool = {
            "name": "propose_action",
            "description": "Propose ONE structured shopping action. You never execute; you only propose.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": list(INTENTS)},
                    "query": {"type": "string"},
                    "max_price": {"type": ["number", "null"]},
                    "product_id": {"type": ["string", "null"]},
                    "items": {"type": "array", "items": {"type": "object", "properties": {
                        "product_id": {"type": "string"}, "qty": {"type": "integer"}, "price": {"type": "number"}},
                        "required": ["product_id", "qty", "price"]}},
                    "reasoning": {"type": "string"},
                },
                "required": ["intent", "reasoning"],
            },
        }
        system = (
            "You are the Planner for a merchant shopping agent. Convert the user message into ONE "
            "structured action via propose_action. Only reference product_ids from this catalog:\n"
            + product_lines +
            "\nFor 'add' include items with exact product_id and current price. For 'checkout' emit "
            "intent=checkout. Never invent products or prices."
        )
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": settings.anthropic_model, "max_tokens": 1024, "system": system,
                  "tools": [tool], "tool_choice": {"type": "tool", "name": "propose_action"},
                  "messages": [{"role": "user", "content": text}]},
            timeout=30,
        )
        resp.raise_for_status()
        for block in resp.json().get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "propose_action":
                plan = block["input"]
                plan["_source"] = "llm"
                return plan
        return None
    except Exception:
        return None


def plan(text: str) -> dict:
    if settings.has_llm:
        result = _llm_plan(text)
        if result is not None:
            return result
    p = _deterministic_plan(text)
    p["_source"] = "deterministic"
    return p
