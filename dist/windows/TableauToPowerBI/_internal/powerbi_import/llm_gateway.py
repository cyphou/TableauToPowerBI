"""Unified LLM gateway — online/offline connectivity for on-the-fly calls (Sprint 221).

Wraps the existing ``llm_client.LLMClient`` with a connectivity-aware routing
layer so any part of the engine can call an LLM *on the fly* — for DAX/M/visual
correction, explanations, naming, and more — whether the machine is **online**
(OpenAI / Anthropic / Azure OpenAI) or **offline** (a local OpenAI-compatible
model: Ollama / LM Studio / vLLM), degrading gracefully to a deterministic no-op
when neither is available.

Design principles:
    * Offline-first, opt-in online — default mode ``auto``: prefer a reachable
      local model, use cloud only when configured + reachable, else no-op.
    * Connectivity is probed, not assumed — a cheap TCP probe picks the route and
      fails over fast.
    * Assistive, never authoritative — returns a confidence-scored suggestion.
    * Secrets from env only; identifiers redacted before any network call.
    * Stdlib-first — local endpoints are plain HTTP; no new dependencies.

Public API:
    LLMGateway(mode='auto', ...).complete(prompt, system=None) -> LLMResult
    LLMGateway(...).call(system, user) -> LLMResult
    LLMGateway(...).is_online() -> bool
    LLMGateway(...).status() -> dict            # for the MCP `llm_status` tool
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
from dataclasses import dataclass, asdict
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("tableau_to_powerbi.llm_gateway")

DEFAULT_LOCAL_URL = "http://localhost:11434/v1"  # Ollama default
_CLOUD_PROVIDERS = ("openai", "anthropic", "azure_openai")


@dataclass
class LLMResult:
    """Result of a gateway call (always returned; never raises)."""
    text: str = ""
    provider: str = "none"
    mode: str = "auto"
    route: str = "none"          # none | local | cloud
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    confidence: str = "low"      # high | medium | low
    source: str = "offline"      # llm | offline
    cached: bool = False
    error: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)

    def __bool__(self) -> bool:
        return bool(self.text)


class LLMGateway:
    """Connectivity-aware façade over ``LLMClient``."""

    def __init__(self, mode=None, provider=None, api_key=None, model=None,
                 endpoint=None, local_url=None, max_calls=None,
                 max_cost_usd=None, timeout=30, probe_timeout=1.5,
                 dry_run=False, redact=True, cache=True):
        env = os.environ.get
        self.mode = (mode or env("LLM_MODE", "auto")).lower()
        if self.mode not in ("auto", "online", "offline"):
            self.mode = "auto"
        self.cloud_provider = (provider or env("LLM_PROVIDER", "openai")).lower()
        self.api_key = api_key or env("LLM_API_KEY", "")
        self.model = model or env("LLM_MODEL") or None
        self.endpoint = endpoint or env("LLM_ENDPOINT") or None  # azure/custom cloud
        self.local_url = local_url or env("LLM_LOCAL_URL", DEFAULT_LOCAL_URL)
        self.max_calls = int(max_calls if max_calls is not None
                             else env("LLM_MAX_CALLS", "100"))
        _mc = max_cost_usd if max_cost_usd is not None else env("LLM_MAX_COST_USD")
        self.max_cost_usd = float(_mc) if _mc not in (None, "") else None
        self.timeout = timeout
        self.probe_timeout = probe_timeout
        self.dry_run = dry_run
        self.redact = redact
        self.cache_enabled = cache

        self._online_cache: Optional[bool] = None
        self._cache: Dict[str, LLMResult] = {}
        self._calls_used = 0
        self._cost_used = 0.0

    # ── connectivity ────────────────────────────────────────────────
    def is_online(self, url=None) -> bool:
        """Cheap TCP reachability probe against the effective endpoint host:port.

        Result is cached for the gateway's lifetime (per-run). Never blocks
        longer than ``probe_timeout``.
        """
        if self._online_cache is not None and url is None:
            return self._online_cache
        target = url or self._probe_target()
        host, port = target
        try:
            with socket.create_connection((host, port), timeout=self.probe_timeout):
                reachable = True
        except OSError:
            reachable = False
        if url is None:
            self._online_cache = reachable
        return reachable

    def _probe_target(self):
        """Return (host, port) to probe based on mode + configured route."""
        # In offline/auto we care about the local endpoint first.
        if self.mode in ("offline", "auto"):
            return _host_port(self.local_url)
        # online → probe the cloud endpoint
        if self.cloud_provider == "azure_openai" and self.endpoint:
            return _host_port(self.endpoint)
        if self.cloud_provider == "anthropic":
            return ("api.anthropic.com", 443)
        return ("api.openai.com", 443)

    # ── route resolution ────────────────────────────────────────────
    def resolve(self):
        """Return (route, provider, endpoint). route ∈ {none, local, cloud}."""
        local_ok = bool(self.local_url) and self.is_online(_host_port(self.local_url))
        cloud_ok = bool(self.api_key) and self._cloud_reachable()

        if self.mode == "offline":
            if local_ok:
                return ("local", "local", self.local_url)
            return ("none", None, None)
        if self.mode == "online":
            if cloud_ok:
                return ("cloud", self.cloud_provider, self.endpoint)
            return ("none", None, None)
        # auto: prefer local, then cloud, then none
        if local_ok:
            return ("local", "local", self.local_url)
        if cloud_ok:
            return ("cloud", self.cloud_provider, self.endpoint)
        return ("none", None, None)

    def _cloud_reachable(self):
        if self.cloud_provider == "azure_openai" and self.endpoint:
            return self.is_online(_host_port(self.endpoint))
        host = ("api.anthropic.com" if self.cloud_provider == "anthropic"
                else "api.openai.com")
        return self.is_online((host, 443))

    @property
    def enabled(self) -> bool:
        return self.resolve()[0] != "none"

    # ── calls ───────────────────────────────────────────────────────
    def complete(self, prompt, system=None) -> LLMResult:
        """Single-prompt completion. Returns an LLMResult (never raises)."""
        return self.call(system or "You are a helpful assistant.", prompt)

    def call(self, system, user) -> LLMResult:
        route, provider, endpoint = self.resolve()
        result = LLMResult(mode=self.mode, route=route, provider=provider or "none")

        if route == "none":
            result.error = "no_route"
            return result

        # Budget guard (calls + cost)
        if self._calls_used >= self.max_calls:
            result.error = "call_budget_exceeded"
            return result
        if self.max_cost_usd is not None and self._cost_used >= self.max_cost_usd:
            result.error = "cost_budget_exceeded"
            return result

        # Redact identifiers/credentials before anything leaves the machine.
        safe_system = self._redact(system)
        safe_user = self._redact(user)

        # Cache
        key = self._cache_key(provider, safe_system, safe_user)
        if self.cache_enabled and key in self._cache:
            cached = self._cache[key]
            out = LLMResult(**{**cached.to_dict()})
            out.cached = True
            return out

        client = self._make_client(route, provider, endpoint)
        if client is None:
            result.error = "client_unavailable"
            return result

        raw = client.call(safe_system, safe_user)
        result.text = raw.get("text", "")
        result.input_tokens = raw.get("input_tokens", 0)
        result.output_tokens = raw.get("output_tokens", 0)
        result.cost = raw.get("cost", 0.0)
        result.cached = raw.get("cached", False)
        if raw.get("error"):
            result.error = raw["error"]
        result.source = "llm" if result.text else "offline"
        result.confidence = "medium" if result.text else "low"

        self._calls_used += 1
        self._cost_used += result.cost
        if self.cache_enabled and result.text:
            self._cache[key] = result
        return result

    # ── helpers ─────────────────────────────────────────────────────
    def _make_client(self, route, provider, endpoint):
        try:
            from powerbi_import.llm_client import LLMClient
        except ImportError:
            try:
                from llm_client import LLMClient  # type: ignore
            except ImportError:
                return None
        try:
            if route == "local":
                return LLMClient(provider="local", api_key=self.api_key or "",
                                 model=self.model, endpoint=self.local_url,
                                 max_calls=self.max_calls, timeout=self.timeout,
                                 dry_run=self.dry_run)
            return LLMClient(provider=provider, api_key=self.api_key,
                             model=self.model, endpoint=self.endpoint,
                             max_calls=self.max_calls, timeout=self.timeout,
                             dry_run=self.dry_run)
        except Exception as exc:  # noqa: BLE001 — never break the caller
            logger.error("LLM client init failed: %s", exc)
            return None

    def _redact(self, text):
        if not text or not self.redact:
            return text or ""
        try:
            from security_validator import redact_credentials
            return redact_credentials(text)
        except Exception:  # noqa: BLE001 — redaction is best-effort
            return text

    @staticmethod
    def _cache_key(provider, system, user):
        h = hashlib.sha256()
        h.update((provider or "").encode("utf-8"))
        h.update(b"\x00")
        h.update((system or "").encode("utf-8"))
        h.update(b"\x00")
        h.update((user or "").encode("utf-8"))
        return h.hexdigest()

    def status(self) -> Dict:
        """Report gateway configuration + reachability (no secrets)."""
        route, provider, _ = self.resolve()
        return {
            "mode": self.mode,
            "route": route,
            "provider": provider,
            "cloud_provider": self.cloud_provider,
            "local_url": self.local_url,
            "has_api_key": bool(self.api_key),
            "online_local": self.is_online(_host_port(self.local_url)),
            "enabled": route != "none",
            "max_calls": self.max_calls,
            "max_cost_usd": self.max_cost_usd,
            "calls_used": self._calls_used,
            "cost_used": round(self._cost_used, 6),
        }


def _host_port(url):
    """Parse (host, port) from a URL; default port by scheme."""
    if not url:
        return ("localhost", 80)
    if "://" not in url:
        url = "http://" + url
    p = urlparse(url)
    host = p.hostname or "localhost"
    port = p.port or (443 if p.scheme == "https" else 80)
    return (host, port)
