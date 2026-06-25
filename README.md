# Claude-proxy

Anthropic-compatible proxy that lets **Claude Code** talk to multiple models
on **OpenCode Zen Go's** OpenAI-compatible chat completions API — including
DeepSeek V4 Flash and MiniMax M3, with more aliases configurable by env var.

See [`proxy/`](proxy/) for the implementation and full documentation.

## One-command quick start

```bash
make bg
```

That creates the venv, installs deps, sets up `.env`, and starts the proxy
in the background. It'll print the exact exports you need for Claude Code.

Then in **any other terminal**:

```bash
eval "$(make claude-env)"
claude
```

Inside Claude Code, use `/model` to switch between `deepseek` and `minimax`.

### Other commands

| Command          | What it does                              |
| ---------------- | ----------------------------------------- |
| `make run`       | Foreground mode (Ctrl-C to stop)          |
| `make status`    | Is the proxy up? + `/healthz` output      |
| `make test`      | Curl smoke tests against the running proxy |
| `make stop`      | Stop the background proxy                 |
| `make claude-env`| Print the ANTHROPIC_* exports to set     |
| `make clean`     | Wipe venv, logs, caches                   |

Prefer shell? `./run.sh` is a drop-in equivalent of every command above:

```bash
./run.sh bg
./run.sh status
./run.sh test
./run.sh stop
./run.sh claude-env
```

## Manual setup

If you'd rather not use the scripts:

```bash
cd proxy
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then set OPENCODE_API_KEY
uvicorn app:app --host 0.0.0.0 --port 8080
```

Then in another terminal:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_API_KEY=dummy
claude
```

Full curl examples, configuration, and architecture details live in
[`proxy/README.md`](proxy/README.md).
