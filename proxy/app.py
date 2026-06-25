"""
app.py
======

FastAPI entry point for the Anthropic -> OpenCode Zen Go proxy.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8080

Environment:
    OPENCODE_API_KEY              (required) bearer token for the upstream
    OPENCODE_BASE_URL             (optional) defaults to https://opencode.ai/zen/go/v1
    MODEL_<N>_ALIAS / _UPSTREAM   (optional) per-model routing
    UPSTREAM_TIMEOUT              (optional) seconds, defaults to 120
    LOG_LEVEL                     (optional) debug|info|warning|error
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from config import ProxyConfig, load_config
from stream import openai_stream_to_anthropic
from translator import (
    AnthropicMessagesRequest,
    anthropic_to_openai,
    openai_to_anthropic_message,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("claude-proxy")


# ---------------------------------------------------------------------------
# Lifespan: shared httpx client + config
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    app.state.config = config

    if not config.api_key or config.api_key == "your-opencode-api-key-here":
        logger.warning(
            "OPENCODE_API_KEY is not set or still the placeholder — "
            "upstream calls will fail until it is provided."
        )
    if not config.routes:
        logger.warning(
            "No MODEL_*_ALIAS / MODEL_*_UPSTREAM entries found — "
            "every /v1/messages request will be rejected with 400."
        )
    else:
        for r in config.routes:
            logger.info("  route: %-10s -> %s", r.alias, r.upstream)

    timeout = httpx.Timeout(config.timeout, connect=10.0)
    app.state.http = httpx.AsyncClient(timeout=timeout)
    logger.info(
        "Proxy ready -> %s (default=%s, timeout=%.0fs)",
        config.base_url,
        config.default_route.upstream if config.default_route else "<none>",
        config.timeout,
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(
    title="Claude -> OpenCode Go Proxy",
    version="0.2.0",
    lifespan=lifespan,
    description=(
        "Anthropic-compatible proxy that routes each Claude Code request to the "
        "right OpenCode Go upstream model based on the incoming `model` field."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upstream_headers(config: ProxyConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _error_response(status: int, err_type: str, message: str) -> JSONResponse:
    """Build an Anthropic-style error envelope."""
    return JSONResponse(
        status_code=status,
        content={
            "type": "error",
            "error": {"type": err_type, "message": message},
        },
    )


def _passthrough_error(resp: httpx.Response) -> JSONResponse:
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = {"raw": resp.text}

    return JSONResponse(
        status_code=resp.status_code,
        content={
            "type": "error",
            "error": {
                "type": "upstream_error",
                "message": (
                    f"Upstream returned {resp.status_code}: "
                    f"{json.dumps(body)[:500]}"
                ),
            },
        },
    )


def _models_payload(config: ProxyConfig) -> dict[str, Any]:
    return {
        "data": [
            {
                "id": r.alias,
                "type": "model",
                "display_name": r.display,
            }
            for r in config.routes
        ]
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    cfg: ProxyConfig = request.app.state.config
    return {
        "status": "ok",
        "default_model": cfg.default_route.alias if cfg.default_route else None,
        "models": [r.alias for r in cfg.routes],
        "upstream": cfg.base_url,
    }


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    """Anthropic-compatible model list. Claude Code reads this on startup
    and uses the `id` values for `/model` switching.
    """
    cfg: ProxyConfig = request.app.state.config
    return _models_payload(cfg)


@app.post("/v1/messages")
async def create_message(request: Request) -> Response:
    config: ProxyConfig = request.app.state.config

    # 1. Parse + validate the Anthropic request.
    try:
        raw = await request.json()
    except json.JSONDecodeError as exc:
        return _error_response(400, "invalid_request_error", f"Invalid JSON: {exc}")

    try:
        anthropic_req = AnthropicMessagesRequest.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        logger.info("Rejected request: validation failed: %s", exc)
        return _error_response(
            400,
            "invalid_request_error",
            f"Request validation failed: {exc}",
        )

    # 2. Route based on the incoming `model` field.
    route = config.resolve(anthropic_req.model)
    if route is None:
        known = ", ".join(r.alias for r in config.routes) or "<none configured>"
        return _error_response(
            400,
            "invalid_request_error",
            f"No models configured. Set MODEL_1_ALIAS and MODEL_1_UPSTREAM. "
            f"(received model={anthropic_req.model!r})",
        )
    if anthropic_req.model and anthropic_req.model.strip().lower() != route.alias.lower():
        # The request asked for something we don't know — fall back to the
        # default but log it so misconfig is visible.
        known = ", ".join(r.alias for r in config.routes)
        logger.warning(
            "Unknown model %r requested; falling back to default %r. Known: %s",
            anthropic_req.model, route.alias, known,
        )

    # 3. Translate request body using the chosen upstream slug.
    upstream_body = anthropic_to_openai(anthropic_req, route.upstream)
    logger.debug(
        "Forwarding: alias=%s upstream=%s stream=%s messages=%d",
        route.alias,
        route.upstream,
        upstream_body.get("stream"),
        len(upstream_body.get("messages", [])),
    )

    # 4. Branch: streaming vs non-streaming.
    if anthropic_req.stream:
        return StreamingResponse(
            _proxy_stream(upstream_body, route, anthropic_req.model, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming path.
    try:
        upstream: httpx.Response = await request.app.state.http.post(
            config.chat_url(),
            headers=_upstream_headers(config),
            json=upstream_body,
        )
    except httpx.TimeoutException:
        logger.warning(
            "Upstream timeout after %.0fs (alias=%s)", config.timeout, route.alias
        )
        return _error_response(504, "timeout_error", "Upstream request timed out")
    except httpx.HTTPError as exc:
        logger.exception("Upstream HTTP error: %s", exc)
        return _error_response(502, "upstream_error", f"Upstream HTTP error: {exc}")

    if upstream.status_code >= 400:
        logger.warning(
            "Upstream error status=%d alias=%s body=%s",
            upstream.status_code,
            route.alias,
            upstream.text[:500],
        )
        return _passthrough_error(upstream)

    try:
        upstream_json = upstream.json()
    except json.JSONDecodeError:
        return _error_response(502, "upstream_error", "Upstream returned non-JSON body")

    return JSONResponse(
        status_code=200,
        content=openai_to_anthropic_message(
            upstream_json, requested_model=route.alias
        ),
    )


# ---------------------------------------------------------------------------
# Streaming transport
# ---------------------------------------------------------------------------


async def _proxy_stream(
    body: dict[str, Any],
    route,                       # config.ModelRoute
    requested_model: str,
    request: Request,
) -> AsyncIterator[bytes]:
    config: ProxyConfig = request.app.state.config
    client: httpx.AsyncClient = request.app.state.http
    try:
        async with client.stream(
            "POST",
            config.chat_url(),
            headers=_upstream_headers(config),
            json=body,
        ) as upstream:
            if upstream.status_code >= 400:
                err_text = await upstream.aread()
                err_msg = (
                    f"Upstream {upstream.status_code} (alias={route.alias}): "
                    f"{err_text.decode('utf-8', errors='replace')[:500]}"
                )
                logger.warning("Streaming upstream error: %s", err_msg)
                yield _sse_error(upstream.status_code, err_msg)
                return

            async def _lines() -> AsyncIterator[str]:
                async for line in upstream.aiter_lines():
                    yield line

            async for chunk in openai_stream_to_anthropic(
                _lines(), requested_model=route.alias
            ):
                yield chunk.encode("utf-8")

    except httpx.TimeoutException:
        logger.warning(
            "Upstream streaming timeout after %.0fs (alias=%s)",
            config.timeout,
            route.alias,
        )
        yield _sse_error(504, "Upstream streaming request timed out")
    except httpx.HTTPError as exc:
        logger.exception("Upstream streaming HTTP error: %s", exc)
        yield _sse_error(502, f"Upstream HTTP error: {exc}")
    except Exception as exc:  # last-resort guard so the connection closes cleanly
        logger.exception("Unexpected streaming error: %s", exc)
        yield _sse_error(500, f"Internal proxy error: {exc}")


def _sse_error(status: int, message: str) -> bytes:
    payload = json.dumps(
        {
            "type": "error",
            "error": {"type": "upstream_error", "message": f"[{status}] {message}"},
        }
    )
    return f"event: error\ndata: {payload}\n\n".encode("utf-8")
