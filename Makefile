# Archivist — dev workflow shortcuts.
# Wraps docker compose and the api tooling so common actions are one word.
# Requires: Docker (with the compose plugin) and uv on the host.

# Load .env if present so POSTGRES_* below match the running db container.
ifneq (,$(wildcard .env))
include .env
export
endif

# Fallbacks when .env is absent (mirror docker-compose.yml defaults).
POSTGRES_USER ?= archivist
POSTGRES_DB ?= archivist

# `uv --directory api` runs tooling against the api/ project without cd.
UV := uv --directory api
UV_BENCH := uv --directory benchmark

.DEFAULT_GOAL := help
.PHONY: help up down logs migrate test test-companion test-benchmark test-all lint apikey team psql

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

up: ## Build and start db + api in the background
	docker compose up -d --build

down: ## Stop and remove containers (keeps the pgdata volume)
	docker compose down

logs: ## Follow logs from all services
	docker compose logs -f

migrate: ## Apply DB migrations (alembic upgrade head) inside the api container
	docker compose exec api alembic upgrade head

test: ## Run the test suite on the host (needs Docker for testcontainers)
	$(UV) run pytest

test-companion: ## Run the companion tests (node --test)
	cd companion && node --test

test-benchmark: ## Run the benchmark tests
	$(UV_BENCH) run pytest

test-all: test test-companion test-benchmark ## Run the api, companion and benchmark tests

lint: ## Run ruff lint + format check on the host
	$(UV) run ruff check .
	$(UV) run ruff format --check .

apikey: ## Issue an API key: make apikey TEAM=<id|name> LABEL="<label>" (prints the key once)
ifndef TEAM
	$(error TEAM is required, e.g. `make apikey TEAM=acme LABEL="acme shared"`)
endif
ifndef LABEL
	$(error LABEL is required, e.g. `make apikey TEAM=acme LABEL="acme shared"`)
endif
	docker compose exec api python scripts/create_api_key.py key-issue --team "$(TEAM)" --label "$(LABEL)"

team: ## Create a team: make team NAME=<name>
ifndef NAME
	$(error NAME is required, e.g. `make team NAME=acme`)
endif
	docker compose exec api python scripts/create_api_key.py team-create "$(NAME)"

psql: ## Open a psql shell on the db container
	docker compose exec db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)
