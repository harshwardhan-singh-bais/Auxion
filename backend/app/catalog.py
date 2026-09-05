"""Catalog loading, search, and cross-sell lookup.

Config-driven (#69): swap config/catalog.json to run a different store on the same engine.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

CATALOG_PATH = Path(__file__).resolve().parent.parent / "config" / "catalog.json"


class Catalog:
    def __init__(self, path: Path = CATALOG_PATH):
        self.path = path
        self._data: dict = {}
        self.reload()

    def reload(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            self._data = json.load(f)
        self._by_id = {p["id"]: p for p in self._data.get("products", [])}

    @property
    def merchant(self) -> dict:
        return self._data.get("merchant", {})

    @property
    def products(self) -> list[dict]:
        return self._data.get("products", [])

    def get(self, product_id: str) -> Optional[dict]:
        return self._by_id.get(product_id)

    def search(self, query: str, max_price: Optional[float] = None) -> list[dict]:
        """Simple token-overlap search over title/tags/category, price-filtered."""
        q = (query or "").lower()
        tokens = {t for t in q.replace(",", " ").split() if t}
        scored: list[tuple[int, dict]] = []
        for p in self.products:
            if max_price is not None and p["price"] > max_price:
                continue
            haystack = " ".join(
                [p["title"], p["category"], " ".join(p.get("tags", []))]
            ).lower()
            hay_tokens = set(haystack.split())
            score = len(tokens & hay_tokens)
            # substring boost for multi-word tags like "t-shirt"
            for t in tokens:
                if t in haystack:
                    score += 1
            if score > 0 or not tokens:
                scored.append((score, p))
        scored.sort(key=lambda x: (-x[0], x[1]["price"]))
        return [p for _, p in scored]

    def cross_sell(self, product_id: str) -> list[dict]:
        """Co-purchase recommendations (#53)."""
        ids = self._data.get("cross_sell", {}).get(product_id, [])
        return [self._by_id[i] for i in ids if i in self._by_id]


catalog = Catalog()
