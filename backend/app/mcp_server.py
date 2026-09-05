"""MCP-style tool interface (#protocol-race flex).

Exposes the merchant's catalog + commerce actions as MCP tool definitions and a
JSON-RPC-ish invoke endpoint, so ANY MCP-compatible agent — not just our buyer —
could shop here. Mounted by main.py under /mcp.

This is a pragmatic HTTP shim implementing the MCP tools/list + tools/call shape;
it reuses the exact same orchestrator path as everything else (no bypass).
"""
from __future__ import annotations

from typing import Any

from .catalog import catalog
from .orchestrator import orchestrator

TOOLS = [
    {
        "name": "search_products",
        "description": "Search the merchant catalog. Returns matching products with prices.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "max_price": {"type": "number"}},
            "required": ["query"]},
    },
    {
        "name": "start_session",
        "description": "Begin a shopping session. Optionally pass a scoped JWT and tier.",
        "inputSchema": {"type": "object", "properties": {
            "tier": {"type": "string"}, "token": {"type": "string"}}},
    },
    {
        "name": "add_to_cart",
        "description": "Add a product to the cart by natural-language request within a session.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["session_id", "text"]},
    },
    {
        "name": "checkout",
        "description": "Place the order for the current cart. Subject to policy gating.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"}}, "required": ["session_id"]},
    },
]


def list_tools() -> dict:
    return {"tools": TOOLS, "merchant": catalog.merchant}


def call_tool(name: str, arguments: dict[str, Any]) -> dict:
    args = arguments or {}
    if name == "search_products":
        q = args.get("query", "")
        mp = args.get("max_price")
        results = catalog.search(q, mp)
        return {"content": [{"type": "json", "json": {"products": results[:6]}}]}
    if name == "start_session":
        sess = orchestrator.get_session(tier=args.get("tier", "unknown"), token=args.get("token"))
        return {"content": [{"type": "json", "json": {"session_id": sess.session_id, "tier": sess.tier}}]}
    if name == "add_to_cart":
        r = orchestrator.handle_message(args.get("text", ""), session_id=args.get("session_id"))
        return {"content": [{"type": "json", "json": r}]}
    if name == "checkout":
        r = orchestrator.handle_message("checkout", session_id=args.get("session_id"))
        return {"content": [{"type": "json", "json": r}]}
    return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name}"}]}
