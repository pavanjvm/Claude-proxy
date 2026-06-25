"""
config.py
=========

Loads the proxy's multi-model configuration from environment variables.

The proxy maps Anthropic-facing model names (what Claude Code sends in the
`model` field) onto upstream OpenCode Go model slugs. Each entry is read
from a trio of `MODEL_<N>_*` env vars so adding a new model is a pure env
change — no code edits.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env if present (best-effort).
load_dotenv(
    dotenv_path=os.path.join(os.path.dirname(__file__), ".env"),
    override=False,
)

_INDEX_RE = re.compile(r"^MODEL_(\d+)_ALIAS$", re.IGNORECASE)


@dataclass(frozen=True)
class ModelRoute:
    """One Anthropic-facing model name -> upstream config."""
    alias: str            # what Claude Code sends (e.g. "deepseek")
    upstream: str         # what the upstream API expects (e.g. "deepseek-v4-flash")
    display: str          # human label for /v1/models

    @property
    def id(self) -> str:
        return self.alias


@dataclass(frozen=True)
class ProxyConfig:
    base_url: str
    api_key: str
    timeout: float
    routes: list[ModelRoute]
    default_route: ModelRoute | None

    def resolve(self, requested_model: str | None) -> ModelRoute | None:
        """Pick the route for an incoming `model` field.

        Resolution rules:
        1. If `model` is set and matches a known alias (case-insensitive) -> that route.
        2. Otherwise return the default route (first MODEL_*_ALIAS).
        3. If nothing is configured, return None — the caller should 400.
        """
        if not self.routes:
            return None
        if requested_model:
            needle = requested_model.strip().lower()
            for r in self.routes:
                if r.alias.lower() == needle:
                    return r
        return self.default_route

    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


def _load_routes() -> list[ModelRoute]:
    """Walk the env, collect all MODEL_<N>_* triples, return sorted by N."""
    by_index: dict[int, dict[str, str]] = {}
    for env_key, env_val in os.environ.items():
        m = _INDEX_RE.match(env_key)
        if not m:
            continue
        idx = int(m.group(1))
        slot = by_index.setdefault(idx, {})
        slot["alias"] = env_val.strip()

        upstream_key = f"MODEL_{idx}_UPSTREAM"
        if upstream_key in os.environ:
            slot["upstream"] = os.environ[upstream_key].strip()

        display_key = f"MODEL_{idx}_DISPLAY"
        if display_key in os.environ:
            slot["display"] = os.environ[display_key].strip()

    routes: list[ModelRoute] = []
    for idx in sorted(by_index):
        slot = by_index[idx]
        alias = slot.get("alias")
        upstream = slot.get("upstream")
        if not alias or not upstream:
            # Skip incomplete entries — but log so misconfig is visible.
            print(
                f"[config] skipping MODEL_{idx}: needs both ALIAS and UPSTREAM "
                f"(got alias={alias!r} upstream={upstream!r})"
            )
            continue
        display = slot.get("display") or f"{alias} -> {upstream}"
        routes.append(ModelRoute(alias=alias, upstream=upstream, display=display))
    return routes


def load_config() -> ProxyConfig:
    api_key = os.getenv("OPENCODE_API_KEY", "")
    base_url = os.getenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1")
    try:
        timeout = float(os.getenv("UPSTREAM_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0

    routes = _load_routes()
    return ProxyConfig(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        routes=routes,
        default_route=routes[0] if routes else None,
    )
