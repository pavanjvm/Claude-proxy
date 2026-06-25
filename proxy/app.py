"""
app.py
======

FastAPI entry point for the Anthropic -> OpenCode Zen Go proxy.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8080

Environment:
    OPENCODE_API_KEY    (required) bearer token for the upstream
    OPENCODE_BASE_URL   (optional) defaults to https://opencode.ai/zen/go/v1
    OPENCODE_MODEL      (optional) defaults to deepseek-v4-flash
    UPSTREAM_TIMEOUT    (optional) seconds, defaults to 120
    LOG_LEVEL           (optional) debug|info|warning|error
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from stream import openai_stream_to_anthropic
from translator import (
    AnthropicMessagesRequest,
    anthropic_to_openai,
    openai_to_anthropic_message,
)

# Load .env if present — non-fatal if the file is missing.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=False)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OPENCODE_BASE_URL = os.getenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
OPENCODE_MODEL = os.getenv("OPENCODE_MODEL", "deepseek-v4-flash")
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "120"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("claude-proxy")


# ---------------------------------------------------------------------------
# Lifespan: shared httpx client
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    api_key = os.getenv("OPENCODE_API_KEY")
    if not api_key or api_key == "your-opencode-api-key-here":
        logger.warning(
            "OPENCODE_API_KEY is not set or still the placeholder — "
            "upstream calls will fail until it is provided."
        )

    timeout = httpx.Timeout(UPSTREAM_TIMEOUT, connect=10.0)
    app.state.http = httpx.AsyncClient(timeout=timeout)
    logger.info(
        "Proxy ready -> %s (model=%s, timeout=%.0fs)",
        OPENCODE_BASE_URL,
        OPENCODE_MODEL,
        UPSTREAM_TIMEOUT,
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(
    title="Claude -> OpenCode DeepSeek Proxy",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Anthropic-compatible proxy that translates Claude Code requests into "
        "OpenCode Zen Go OpenAI Chat Completions calls targeting DeepSeek V4 Flash."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upstream_headers() -> dict[str, str]:
    api_key = os.getenv("OPENCODE_API_KEY", "")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _upstream_url() -> str:
    return f"{OPENCODE_BASE_URL}/chat/completions"


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
    """Convert an upstream httpx error into an Anthropic-style JSON body."""
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "model": OPENCODE_MODEL, "upstream": OPENCODE_BASE_URL}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """Minimal stub: Claude Code occasionally calls this to discover models."""
    return {
        "data": [
            {
                "id": OPENCODE_MODEL,
                "type": "model",
                "display_name": "DeepSeek V4 Flash (via OpenCode)",
            }
        ],
    }


@app.post("/v1/messages")
async def create_message(request: Request) -> Response:
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

    requested_model = anthropic_req.model
    upstream_body = anthropic_to_openai(anthropic_req, OPENCODE_MODEL)
    logger.debug(
        "Forwarding to upstream: model=%s stream=%s messages=%d",
        upstream_body.get("model"),
        upstream_body.get("stream"),
        len(upstream_body.get("messages", [])),
    )

    # 2. Branch: streaming vs non-streaming.
    if anthropic_req.stream:
        return StreamingResponse(
            _proxy_stream(upstream_body, requested_model, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming path.
    try:
        upstream: httpx.Response = await request.app.state.http.post(
            _upstream_url(),
            headers=_upstream_headers(),
            json=upstream_body,
        )
    except httpx.TimeoutException:
        logger.warning("Upstream timeout after %.0fs", UPSTREAM_TIMEOUT)
        return _error_response(504, "timeout_error", "Upstream request timed out")
    except httpx.HTTPError as exc:
        logger.exception("Upstream HTTP error: %s", exc)
        return _error_response(502, "upstream_error", f"Upstream HTTP error: {exc}")

    if upstream.status_code >= 400:
        logger.warning(
            "Upstream error status=%d body=%s",
            upstream.status_code,
            upstream.text[:500],
        )
        return _passthrough_error(upstream)

    try:
        upstream_json = upstream.json()
    except json.JSONDecodeError:
        return _error_response(502, "upstream_error", "Upstream returned non-JSON body")

    return JSONResponse(
        status_code=200,
        content=openai_to_anthropic_message(upstream_json, requested_model),
    )


# ---------------------------------------------------------------------------
# Streaming transport
# ---------------------------------------------------------------------------


async def _proxy_stream(
    body: dict[str, Any],
    requested_model: str,
    request: Request,
) -> AsyncIterator[bytes]:
    """Open a streaming POST to the upstream and translate SSE frames."""
    client: httpx.AsyncClient = request.app.state.http
    try:
        async with client.stream(
            "POST",
            _upstream_url(),
            headers=_upstream_headers(),
            json=body,
        ) as upstream:
            if upstream.status_code >= 400:
                # Surface error as a final message_delta so Claude Code sees it.
                err_text = await upstream.aread()
                err_msg = (
                    f"Upstream {upstream.status_code}: "
                    f"{err_text.decode('utf-8', errors='replace')[:500]}"
                )
                logger.warning("Streaming upstream error: %s", err_msg)
                yield _sse_error(upstream.status_code, err_msg)
                return

            async def _lines() -> AsyncIterator[str]:
                async for line in upstream.aiter_lines():
                    yield line

            async for chunk in openai_stream_to_anthropic(_lines(), requested_model):
                yield chunk.encode("utf-8")

    except httpx.TimeoutException:
        logger.warning("Upstream streaming timeout after %.0fs", UPSTREAM_TIMEOUT)
        yield _sse_error(504, "Upstream streaming request timed out")
    except httpx.HTTPError as exc:
        logger.exception("Upstream streaming HTTP error: %s", exc)
        yield _sse_error(502, f"Upstream HTTP error: {exc}")
    except Exception as exc:  # last-resort guard so the connection closes cleanly
        logger.exception("Unexpected streaming error: %s", exc)
        yield _sse_error(500, f"Internal proxy error: {exc}")


def _sse_error(status: int, message: str) -> bytes:
    """Emit a final message_delta carrying an error so the client sees it."""
    import json as _json

    payload = _json.dumps(
        {
            "type": "error",
            "error": {"type": "upstream_error", "message": f"[{status}] {message}"},
        }
    )
    return f"event: error\ndata: {payload}\n\n".encode("utf-8")
