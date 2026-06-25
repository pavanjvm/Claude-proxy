# Claude Code -> OpenCode Go proxy
#
# One-command usage:
#   make run       # bootstrap venv + deps + .env, then start uvicorn in the foreground
#   make bg        # start uvicorn in the background, write logs to proxy/proxy.log
#   make stop      # stop the background uvicorn
#   make status    # show whether the background uvicorn is up + health
#   make test      # curl-based smoke tests against the running proxy
#   make clean     # remove venv, caches, log, .env (asks before destructive steps)
#   make help      # list targets

PROXY_DIR    := proxy
VENV         := $(PROXY_DIR)/.venv
BIN          := $(abspath $(VENV)/bin)
PY           := $(BIN)/python
PIP          := $(BIN)/pip
UVICORN      := $(BIN)/uvicorn
PIDFILE      := $(PROXY_DIR)/.uvicorn.pid
LOGFILE      := $(PROXY_DIR)/proxy.log
HOST         ?= 0.0.0.0
PORT         ?= 8080
ENV_FILE     := $(PROXY_DIR)/.env
ENV_EXAMPLE  := $(PROXY_DIR)/.env.example

.DEFAULT_GOAL := help

.PHONY: help venv install env run bg stop status test clean

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

venv: ## Create the Python virtualenv under proxy/.venv.
	@test -d "$(VENV)" || (cd $(PROXY_DIR) && python3 -m venv .venv)
	@echo "venv ready: $(VENV)"

install: venv ## Install Python dependencies into the venv.
	@$(PIP) install --upgrade pip --quiet
	@$(PIP) install -r $(PROXY_DIR)/requirements.txt --quiet
	@echo "deps installed"

env: ## Create proxy/.env from .env.example if it doesn't exist.
	@if [ ! -f "$(ENV_FILE)" ]; then \
		cp "$(ENV_EXAMPLE)" "$(ENV_FILE)"; \
		echo ""; \
		echo "Created $(ENV_FILE) from .env.example."; \
		echo "==> Edit it and set OPENCODE_API_KEY before using the proxy."; \
	else \
		echo "$(ENV_FILE) already exists — leaving it alone."; \
	fi

run: install env ## Bootstrap + start uvicorn in the foreground.
	@if grep -q "your-opencode-api-key-here" "$(ENV_FILE)" 2>/dev/null; then \
		echo ""; \
		echo "!! OPENCODE_API_KEY is still the placeholder."; \
		echo "!! Edit $(ENV_FILE) and replace it with your real key."; \
		echo ""; \
	fi
	@cd $(PROXY_DIR) && $(UVICORN) app:app --host $(HOST) --port $(PORT)

bg: install env ## Start uvicorn in the background (logs -> proxy.log).
	@if [ -f "$(PIDFILE)" ] && kill -0 `cat $(PIDFILE)` 2>/dev/null; then \
		echo "uvicorn already running (pid `cat $(PIDFILE)`)"; \
		exit 0; \
	fi
	@set +H; cd $(PROXY_DIR) && (nohup $(UVICORN) app:app --host $(HOST) --port $(PORT) > proxy.log 2>&1 & echo $$! > .uvicorn.pid)
	@sleep 1
	@if [ -s "$(PIDFILE)" ] && kill -0 `cat $(PIDFILE)` 2>/dev/null; then \
		echo "uvicorn started (pid `cat $(PIDFILE)`), logs -> $(LOGFILE)"; \
	else \
		echo "uvicorn failed to start; last log lines:"; \
		tail -n 20 $(LOGFILE) || true; \
		exit 1; \
	fi

stop: ## Stop the background uvicorn.
	@if [ -f "$(PIDFILE)" ]; then \
		PID=`cat $(PIDFILE)`; \
		if kill -0 $$PID 2>/dev/null; then \
			kill $$PID && echo "stopped uvicorn (pid $$PID)"; \
		else \
			echo "pid $$PID not running"; \
		fi; \
		rm -f $(PIDFILE); \
	else \
		echo "no pidfile at $(PIDFILE) — nothing to stop"; \
	fi

status: ## Show background uvicorn status + health check.
	@if [ -f "$(PIDFILE)" ] && kill -0 `cat $(PIDFILE)` 2>/dev/null; then \
		echo "uvicorn: running (pid `cat $(PIDFILE)`)"; \
	else \
		echo "uvicorn: not running"; \
		exit 1; \
	fi
	@curl -sS --max-time 3 http://127.0.0.1:$(PORT)/healthz || echo " (health check failed)"

test: ## Curl smoke tests against the running proxy.
	@echo "== /healthz =="
	@curl -sS --max-time 3 http://127.0.0.1:$(PORT)/healthz | sed 's/^/  /'
	@echo ""
	@echo "== /v1/models =="
	@curl -sS --max-time 3 http://127.0.0.1:$(PORT)/v1/models | sed 's/^/  /'
	@echo ""
	@echo "== POST /v1/messages (model=deepseek) =="
	@curl -sS --max-time 30 http://127.0.0.1:$(PORT)/v1/messages \
		-H "content-type: application/json" \
		-H "x-api-key: dummy" \
		-d '{"model":"deepseek","max_tokens":64,"messages":[{"role":"user","content":"Reply with just the word: pong"}]}' \
		| sed 's/^/  /'
	@echo ""
	@echo "== POST /v1/messages (model=minimax) =="
	@curl -sS --max-time 30 http://127.0.0.1:$(PORT)/v1/messages \
		-H "content-type: application/json" \
		-H "x-api-key: dummy" \
		-d '{"model":"minimax","max_tokens":64,"messages":[{"role":"user","content":"Reply with just the word: pong"}]}' \
		| sed 's/^/  /'

clean: ## Remove venv, caches, log, pidfile, .env (asks before deleting .env).
	@rm -rf $(VENV) $(PROXY_DIR)/__pycache__ $(PROXY_DIR)/.uvicorn.pid
	@rm -rf $(PROXY_DIR)/.venv $(PROXY_DIR)/proxy.log
	@find . -name __pycache__ -prune -exec rm -rf {} +
	@if [ -f "$(ENV_FILE)" ]; then \
		read -p "Delete $(ENV_FILE)? [y/N] " ans; \
		case "$$ans" in [yY]*) rm -f "$(ENV_FILE)"; echo "deleted";; *) echo "kept";; esac; \
	fi
	@echo "cleaned"
