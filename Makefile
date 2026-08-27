.DEFAULT_GOAL := help
PY := uv run

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	uv sync

up: ## Start Postgres + Milvus
	docker compose up -d

down: ## Stop containers
	docker compose down

mcp: ## Run the Grafana MCP server (foreground, port 8005)
	$(PY) python mcp_servers/grafana_server.py

api: ## Run the HTTP service + web UI
	$(PY) oncall-api

slackd: ## Run the Slack bot (Socket Mode)
	$(PY) oncall-slackd

evidence: ## Deterministic floor only, no model, no keys needed
	$(PY) oncall evidence "[FIRING] news-list-for-channel p99 app=server-feed"

test: ## Run the suite
	$(PY) pytest -q

lint: ## ruff + import contracts
	$(PY) ruff check app tests
	$(PY) lint-imports

check: lint test ## Everything CI runs

.PHONY: help install up down mcp api slackd evidence test lint check
