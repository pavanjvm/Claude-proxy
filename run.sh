#!/usr/bin/env bash
# run.sh — one-command launcher for the Claude -> OpenCode Go proxy.
#
#   ./run.sh           # start in the foreground
#   ./run.sh bg        # start in the background, write logs to proxy/proxy.log
#   ./run.sh stop      # stop the background instance
#   ./run.sh status    # show pid + health
#   ./run.sh test      # curl-based smoke tests
#   ./run.sh help      # show usage

set -euo pipefail

# Resolve the directory containing this script so run.sh works from any cwd.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

PROXY_DIR="$SCRIPT_DIR/proxy"
VENV="$PROXY_DIR/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
UVICORN="$VENV/bin/uvicorn"
PIDFILE="$PROXY_DIR/.uvicorn.pid"
LOGFILE="$PROXY_DIR/proxy.log"
ENV_FILE="$PROXY_DIR/.env"
ENV_EXAMPLE="$PROXY_DIR/.env.example"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"

cmd="${1:-run}"

ensure_venv() {
  if [ ! -d "$VENV" ]; then
    echo ">> creating venv at $VENV"
    ( cd "$PROXY_DIR" && python3 -m venv .venv )
  fi
}

ensure_deps() {
  echo ">> installing requirements"
  "$PIP" install --upgrade pip --quiet
  "$PIP" install -r "$PROXY_DIR/requirements.txt" --quiet
}

ensure_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    echo ""
    echo "!! Created $ENV_FILE from .env.example."
    echo "!! Edit it and set OPENCODE_API_KEY before continuing."
    echo ""
  fi
}

warn_if_placeholder_key() {
  if grep -q "your-opencode-api-key-here" "$ENV_FILE" 2>/dev/null; then
    echo ""
    echo "!! OPENCODE_API_KEY is still the placeholder."
    echo "!! Edit $ENV_FILE and replace it with your real key."
    echo ""
  fi
}

start_bg() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "uvicorn already running (pid $(cat "$PIDFILE"))"
    return 0
  fi
  # `set +H` disables bash history expansion so `$!` is treated as
  # "last background pid" instead of being clobbered.
  set +H
  ( cd "$PROXY_DIR" && nohup "$UVICORN" app:app --host "$HOST" --port "$PORT" \
      > proxy.log 2>&1 & echo $! > .uvicorn.pid )
  sleep 1
  if [ -s "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "uvicorn started (pid $(cat "$PIDFILE")), logs -> $LOGFILE"
  else
    echo "uvicorn failed to start; last log lines:"
    tail -n 20 "$LOGFILE" 2>/dev/null || true
    return 1
  fi
}

stop_bg() {
  if [ ! -f "$PIDFILE" ]; then
    echo "no pidfile at $PIDFILE — nothing to stop"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" && echo "stopped uvicorn (pid $pid)"
  else
    echo "pid $pid not running"
  fi
  rm -f "$PIDFILE"
}

status() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "uvicorn: running (pid $(cat "$PIDFILE"))"
  else
    echo "uvicorn: not running"
    return 1
  fi
  curl -sS --max-time 3 "http://127.0.0.1:$PORT/healthz" || echo " (health check failed)"
}

do_test() {
  echo "== /healthz =="
  curl -sS --max-time 3 "http://127.0.0.1:$PORT/healthz" | sed 's/^/  /'
  echo
  echo "== /v1/models =="
  curl -sS --max-time 3 "http://127.0.0.1:$PORT/v1/models" | sed 's/^/  /'
  echo
  echo "== POST /v1/messages (model=deepseek) =="
  curl -sS --max-time 30 "http://127.0.0.1:$PORT/v1/messages" \
    -H "content-type: application/json" \
    -H "x-api-key: dummy" \
    -d '{"model":"deepseek","max_tokens":64,"messages":[{"role":"user","content":"Reply with just the word: pong"}]}' \
    | sed 's/^/  /'
  echo
  echo "== POST /v1/messages (model=minimax) =="
  curl -sS --max-time 30 "http://127.0.0.1:$PORT/v1/messages" \
    -H "content-type: application/json" \
    -H "x-api-key: dummy" \
    -d '{"model":"minimax","max_tokens":64,"messages":[{"role":"user","content":"Reply with just the word: pong"}]}' \
    | sed 's/^/  /'
}

usage() {
  cat <<EOF
Usage: $0 [command]

Commands:
  run     Start uvicorn in the foreground (default)
  bg      Start uvicorn in the background (logs -> $LOGFILE)
  stop    Stop the background uvicorn
  status  Show pid + health check
  test    Run curl smoke tests against the running proxy
  help    Show this message

Env overrides:
  HOST=0.0.0.0 PORT=8080
EOF
}

case "$cmd" in
  run)
    ensure_venv
    ensure_deps
    ensure_env
    warn_if_placeholder_key
    ( cd "$PROXY_DIR" && exec "$UVICORN" app:app --host "$HOST" --port "$PORT" )
    ;;
  bg)
    ensure_venv
    ensure_deps
    ensure_env
    warn_if_placeholder_key
    start_bg
    ;;
  stop)
    stop_bg
    ;;
  status)
    status
    ;;
  test)
    do_test
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: $cmd"
    usage
    exit 1
    ;;
esac
