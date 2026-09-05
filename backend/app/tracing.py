"""Lightweight span tracing rendered as a waterfall in the UI.

Each handled message opens a trace; each pipeline stage is a span with start/end.
The dashboard draws these as a flame/waterfall graph so judges see Planner vs
Policy vs Executor latency at a glance.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from threading import Lock
from typing import Optional


class Trace:
    def __init__(self, trace_id: str, name: str, session_id: Optional[str]):
        self.trace_id = trace_id
        self.name = name
        self.session_id = session_id
        self.start = time.time()
        self.end: Optional[float] = None
        self.spans: list[dict] = []

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "session_id": self.session_id,
            "start": self.start,
            "end": self.end,
            "duration_ms": round(((self.end or time.time()) - self.start) * 1000, 2),
            "spans": self.spans,
        }


class Tracer:
    def __init__(self):
        self._traces: dict[str, Trace] = {}
        self._order: list[str] = []
        self._lock = Lock()
        self._current: Optional[Trace] = None

    def start_trace(self, name: str, session_id: Optional[str] = None) -> Trace:
        tid = "trace_" + uuid.uuid4().hex[:12]
        t = Trace(tid, name, session_id)
        with self._lock:
            self._traces[tid] = t
            self._order.append(tid)
            self._order = self._order[-100:]
            self._current = t
        return t

    def end_trace(self, t: Trace) -> None:
        t.end = time.time()

    @contextmanager
    def span(self, trace: Trace, name: str, attributes: Optional[dict] = None):
        span = {"name": name, "start": time.time(), "end": None, "attributes": attributes or {}}
        trace.spans.append(span)
        try:
            yield span
        finally:
            span["end"] = time.time()
            span["duration_ms"] = round((span["end"] - span["start"]) * 1000, 2)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            ids = self._order[-limit:][::-1]
            return [self._traces[i].to_dict() for i in ids if i in self._traces]

    def get(self, trace_id: str) -> Optional[dict]:
        t = self._traces.get(trace_id)
        return t.to_dict() if t else None


tracer = Tracer()
