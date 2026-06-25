"""
stream.py
=========

SSE streaming translation from OpenAI Chat Completions (delta chunks) to
Anthropic Messages API events (message_start, content_block_start,
content_block_delta, content_block_stop, message_delta, message_stop).

Claude Code expects a specific event shape — see
https://docs.anthropic.com/en/api/messages-streaming — and treats any deviation
as a malformed response, so the order and field names below are load-bearing.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import AsyncIterator


ANTHROPIC_API_VERSION = "2023-06-01"
EVENT_MESSAGE_START = "message_start"
EVENT_CONTENT_BLOCK_START = "content_block_start"
EVENT_CONTENT_BLOCK_DELTA = "content_block_delta"
EVENT_CONTENT_BLOCK_STOP = "content_block_stop"
EVENT_MESSAGE_DELTA = "message_delta"
EVENT_MESSAGE_STOP = "message_stop"
EVENT_PING = "ping"


def _sse(event: str, data: dict) -> str:
    """Format one Anthropic SSE event block."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


async def openai_stream_to_anthropic(
    upstream_lines: AsyncIterator[str],
    requested_model: str,
) -> AsyncIterator[str]:
    """Translate an OpenAI SSE byte stream into Anthropic SSE events.

    `upstream_lines` should yield *decoded* lines from `response.aiter_lines()`.
    The caller is responsible for HTTP error handling.
    """
    message_id = _new_message_id()
    started_at = int(time.time() * 1000)

    # ---- message_start ---------------------------------------------------
    yield _sse(
        EVENT_MESSAGE_START,
        {
            "type": EVENT_MESSAGE_START,
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    # Single text content block — DeepSeek V4 Flash does not stream tool calls
    # in a way we can map cleanly yet, so we route everything through block 0.
    block_index = 0
    block_open = False
    text_started = False
    finish_reason: str | None = None
    input_tokens = 0
    output_tokens = 0

    async for raw in upstream_lines:
        if not raw:
            continue
        line = raw.strip()
        if not line:
            continue
        if line.startswith(":"):  # SSE comment / ping
            yield _sse(EVENT_PING, {"type": EVENT_PING})
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            # Skip malformed lines but keep the stream alive.
            continue

        # Usage only arrives in some providers' final chunk.
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            input_tokens = int(usage.get("prompt_tokens", input_tokens) or 0)
            output_tokens = int(usage.get("completion_tokens", output_tokens) or 0)

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            finish = choice.get("finish_reason")
            if finish:
                finish_reason = finish

            content_piece = delta.get("content")
            if content_piece:
                if not text_started:
                    # Open the text block lazily on first token.
                    yield _sse(
                        EVENT_CONTENT_BLOCK_START,
                        {
                            "type": EVENT_CONTENT_BLOCK_START,
                            "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    block_open = True
                    text_started = True
                yield _sse(
                    EVENT_CONTENT_BLOCK_DELTA,
                    {
                        "type": EVENT_CONTENT_BLOCK_DELTA,
                        "index": block_index,
                        "delta": {"type": "text_delta", "text": content_piece},
                    },
                )

        # When a choice is finished mid-stream we let the upstream tell us.

    # ---- close the text block ------------------------------------------
    if block_open:
        yield _sse(
            EVENT_CONTENT_BLOCK_STOP,
            {"type": EVENT_CONTENT_BLOCK_STOP, "index": block_index},
        )

    # ---- message_delta (stop_reason + final usage) ----------------------
    stop_reason = _map_finish_reason(finish_reason)
    yield _sse(
        EVENT_MESSAGE_DELTA,
        {
            "type": EVENT_MESSAGE_DELTA,
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
    )

    # ---- message_stop ---------------------------------------------------
    yield _sse(
        EVENT_MESSAGE_STOP,
        {"type": EVENT_MESSAGE_STOP},
    )

    # Reference `started_at` so it isn't optimised away — useful for logs.
    _ = started_at


def _map_finish_reason(finish: str | None) -> str | None:
    if finish is None:
        return None
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
        "function_call": "tool_use",
    }
    return mapping.get(finish, "end_turn")


async def keepalive_ping() -> str:
    """Emit a single Anthropic-shaped ping event (used between chunks if needed)."""
    return _sse(EVENT_PING, {"type": EVENT_PING})
