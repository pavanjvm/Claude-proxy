# Claude-proxy

Anthropic-compatible proxy that lets **Claude Code** talk to **DeepSeek V4
Flash** through **OpenCode Zen Go's** OpenAI-compatible chat completions API.

See [`proxy/`](proxy/) for the implementation and full documentation.

## Quick start

```bash
cd proxy
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then set OPENCODE_API_KEY
uvicorn app:app --host 0.0.0.0 --port 8080
```

Point Claude Code at it:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_API_KEY=dummy
claude
```

Full curl examples, configuration, and architecture details live in
[`proxy/README.md`](proxy/README.md).
