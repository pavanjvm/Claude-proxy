# Claude Code → OpenCode Go Multi-Model Proxy

A small, production-quality proxy server that lets **Claude Code** talk to
multiple models on **OpenCode Zen Go's** OpenAI-compatible chat completions
API. You pick a model in Claude Code with `/model`; the proxy routes each
request to the right upstream.

```
+----------------+        +-------------------+        +----------------------+
|  Claude Code   |  --->  |  This Proxy       |  --->  | OpenCode Zen Go API  |
|  (Anthropic)   |  <---  |  (FastAPI / SSE)  |  <---  | (OpenAI-compatible)  |
+----------------+        +-------------------+        +----------------------+
                          routes by incoming `model`
```

The Anthropic `model` field is now **load-bearing**: it tells the proxy which
upstream to use. Aliases are configured via env vars (see below). Default
example pairs `deepseek` with `deepseek-v4-flash` and `minimax` with
`minimax-m3` on the same OpenCode Go endpoint.

## Features

- Anthropic-compatible `POST /v1/messages` endpoint.
- Multi-model routing — one OpenCode account, many models.
- `/v1/models` lists every configured alias so Claude Code's model picker
  shows them.
- Translates Anthropic requests → OpenAI Chat Completions.
- Translates OpenAI streaming chunks → Anthropic SSE events
  (`message_start`, `content_block_start`, `content_block_delta`,
  `content_block_stop`, `message_delta`, `message_stop`).
- Streaming and non-streaming paths.
- 401 / 429 / timeout / invalid-request error handling with passthrough
  status codes.
- Adding a new model is a single env entry — no code change.

## Requirements

- Python 3.11+
- An OpenCode Zen Go API key (set as `OPENCODE_API_KEY`).

## Installation

```bash
cd proxy
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Copy the example env file and set your key:

```bash
cp .env.example .env
# edit .env — at minimum, set OPENCODE_API_KEY
```

The shipped `.env.example` configures two models out of the box:

| Alias (Anthropic-side) | Upstream slug (OpenCode Go) |
| ---------------------- | --------------------------- |
| `deepseek`             | `deepseek-v4-flash`         |
| `minimax`              | `minimax-m3`                |

## Run

```bash
uvicorn app:app --host 0.0.0.0 --port 8080
```

Or with auto-reload during development:

```bash
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

You should see:

```
INFO [claude-proxy]   route: deepseek   -> deepseek-v4-flash
INFO [claude-proxy]   route: minimax    -> minimax-m3
INFO [claude-proxy] Proxy ready -> https://opencode.ai/zen/go/v1 (default=deepseek-v4-flash, timeout=120s)
```

## Configuration (Claude Code)

Point Claude Code at the proxy. The API key can be any non-empty string — it
is never sent to the upstream.

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_API_KEY=dummy

claude
```

Inside Claude Code, switch models with:

```
/model deepseek
/model minimax
```

Both names are listed in `/v1/models` and resolve to the right upstream.

## Endpoints

| Method | Path           | Purpose                                                                |
| ------ | -------------- | ---------------------------------------------------------------------- |
| `POST` | `/v1/messages` | Anthropic Messages API. Streams when `stream=true`.                    |
| `GET`  | `/v1/models`   | List of all configured aliases. Claude Code uses this for `/model`.    |
| `GET`  | `/healthz`     | Liveness + current routing table.                                      |

## Environment variables

### Shared

| Variable            | Required | Default                              | Notes                                              |
| ------------------- | -------- | ------------------------------------ | -------------------------------------------------- |
| `OPENCODE_API_KEY`  | yes      | —                                    | Bearer token sent to the upstream.                 |
| `OPENCODE_BASE_URL` | no       | `https://opencode.ai/zen/go/v1`      | Override to point at a different gateway.          |
| `UPSTREAM_TIMEOUT`  | no       | `120`                                | Seconds before the upstream is abandoned.          |
| `LOG_LEVEL`         | no       | `info`                               | `debug`, `info`, `warning`, `error`.               |

### Per-model routing

Each model is described by a numbered triple of env vars. The number just
orders the entries; pick any positive integer, but keep them consecutive and
unique.

```
MODEL_<N>_ALIAS=<name-claude-code-sends>
MODEL_<N>_UPSTREAM=<slug-opencode-accepts>
MODEL_<N>_DISPLAY=<human-friendly-label>     # optional
```

The first entry (lowest `N`) is the default if a request omits `model` or
sends something the proxy doesn't recognise.

#### Example: two models

