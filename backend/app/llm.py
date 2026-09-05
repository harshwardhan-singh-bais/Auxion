"""LLM provider fallback chain.

Ordered layers: Groq -> SambaNova -> Mistral -> Cerebras -> NVIDIA -> Nemotron
-> OpenRouter -> Ollama (local, keyless). Every layer speaks the OpenAI
chat-completions + tool-calling protocol, so one client drives them all.

Behavior:
- `propose_action()` tries each configured provider in order; on any failure
  (auth, network, rate-limit, bad response) it records the error and moves to
  the next layer automatically.
- If every layer fails, it returns (None, report) and the caller falls back to
  the deterministic parser. The report always carries human-readable errors.
- Per-provider health (status / last error / latency / last-used model) is
  exposed via status() for the /api/llm/status endpoint and the dashboard.

The LLM only proposes structured intent. It never executes or moves money.
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Optional

import httpx

from .config import settings

# Layer order = failover order. Model + base URL overridable via env
# (e.g. GROQ_MODEL, OPENROUTER_BASE_URL). Ollama needs no key.
PROVIDER_DEFS: list[dict] = [
    {"id": "groq",        "env": "GROQ_API_KEY",        "model_env": "GROQ_MODEL",
     "model": "openai/gpt-oss-120b", "base": "https://api.groq.com/openai/v1"},
    {"id": "sambanova",   "env": "SAMBANOVA_API_KEY",   "model_env": "SAMBANOVA_MODEL",
     "model": "Meta-Llama-3.1-70B-Instruct", "base": "https://api.sambanova.ai/v1"},
    {"id": "mistral",     "env": "MISTRAL_API_KEY",     "model_env": "MISTRAL_MODEL",
     "model": "mistral-small-latest", "base": "https://api.mistral.ai/v1"},
    {"id": "cerebras",    "env": "CEREBRAS_API_KEY",    "model_env": "CEREBRAS_MODEL",
     "model": "gpt-oss-120b", "base": "https://api.cerebras.ai/v1"},
    {"id": "nvidia",      "env": "NVIDIA_API_KEY",      "model_env": "NVIDIA_MODEL",
     "model": "nvidia/llama-3.1-nemotron-70b-instruct", "base": "https://integrate.api.nvidia.com/v1"},
    {"id": "nemotron",    "env": "NEMOTRON_API_KEY",    "model_env": "NEMOTRON_MODEL",
     "model": "nvidia/llama-3.1-nemotron-ultra-253b-v1", "base": "https://integrate.api.nvidia.com/v1"},
    {"id": "openrouter",  "env": "OPENROUTER_API_KEY",  "model_env": "OPENROUTER_MODEL",
     "model": "meta-llama/llama-3.3-70b-instruct", "base": "https://openrouter.ai/api/v1"},
]
# Ollama is local and keyless; base URL from env, on by default.
OLLAMA_DEF = {"id": "ollama", "env": None, "model_env": "OLLAMA_MODEL",
              "model": "llama3.1:70b", "base": settings.llm_bases["ollama"]}

REQUEST_TIMEOUT = 25.0


class LLMChain:
    def __init__(self):
        self._lock = Lock()
        self._state: dict[str, dict] = {}
        self._build_state()

    # ---- provider registry ----
    def providers(self) -> list[dict]:
        defs = PROVIDER_DEFS + [OLLAMA_DEF]
        out = []
        for d in defs:
            key = ("ollama" if settings.ollama_enabled else "") if not d["env"] else settings.llm_keys.get(d["env"], "")
            out.append({
                "id": d["id"],
                "base": settings.llm_bases.get(d["id"]) or d["base"],
                "model": settings.llm_models.get(d["model_env"]) or d["model"],
                "configured": bool(key),
            })
        return out

    def _build_state(self) -> None:
        with self._lock:
            for p in self.providers():
                self._state[p["id"]] = {
                    "id": p["id"], "base": p["base"], "model": p["model"],
                    "configured": p["configured"],
                    "status": "ready" if p["configured"] else "unconfigured",
                    "last_error": None, "last_latency_ms": None, "last_used_at": None,
                    "calls": 0, "failures": 0,
                }

    def _key_for(self, provider_id: str) -> str:
        d = next(x for x in PROVIDER_DEFS + [OLLAMA_DEF] if x["id"] == provider_id)
        if not d["env"]:
            return "ollama"
        return settings.llm_keys.get(d["env"], "")

    def status(self) -> dict:
        with self._lock:
            active = self._state.get("_active")
            chain = [dict(s) for k, s in self._state.items() if k != "_active"]
        return {
            "chain": chain,
            "active_provider": active.get("id") if active else None,
            "active_model": active.get("model") if active else None,
            "any_configured": any(s["configured"] for s in chain),
        }

    # ---- error normalization ----
    @staticmethod
    def _friendly_err(exc: Exception, provider_id: str) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return f"{provider_id}: timed out after {REQUEST_TIMEOUT:.0f}s"
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            detail = ""
            try:
                body = exc.response.json()
                detail = body.get("error", {}).get("message") or body.get("message") or body.get("detail") or ""
                if isinstance(detail, dict):
                    detail = detail.get("message", "")
            except Exception:
                pass
            hints = {401: "invalid/missing API key", 403: "key not authorized for this model",
                     404: "model not available on this provider", 429: "rate limit exceeded",
                     500: "provider internal error", 503: "provider unavailable"}
            hint = hints.get(code, "HTTP error")
            return f"{provider_id}: HTTP {code} ({hint})" + (f" — {detail[:140]}" if detail else "")
        if isinstance(exc, httpx.ConnectError):
            return f"{provider_id}: connection refused (is it reachable?)"
        return f"{provider_id}: {type(exc).__name__}: {str(exc)[:140]}"

    # ---- single provider attempt (OpenAI-compatible chat completions + tools) ----
    def _call_provider(self, p: dict, system: str, user: str, tool: dict,
                       tool_name: str, max_tokens: int) -> dict:
        headers = {"content-type": "application/json", "authorization": f"Bearer {p['key']}"}
        if p["id"] == "openrouter":
            headers["HTTP-Referer"] = "http://localhost:8000"
            headers["X-Title"] = "Auxion"
        payload = {
            "model": p["model"], "max_tokens": max_tokens, "temperature": 0.1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": [{"type": "function", "function": tool}],
            "tool_choice": "auto",
        }
        resp = httpx.post(p["base"].rstrip("/") + "/chat/completions",
                          json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        tc = (msg.get("tool_calls") or [{}])[0]
        args = tc.get("function", {}).get("arguments")
        import json as _json
        plan = _json.loads(args) if isinstance(args, str) else args
        if not isinstance(plan, dict) or "intent" not in plan:
            # some providers return plain text; extract the first JSON object
            content = msg.get("content") or ""
            start = content.find("{")
            if start != -1:
                try:
                    plan = _json.loads(content[start:])
                except Exception:
                    plan = None
            else:
                plan = None
        if not isinstance(plan, dict) or "intent" not in plan:
            raise ValueError("provider returned no usable tool call")
        plan["_provider"] = p["id"]
        plan["_model"] = p["model"]
        return plan

    def _mark(self, p_id: str, ok: bool, err: Optional[str], latency_ms: int) -> None:
        with self._lock:
            s = self._state[p_id]
            s["calls" if ok else "failures"] += 1
            s["status"] = "ok" if ok else "degraded"
            s["last_error"] = None if ok else err
            s["last_latency_ms"] = latency_ms
            s["last_used_at"] = time.time()

    def propose_action(self, system: str, user: str, tool: dict, tool_name: str,
                       max_tokens: int = 1024) -> tuple[Optional[dict], dict]:
        """Try each provider layer in order. Returns (plan | None, report).

        report = {"active": provider_id | None, "error": str | None,
                  "latency_ms": int, "attempts": [{provider, error?}], "configured": n}
        """
        attempts: list[dict] = []
        configured = [p for p in self.providers() if p["configured"]]
        report = {"active": None, "error": None, "latency_ms": 0,
                  "attempts": attempts, "configured": len(configured)}
        if not configured:
            report["error"] = "no LLM provider configured — using deterministic intent parser"
            return None, report

        for p in configured:
            p = dict(p)
            p["key"] = self._key_for(p["id"])
            t0 = time.time()
            try:
                plan = self._call_provider(p, system, user, tool, tool_name, max_tokens)
            except Exception as e:
                err = self._friendly_err(e, p["id"])
                self._mark(p["id"], False, err, int((time.time() - t0) * 1000))
                attempts.append({"provider": p["id"], "error": err})
                continue
            latency = int((time.time() - t0) * 1000)
            self._mark(p["id"], True, None, latency)
            with self._lock:
                self._state["_active"] = {"id": p["id"], "model": p["model"]}
            report.update({"active": p["id"], "model": p["model"], "latency_ms": latency})
            attempts.append({"provider": p["id"], "ok": True})
            return plan, report

        all_errors = "; ".join(a["error"] for a in attempts if a.get("error"))
        report["error"] = (f"all {len(configured)} LLM providers failed — "
                           f"falling back to deterministic parser. ({all_errors})")
        return None, report

    def ping_all(self) -> dict:
        """Lightweight health probe of every configured provider (1-token call)."""
        results = []
        for p in self.providers():
            if not p["configured"]:
                results.append({"id": p["id"], "status": "unconfigured"})
                continue
            p = dict(p)
            p["key"] = self._key_for(p["id"])
            t0 = time.time()
            try:
                headers = {"content-type": "application/json", "authorization": f"Bearer {p['key']}"}
                resp = httpx.post(p["base"].rstrip("/") + "/chat/completions",
                                  json={"model": p["model"], "max_tokens": 1,
                                        "messages": [{"role": "user", "content": "hi"}]},
                                  headers=headers, timeout=min(REQUEST_TIMEOUT, 12.0))
                resp.raise_for_status()
                self._mark(p["id"], True, None, int((time.time() - t0) * 1000))
                results.append({"id": p["id"], "status": "ok"})
            except Exception as e:
                err = self._friendly_err(e, p["id"])
                self._mark(p["id"], False, err, int((time.time() - t0) * 1000))
                results.append({"id": p["id"], "status": "degraded", "error": err})
        return {"providers": results}


llm_chain = LLMChain()
