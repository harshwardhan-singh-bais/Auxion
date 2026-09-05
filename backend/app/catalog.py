"""Catalog: multi-merchant, config-driven (#69), with cross-sell + bundles (#53).

Swap config/catalog.json or set AUXION_MERCHANT to run a different store on the
same engine. Reloads from disk so edits appear live.
"""
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Optional

from .config import CONFIG_DIR, settings

CATALOG_PATH = CONFIG_DIR / "catalog.json"


class Catalog:
    def __init__(self, path: Path = CATALOG_PATH):
        self.path = path
        self._data: dict = {}
        self._mtime = 0.0
        self._lock = Lock()
        self.active = settings.active_merchant
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        mtime = self.path.stat().st_mtime
        if force or mtime != self._mtime:
            with self._lock:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                self._mtime = mtime
                self._reindex()

    def _reindex(self) -> None:
        m = self._data["merchants"].get(self.active)
        if not m:
            # fall back to first merchant
            self.active = next(iter(self._data["merchants"]))
            m = self._data["merchants"][self.active]
        self._m = m
        self._by_id = {p["id"]: p for p in m.get("products", [])}

    # ---- merchant switching ----
    def merchants(self) -> list[dict]:
        return [v["merchant"] for v in self._data["merchants"].values()]

    def set_active(self, merchant_id: str) -> bool:
        if merchant_id in self._data["merchants"]:
            self.active = merchant_id
            self._reindex()
            return True
        return False

    @property
    def merchant(self) -> dict:
        self.reload()
        return self._m["merchant"]

    @property
    def products(self) -> list[dict]:
        self.reload()
        return self._m.get("products", [])

    @property
    def bundles(self) -> list[dict]:
        self.reload()
        return self._m.get("bundles", [])

    def get(self, product_id: str) -> Optional[dict]:
        self.reload()
        return self._by_id.get(product_id)

    def bundle(self, bundle_id: str) -> Optional[dict]:
        for b in self.bundles:
            if b["id"] == bundle_id:
                return b
        return None

    def search(self, query: str, max_price: Optional[float] = None) -> list[dict]:
        q = (query or "").lower()
        tokens = {t.strip("-") for t in q.replace(",", " ").replace("-", " ").split() if t}
        scored: list[tuple[int, dict]] = []
        for p in self.products:
            if max_price is not None and p["price"] > max_price:
                continue
            haystack = " ".join([p["title"], p["category"], " ".join(p.get("tags", []))]).lower()
            hay_tokens = set(haystack.replace("-", " ").split())
            score = len(tokens & hay_tokens)
            for t in tokens:
                if t and t in haystack:
                    score += 1
            if score > 0:
                scored.append((score, p))
        scored.sort(key=lambda x: (-x[0], x[1]["price"]))
        return [p for _, p in scored]

    def cross_sell(self, product_id: str) -> list[dict]:
        self.reload()
        ids = self._m.get("cross_sell", {}).get(product_id, [])
        return [self._by_id[i] for i in ids if i in self._by_id]


catalog = Catalog()
