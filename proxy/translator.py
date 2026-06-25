"""
translator.py
=============

Translation layer between Anthropic Messages API and OpenAI Chat Completions.

The proxy receives Anthropic-format requests on `POST /v1/messages` and
forwards OpenAI-format requests to the OpenCode Zen Go backend. This module
is intentionally pure: it takes parsed Pydantic models in and returns dicts
out, with no I/O, so it can be tested in isolation.
"""

from __future__ import annotations

from typing import Any, Iterable

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Anthropic-side request models
# ---------------------------------------------------------------------------


class AnthropicTextBlock(BaseModel):
    type: str = "text"
    text: str


class AnthropicToolUseBlock(BaseModel):
    type: str = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class AnthropicToolResultBlock(BaseModel):
    type: str = "tool_result"
    tool_use_id: str
    content: Any  # str or list of text blocks
    is_error: bool | None = None


class AnthropicMessage(BaseModel):
    role: str
    # Content may be a plain string OR a list of typed blocks.
    content: Any


class AnthropicTool(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class AnthropicMessagesRequest(BaseModel):
    model: str
    messages: list[AnthropicMessage]
    max_tokens: int = 1024
    system: str | list[AnthropicTextBlock] | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: list[str] | None = None
    stream: bool = False
    tools: list[AnthropicTool] | None = None
    tool_choice: Any | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Translation: Anthropic -> OpenAI
# ---------------------------------------------------------------------------


def _flatten_content(content: Any) -> str:
    """Collapse a list of Anthropic content blocks into a plain string.

    Tool-use and tool-result blocks are serialised as JSON for now — a real
    tool-calling implementation will live elsewhere and intercept before this
    helper is called.
    """
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            import json

            parts.append(
                f"[tool_use name={block.get('name')} "
                f"input={json.dumps(block.get('input', {}))}]"
            )
        elif btype == "tool_result":
            import json

            inner = block.get("content", "")
            if not isinstance(inner, str):
                inner = json.dumps(inner)
            parts.append(f"[tool_result id={block.get('tool_use_id')} {inner}]")
        else:
            parts.append(str(block))
    return "\n".join(p for p in parts if p)


def _system_to_openai(
    system: str | list[AnthropicTextBlock] | None,
) -> list[dict[str, str]] | None:
    """Convert Anthropic `system` field to one or more OpenAI system messages."""
    if system is None:
        return None
    if isinstance(system, str):
        return [{"role": "system", "content": system}]
    # List of text blocks.
    text = "\n".join(b.text for b in system if getattr(b, "text", None))
    if not text:
        return None
    return [{"role": "system", "content": text}]


def _convert_messages(
    messages: Iterable[AnthropicMessage],
) -> list[dict[str, str]]:
    """Convert Anthropic messages into OpenAI chat messages."""
    out: list[dict[str, str]] = []
    for m in messages:
        role = m.role
        if role not in {"user", "assistant", "system"}:
            # Anthropic only defines user/assistant; coerce anything else.
            role = "user"
        out.append({"role": role, "content": _flatten_content(m.content)})
    return out


def _convert_tools(tools: list[AnthropicTool] | None) -> list[dict[str, Any]] | None:
    """Convert Anthropic tool definitions to OpenAI function-calling format."""
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for t in tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema or {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
        )
    return out


def _convert_tool_choice(tool_choice: Any) -> Any:
    """Best-effort translation of Anthropic tool_choice to OpenAI form."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        mapping = {
            "auto": "auto",
            "any": "required",
            "none": "none",
        }
        return mapping.get(tool_choice, tool_choice)
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        return {"type": "function", "function": {"name": tool_choice.get("name", "")}}
    return tool_choice


def anthropic_to_openai(
    req: AnthropicMessagesRequest,
    upstream_model: str,
) -> dict[str, Any]:
    """Translate an Anthropic Messages request into an OpenAI Chat Completion body.

    The Anthropic `model` field is intentionally ignored — we always pin to the
    configured upstream model (DeepSeek V4 Flash by default).
    """
    messages: list[dict[str, str]] = []
    system_msgs = _system_to_openai(req.system)
    if system_msgs:
        messages.extend(system_msgs)
    messages.extend(_convert_messages(req.messages))

    body: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": req.max_tokens,
        "stream": req.stream,
    }
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.top_p is not None:
        body["top_p"] = req.top_p
    if req.stop_sequences:
        body["stop"] = req.stop_sequences

    tools = _convert_tools(req.tools)
    if tools:
        body["tools"] = tools
    choice = _convert_tool_choice(req.tool_choice)
    if choice is not None:
        body["tool_choice"] = choice

    return body


# ---------------------------------------------------------------------------
# Translation: OpenAI -> Anthropic (non-streaming response)
# ---------------------------------------------------------------------------


def openai_to_anthropic_message(
    openai_response: dict[str, Any],
    requested_model: str,
) -> dict[str, Any]:
    """Translate a non-streaming OpenAI ChatCompletion response into the
    Anthropic Messages response shape used by Claude Code.
    """
    choice = (openai_response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    finish_reason = choice.get("finish_reason") or "end_turn"

    stop_reason = _map_finish_reason(finish_reason)

    usage = openai_response.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens", 0))
    output_tokens = int(usage.get("completion_tokens", 0))

    return {
        "id": openai_response.get("id", "msg_proxy"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _map_finish_reason(finish: str) -> str | None:
    """Map OpenAI finish_reason onto Anthropic stop_reason values."""
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
        "function_call": "tool_use",
    }
    return mapping.get(finish, "end_turn")
