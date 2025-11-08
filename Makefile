# Makefile - Data in Motion Hackathon
# Common build/run/development shortcuts for docker-compose stack and local tooling.
#
# Usage:
#   make help
#   make up
#   make web-build
#
# Override variables inline:
#   make up LOG_LEVEL=DEBUG
#
# Requires Docker + Compose v2.

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
COMPOSE_FILE         ?= deploy/docker/docker-compose.yml
DC                   ?= docker compose -f $(COMPOSE_FILE)

# Default environment overrides passed to some run targets
LOG_LEVEL            ?= INFO
PY_API_PORT          ?= 8080
GO_MOVER_PORT        ?= 8090
WEB_PORT             ?= 8082

# Curl command (fallback if not installed)
CURL                 ?= curl -fsS

# A sample dataset name used by seed targets
DATASET_NAME         ?= sample-dataset
DATASET_PATH_URI     ?= file:///shared_storage/$(DATASET_NAME)
DATASET_SIZE_BYTES   ?= 1048576

# Colors (disable by setting NO_COLOR=1)
NO_COLOR             ?= 0
ifeq ($(NO_COLOR),0)
  GREEN=\033[32m
  YELLOW=\033[33m
  BLUE=\033[34m
  BOLD=\033[1m
  RESET=\033[0m
else
  GREEN=
  YELLOW=
  BLUE=
  BOLD=
  RESET=
endif

# -------------------------------------------------------------------
# Phony Targets
# -------------------------------------------------------------------
.PHONY: help up up-ext down down-v ps logs build build-all \
        rebuild-go rebuild-py rebuild-web rebuild-cli web-build \
        health health-cli seed-dataset seed-access list-datasets \
        list-jobs-go list-jobs-py cli-run cli-shell py-shell go-shell \
        web-shell prune docker-info lint-go lint-ui clean

# -------------------------------------------------------------------
# Help
# -------------------------------------------------------------------
help:
	@echo "$(BOLD)Data in Motion Hackathon Make Targets$(RESET)"
	@echo ""
	@echo "$(GREEN)Stack Lifecycle$(RESET)"
	@echo "  make up           - Build & start core services (redpanda, python_api, go_mover, web, cli)"
	@echo "  make up-ext       - Start stack including extended profile (minio, console, prometheus, grafana)"
	@echo "  make down         - Stop containers"
	@echo "  make down-v       - Stop and remove volumes (CAUTION: metadata loss)"
	@echo "  make ps           - List running containers"
	@echo "  make logs         - Tail all service logs"
	@echo ""
	@echo "$(GREEN)Build / Rebuild$(RESET)"
	@echo "  make build-all    - Build all service images"
	@echo "  make rebuild-go   - Rebuild Go mover service image"
	@echo "  make rebuild-py   - Rebuild Python API image"
	@echo "  make rebuild-web  - Rebuild Web UI image"
	@echo "  make rebuild-cli  - Rebuild CLI image"
	@echo "  make web-build    - Build web only (Data Motion UI)"
	@echo ""
	@echo "$(GREEN)Health / Inspection$(RESET)"
	@echo "  make health       - Curl health endpoints (python_api, go_mover, web)"
	@echo "  make health-cli   - Run CLI health command in ephemeral container"
	@echo "  make docker-info  - Show Docker system info"
	@echo ""
	@echo "$(GREEN)CLI Convenience$(RESET)"
	@echo "  make cli-run ARGS='datasets list'    - Run CLI with custom subcommand"
	@echo "  make cli-shell                       - Shell into a transient CLI container"
	@echo ""
	@echo "$(GREEN)Shell Access$(RESET)"
	@echo "  make py-shell     - /bin/bash into python_api"
	@echo "  make go-shell     - /bin/sh into go_mover"
	@echo "  make web-shell    - /bin/sh into web"
	@echo ""
	@echo "$(GREEN)Demo / Data Seeding$(RESET)"
	@echo "  make seed-dataset - Create sample dataset via Python API"
	@echo "  make seed-access  - Post a sample access event"
	@echo "  make list-datasets- List datasets (Python API)"
	@echo "  make list-jobs-py - List migration jobs (Python API)"
	@echo "  make list-jobs-go - List migration jobs (Go mover)"
	@echo ""
	@echo "$(GREEN)Lint / Cleanup$(RESET)"
	@echo "  make lint-go      - go vet + staticcheck (if installed) on go_api"
	@echo "  make lint-ui      - npm run lint for React UI"
	@echo "  make prune        - Remove dangling Docker images"
	@echo "  make clean        - Remove build artifacts (frontend dist)"
	@echo ""
	@echo "Override ports: make up WEB_PORT=8098 PY_API_PORT=8088"
	@echo ""

# -------------------------------------------------------------------
# Lifecycle
# -------------------------------------------------------------------
up:
	$(DC) up --build -d

up-ext:
	$(DC) --profile extended up --build -d

down:
	$(DC) down

down-v:
	$(DC) down -v

ps:
	$(DC) ps

logs:
	$(DC) logs -f

# -------------------------------------------------------------------
# Builds
# -------------------------------------------------------------------
build-all:
	$(DC) build

rebuild-go:
	$(DC) build --no-cache go_mover

rebuild-py:
	$(DC) build --no-cache python_api

rebuild-web:
	$(DC) build --no-cache web

rebuild-cli:
	$(DC) build --no-cache cli

web-build:
	@echo "$(BLUE)Building web UI image...$(RESET)"
	$(DC) build web

# -------------------------------------------------------------------
# Health / Inspection
# -------------------------------------------------------------------
health:
	@echo "$(BLUE)Python API health$(RESET)"
	@$(CURL) http://localhost:$(PY_API_PORT)/health || true
	@echo "$(BLUE)Go mover health$(RESET)"
	@$(CURL) http://localhost:$(GO_MOVER_PORT)/health || true
	@echo "$(BLUE)Web UI health (static)$(RESET)"
	@$(CURL) http://localhost:$(WEB_PORT)/health || true

health-cli:
	$(DC) run --rm cli health

docker-info:
	docker info

# -------------------------------------------------------------------
# CLI Convenience
# -------------------------------------------------------------------
# Usage: make cli-run ARGS="datasets list"
cli-run:
	@if [ -z "$(ARGS)" ]; then echo "Specify ARGS=\"<cli subcommand>\""; exit 2; fi
	$(DC) run --rm cli $(ARGS)

cli-shell:
	$(DC) run --rm --entrypoint /bin/bash cli

# -------------------------------------------------------------------
# Shell Access
# -------------------------------------------------------------------
py-shell:
	docker exec -it python_api /bin/bash

go-shell:
	docker exec -it go_mover /bin/sh

web-shell:
	docker exec -it web /bin/sh

# -------------------------------------------------------------------
# Demo / Data Seeding
# -------------------------------------------------------------------
seed-dataset:
	@echo "$(YELLOW)Creating dataset $(DATASET_NAME)...$(RESET)"
	@$(CURL) -X POST http://localhost:$(PY_API_PORT)/datasets \
	  -H 'Content-Type: application/json' \
	  -d '{"name":"$(DATASET_NAME)","path_uri":"$(DATASET_PATH_URI)","size_bytes":$(DATASET_SIZE_BYTES)}' || true

seed-access:
	@echo "$(YELLOW)Posting access event for dataset id=1 (adjust as needed)...$(RESET)"
	@$(CURL) -X POST http://localhost:$(PY_API_PORT)/access-events \
	  -H 'Content-Type: application/json' \
	  -d '{"dataset_id":1,"op":"read","size_bytes":4096,"client_lat_ms":12.3}' || true

list-datasets:
	@$(CURL) http://localhost:$(PY_API_PORT)/datasets || true

list-jobs-py:
	@$(CURL) http://localhost:$(PY_API_PORT)/jobs || true

list-jobs-go:
	@$(CURL) http://localhost:$(GO_MOVER_PORT)/jobs || true

# -------------------------------------------------------------------
# Lint / Static Analysis
# -------------------------------------------------------------------
lint-go:
	@echo "$(BLUE)Go vet...(RESET)"
	@(cd services/go_api && go vet ./... || true)
	@if command -v staticcheck >/dev/null 2>&1; then \
	  echo "$(BLUE)staticcheck...$(RESET)"; \
	  (cd services/go_api && staticcheck ./... || true); \
	else \
	  echo "staticcheck not installed (skip)"; \
	fi

lint-ui:
	@echo "$(BLUE)UI lint...(RESET)"
	@(cd ui/react && npm install --no-audit --no-fund && npm run lint || true)

# -------------------------------------------------------------------
# Cleanup
# -------------------------------------------------------------------
prune:
	docker image prune -f

clean:
	@echo "$(BLUE)Removing frontend dist output...$(RESET)"
	@rm -rf ui/react/dist