```
OPENCODE_API_KEY=sk-zen-...
OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1

MODEL_1_ALIAS=deepseek
MODEL_1_UPSTREAM=deepseek-v4-flash
MODEL_1_DISPLAY=DeepSeek V4 Flash (via OpenCode Go)

MODEL_2_ALIAS=minimax
MODEL_2_UPSTREAM=minimax-m3
MODEL_2_DISPLAY=MiniMax M3 (via OpenCode Go)
```

#### Example: add a third model

```
MODEL_3_ALIAS=kimi
MODEL_3_UPSTREAM=kimi-k2.6
```

Restart `uvicorn` and `/v1/models` will show the new entry.

## Supported request fields

`POST /v1/messages` accepts the following fields. Anything not listed is
currently ignored.

| Anthropic field   | Status      |
| ----------------- | ----------- |
| `model`           | **used for routing** — must match a configured alias |
| `messages`        | supported (text, `tool_use`, `tool_result` blocks)    |
| `max_tokens`      | supported   |
| `system`          | supported (string or list of text blocks) |
| `temperature`     | supported   |
| `top_p`           | supported   |
| `stop_sequences`  | supported   |
| `stream`          | supported   |
| `tools`           | stub — translated to OpenAI functions, not invoked yet |
| `tool_choice`     | stub — translated best-effort                       |
| `metadata`        | ignored    |

## Project structure

```
proxy/
├── app.py            # FastAPI routes, transport, error handling
├── config.py         # Multi-model config loader (MODEL_*_ALIAS / _UPSTREAM)
├── translator.py     # Anthropic <-> OpenAI request/response translation
├── stream.py         # OpenAI SSE -> Anthropic SSE translation
├── requirements.txt
├── .env.example
└── README.md
```

`config.py` knows only about env vars and the route table. `translator.py`
is pure data in / data out (no I/O), which makes it easy to unit-test.
`stream.py` consumes an async iterator of upstream lines and yields
Anthropic-formatted SSE strings — no knowledge of FastAPI or httpx leaks in.

## Testing with curl

### Non-streaming (explicit model)

```bash
curl -s http://localhost:8080/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: dummy" \
  -d '{
    "model": "deepseek",
    "max_tokens": 256,
    "messages": [
      {"role": "user", "content": "Say hello in one short sentence."}
    ]
  }'
```

### Non-streaming (other model)

```bash
curl -s http://localhost:8080/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: dummy" \
  -d '{
    "model": "minimax",
    "max_tokens": 256,
    "messages": [
      {"role": "user", "content": "Say hello in one short sentence."}
    ]
  }'
```

The only difference is the `model` field; the proxy picks the right
upstream automatically.

### Streaming

```bash
curl -N http://localhost:8080/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: dummy" \
  -d '{
    "model": "deepseek",
    "max_tokens": 256,
    "stream": true,
    "messages": [
      {"role": "user", "content": "Stream a haiku about proxies."}
    ]
  }'
```

You should see an event sequence of the form:

```
event: message_start
data: {...}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Bytes"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn",...},"usage":{...}}

event: message_stop
data: {"type":"message_stop"}
```

### Health check + routing table

```bash
curl -s http://localhost:8080/healthz
# => {"status":"ok","default_model":"deepseek","models":["deepseek","minimax"],"upstream":"https://opencode.ai/zen/go/v1"}
```

### Model discovery (used by Claude Code's `/model` picker)

```bash
curl -s http://localhost:8080/v1/models | jq
```

```json
{
  "data": [
    {"id": "deepseek", "type": "model", "display_name": "DeepSeek V4 Flash (via OpenCode Go)"},
    {"id": "minimax",  "type": "model", "display_name": "MiniMax M3 (via OpenCode Go)"}
  ]
}
```

## Error semantics

- `400 invalid_request_error` — the inbound JSON failed validation, or
  `model` did not match any configured alias.
- `401` / `429` (and any other upstream status) — passthrough, wrapped in
  Anthropic's `{"type":"error","error":{...}}` envelope.
- `502 upstream_error` — the upstream connection failed, timed out, or
  returned non-JSON.
- `504 timeout_error` — explicit timeout from this proxy.

Streaming failures are delivered as a final `event: error` SSE frame so the
client sees a structured error rather than a truncated stream.

## Extending

- **More models** — add a `MODEL_<N>_*` triple in `.env` and restart.
- **Different providers per model** — today every model uses
  `OPENCODE_BASE_URL` + `OPENCODE_API_KEY`. If you want one alias to hit
  OpenRouter, etc., add `MODEL_<N>_BASE_URL` / `MODEL_<N>_API_KEY`
  overrides in `config.py` (small change).
- **Tool calling** — see the previous architecture notes; the streaming
  layer already has hooks for emitting a second content block.
- **Caching / rate limiting** — wrap `request.app.state.http` in middleware.

## License

MIT (or whatever you prefer — drop a `LICENSE` file in to lock it in).
