"""Append-only, hash-chained audit log (#1) with export (#70).

Each entry stores the hash of the previous entry, so any tampering with an
earlier record breaks the chain and is detectable via verify().
Persisted as JSONL so it survives restarts and is trivially exportable.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Optional

AUDIT_PATH = Path(__file__).resolve().parent.parent / "data" / "audit.jsonl"

GENESIS = "0" * 64


def _hash_entry(entry: dict) -> str:
    # Hash a canonical form EXCLUDING the entry's own hash field.
    payload = {k: v for k, v in entry.items() if k != "hash"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


class AuditLog:
    def __init__(self, path: Path = AUDIT_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._entries.append(json.loads(line))

    @property
    def last_hash(self) -> str:
        return self._entries[-1]["hash"] if self._entries else GENESIS

    def append(self, event_type: str, data: dict[str, Any], session_id: Optional[str] = None) -> dict:
        with self._lock:
            entry = {
                "seq": len(self._entries),
                "ts": time.time(),
                "session_id": session_id,
                "event_type": event_type,
                "data": data,
                "prev_hash": self.last_hash,
            }
            entry["hash"] = _hash_entry(entry)
            self._entries.append(entry)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            return entry

    def entries(self, limit: Optional[int] = None) -> list[dict]:
        if limit is None:
            return list(self._entries)
        return self._entries[-limit:]

    def verify(self) -> dict:
        """Walk the chain; report first break if any (proves integrity live)."""
        prev = GENESIS
        for e in self._entries:
            if e.get("prev_hash") != prev:
                return {"valid": False, "broken_at": e["seq"], "reason": "prev_hash mismatch"}
            if _hash_entry(e) != e.get("hash"):
                return {"valid": False, "broken_at": e["seq"], "reason": "hash mismatch"}
            prev = e["hash"]
        return {"valid": True, "count": len(self._entries), "head": prev}

    def export_json(self) -> str:
        return json.dumps(self._entries, indent=2)

    def export_csv(self) -> str:
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["seq", "ts", "session_id", "event_type", "data", "prev_hash", "hash"])
        for e in self._entries:
            w.writerow([
                e["seq"], e["ts"], e.get("session_id"), e["event_type"],
                json.dumps(e["data"], separators=(",", ":")), e["prev_hash"], e["hash"],
            ])
        return buf.getvalue()


audit = AuditLog()
