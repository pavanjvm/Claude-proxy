# Claude Code → DeepSeek V4 Flash Proxy

A small, production-quality proxy server that lets **Claude Code** talk to
**DeepSeek V4 Flash** through **OpenCode Zen Go's** OpenAI-compatible chat
completions API.

Claude Code thinks it's talking to Anthropic (`POST /v1/messages` with the
Anthropic Messages API shape). Internally, the proxy translates the request
into an OpenAI Chat Completion call against
`https://opencode.ai/zen/go/v1/chat/completions`, using the configured
upstream model (`deepseek-v4-flash` by default).

```
+----------------+        +-------------------+        +----------------------+
|  Claude Code   |  --->  |  This Proxy       |  --->  | OpenCode Zen Go API  |
|  (Anthropic)   |  <---  |  (FastAPI / SSE)  |  <---  | (OpenAI-compatible)  |
+----------------+        +-------------------+        +----------------------+
                          pins model = deepseek-v4-flash
```

## Features

- Anthropic-compatible `POST /v1/messages` endpoint.
- Translates Anthropic requests → OpenAI Chat Completions.
- Translates OpenAI streaming chunks → Anthropic SSE events
  (`message_start`, `content_block_start`, `content_block_delta`,
  `content_block_stop`, `message_delta`, `message_stop`).
- Streaming and non-streaming paths.
- 401 / 429 / timeout / invalid-request error handling with passthrough
  status codes.
- Upstream model is pinned — incoming `model` is ignored.
- Pluggable translator; tool-calling can be added without touching the
  transport layer.

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
# edit .env and replace OPENCODE_API_KEY
```

## Run

```bash
uvicorn app:app --host 0.0.0.0 --port 8080
```

Or, with auto-reload during development:

```bash
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

You should see something like:

```
Proxy ready -> https://opencode.ai/zen/go/v1 (model=deepseek-v4-flash, timeout=120s)
```

## Configuration (Claude Code)

Point Claude Code at the proxy with two environment variables. The API key
can be any non-empty string — it is never sent to the upstream.

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_API_KEY=dummy

claude
```

If you prefer a per-project setting, drop the same two lines into a
`.envrc` / shell profile / Claude Code settings file.

## Endpoints

| Method | Path           | Purpose                                             |
| ------ | -------------- | --------------------------------------------------- |
| `POST` | `/v1/messages` | Anthropic Messages API. Streams when `stream=true`. |
| `GET`  | `/v1/models`   | Minimal model list (Claude Code discovery).         |
| `GET`  | `/healthz`     | Liveness check.                                     |

## Environment variables

| Variable            | Required | Default                              | Notes                                       |
| ------------------- | -------- | ------------------------------------ | ------------------------------------------- |
| `OPENCODE_API_KEY`  | yes      | —                                    | Bearer token sent to the upstream.          |
| `OPENCODE_BASE_URL` | no       | `https://opencode.ai/zen/go/v1`      | Override to point at a different gateway.   |
| `OPENCODE_MODEL`    | no       | `deepseek-v4-flash`                  | Upstream model. Incoming `model` is ignored.|
| `UPSTREAM_TIMEOUT`  | no       | `120`                                | Seconds before the upstream is abandoned.   |
| `HOST`              | no       | `0.0.0.0`                            | Informational; pass to uvicorn yourself.    |
| `PORT`              | no       | `8080`                               | Informational; pass to uvicorn yourself.    |
| `LOG_LEVEL`         | no       | `info`                               | `debug`, `info`, `warning`, `error`.        |

## Supported request fields

`POST /v1/messages` accepts the following fields. Anything not listed is
currently ignored.

| Anthropic field   | Status      |
| ----------------- | ----------- |
| `model`           | accepted but ignored — always uses the upstream model |
| `messages`        | supported (text, `tool_use`, `tool_result` blocks) |
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
├── translator.py     # Anthropic <-> OpenAI request/response translation
├── stream.py         # OpenAI SSE -> Anthropic SSE translation
├── requirements.txt
├── .env.example
└── README.md
```

`translator.py` is pure data in / data out (no I/O), which makes it easy to
unit-test. `stream.py` consumes an async iterator of upstream lines and yields
Anthropic-formatted SSE strings — no knowledge of FastAPI or httpx leaks in.

## Testing with curl

### Non-streaming

```bash
curl -s http://localhost:8080/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: dummy" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 256,
    "messages": [
      {"role": "user", "content": "Say hello in one short sentence."}
    ]
  }'
```

### Streaming

```bash
curl -N http://localhost:8080/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: dummy" \
  -d '{
    "model": "claude-sonnet-4-6",
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

### Health check

```bash
curl -s http://localhost:8080/healthz
# => {"status":"ok","model":"deepseek-v4-flash","upstream":"https://opencode.ai/zen/go/v1"}
```

### Inspecting model discovery

```bash
curl -s http://localhost:8080/v1/models
```

## Error semantics

- `400 invalid_request_error` — the inbound JSON failed validation.
- `401` / `429` (and any other upstream status) — passthrough, wrapped in
  Anthropic's `{"type":"error","error":{...}}` envelope.
- `502 upstream_error` — the upstream connection failed, timed out, or
  returned non-JSON.
- `504 timeout_error` — explicit timeout from this proxy.

Streaming failures are delivered as a final `event: error` SSE frame so the
client sees a structured error rather than a truncated stream.

## Extending

- **Tool calling**: hook the real `tool_use` / `tool_result` blocks into
  `translator._flatten_content` and add a separate `openai_tool_call_to_anthropic`
  helper. The streaming layer already has hooks (`text_started`, block index)
  for emitting a second content block.
- **Additional providers**: keep `translator.py` pure and add a new
  `*_to_openai` adapter next to it.
- **Caching / rate limiting**: wrap `request.app.state.http` in middleware.

## License

MIT (or whatever you prefer — drop a `LICENSE` file in to lock it in).
