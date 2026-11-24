# kestrel — local development entrypoints
#
# Everything reads .env (copy .env.example first).

COMPOSE ?= docker compose
SERVICE ?= agent-api
# Empty values beat .env in compose precedence. Used for the core stack and for
# tests, so nothing tries to reach Langfuse when it is not running.
NO_TRACING = LANGFUSE_BASE_URL= LANGFUSE_PUBLIC_KEY= LANGFUSE_SECRET_KEY=

# `test`, `lint` and `fmt` run in the built image. Mount the working tree over
# the copy baked at build time: otherwise the suite tests stale code and `fmt`
# rewrites a file inside a throwaway container.
MOUNT_SRC_RO = -v "$(CURDIR)/app/src:/app/src:ro" -v "$(CURDIR)/app/tests:/app/tests:ro"
MOUNT_SRC_RW = -v "$(CURDIR)/app/src:/app/src" -v "$(CURDIR)/app/tests:/app/tests"

.DEFAULT_GOAL := help

.PHONY: help init up up-all down reset logs ps temporal smoke report test lint fmt sandbox psql redis-cli provider-check llm-login llm-check dynatrace-check

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

init: ## Create .env from the example if missing
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

up: init ## Start the core stack: Temporal + API + worker (3 containers)
	$(NO_TRACING) $(COMPOSE) up -d --build
	@echo ""
	@echo "  Agent API     http://localhost:8000/docs"
	@echo "  Temporal UI   http://localhost:8233"
	@echo "  Tracing:      make up-all"
	@echo ""
	@$(COMPOSE) ps

up-all: init ## Start everything, including Langfuse tracing (+5 containers)
	$(COMPOSE) --profile observability up -d --build
	@echo ""
	@echo "  Agent API     http://localhost:8000/docs"
	@echo "  Temporal UI   http://localhost:8233"
	@echo "  Langfuse UI   http://localhost:3000"
	@echo ""
	@$(COMPOSE) --profile observability ps

down: ## Stop the stack, keep volumes
	$(COMPOSE) --profile observability down

reset: ## Stop everything and DELETE all local data (workflow history included)
	$(COMPOSE) --profile observability down -v

logs: ## Follow logs (make logs SERVICE=agent-worker)
	$(COMPOSE) logs -f $(SERVICE)

ps: ## Show service status
	$(COMPOSE) --profile observability ps

temporal: ## List workflows in the local Temporal server
	$(COMPOSE) exec temporal temporal workflow list

smoke: ## Full run against the local stack: start incident, approve, report
	$(COMPOSE) exec $(SERVICE) python -m kestrel.adapters.inbound.cli smoke

# The sentence arrives on stdin, never as a word inside a recipe. A recipe is
# expanded by make before the shell sees it, so a message containing `$(shell …)`
# runs on the host (measured, not theorized), and the shell would then re-split
# its quotes and backticks. Either way the text that *is* the incident identity
# gets rewritten — or executed — before the CLI sees it.
report: ## Start a run from a sentence on stdin: printf '%s' "java-micronaut would not start" | make report
	$(COMPOSE) exec -T $(SERVICE) python -m kestrel.adapters.inbound.cli report -

test: ## Run the test suite against the working tree (works with the stack stopped)
	$(NO_TRACING) $(COMPOSE) run --rm --no-deps $(MOUNT_SRC_RO) agent-worker pytest -q

lint: ## Ruff check against the working tree
	$(NO_TRACING) $(COMPOSE) run --rm --no-deps $(MOUNT_SRC_RO) agent-worker ruff check src tests

fmt: ## Ruff format the working tree
	$(NO_TRACING) $(COMPOSE) run --rm --no-deps $(MOUNT_SRC_RW) agent-worker ruff format src tests

sandbox: ## Run one isolated command: make sandbox CMD="kubectl get pods"
	@test -n "$(CMD)" || (echo 'usage: make sandbox CMD="kubectl get pods -n checkout"' && exit 1)
	$(COMPOSE) run --rm sandbox sh -lc '$(CMD)'

psql: ## psql into the Langfuse database (observability profile)
	$(COMPOSE) --profile observability exec postgres psql -U $${POSTGRES_USER:-postgres} -d langfuse

redis-cli: ## redis-cli against Langfuse's Redis (observability profile)
	$(COMPOSE) --profile observability exec redis redis-cli -a $${REDIS_PASSWORD:-devredis}

provider-check: ## Verify the active telemetry provider and list what it sees
	$(COMPOSE) exec $(SERVICE) python -m kestrel.adapters.inbound.cli provider-check

llm-login: ## Once per machine: authorize your GitHub Copilot seat (device flow)
	$(NO_TRACING) $(COMPOSE) run --rm -it agent-worker python -m kestrel.adapters.inbound.cli llm-login

# The worker, not $(SERVICE): the seat token lives on the worker's volume, and
# the API never reasons.
llm-check: ## Verify the configured LLM answers, and with what credential
	$(COMPOSE) exec agent-worker python -m kestrel.adapters.inbound.cli llm-check

dynatrace-check: ## Deprecated alias of provider-check
	@$(MAKE) --no-print-directory provider-check
