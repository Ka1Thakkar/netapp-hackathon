# Data in Motion – Docker / Compose Ops Guide

This document provides Makefile-style instructions and common operational workflows
for building, running, and iterating on the prototype stack defined in `docker-compose.yml`.

Services:
- redpanda        (Kafka-compatible broker)
- python_api      (FastAPI policy + analytics + ML scaffolding)
- go_mover        (Go migration orchestrator simulation)
- web             (React SPA + Nginx reverse proxy to APIs)
- cli             (Containerized CLI utility)
- minio (optional, profile=extended)
- redpanda_console (optional, profile=extended)

Volumes:
- python_data        (SQLite metadata persistence)
- shared_storage     (simulated on-prem/local filesystem)
- minio_data         (object store data)

Profiles:
- core (implicit): redpanda, python_api, go_mover
- extended: adds minio and redpanda_console

--------------------------------------------------------------------------------
## 0. Repository Root Assumption

All commands below assume your shell working directory is the repository root:
`netapp-hackathon/`

Compose file path: `deploy/docker/docker-compose.yml`

If you prefer shorthand:
```
export COMPOSE_FILE=deploy/docker/docker-compose.yml
```

--------------------------------------------------------------------------------
## 1. Quick Start

Bring up core stack (build images if missing):
```
docker compose -f deploy/docker/docker-compose.yml up --build -d
```

Check container status:
```
docker compose -f deploy/docker/docker-compose.yml ps
```

Tail logs for all services:
```
docker compose -f deploy/docker/docker-compose.yml logs -f
```

Stop stack:
```
docker compose -f deploy/docker/docker-compose.yml down
```

Stop + remove volumes (CAUTION: metadata loss):
```
docker compose -f deploy/docker/docker-compose.yml down -v
```

--------------------------------------------------------------------------------
## 2. Extended Profile (MinIO + Console)

Enable extended services:
```
docker compose -f deploy/docker/docker-compose.yml --profile extended up -d
```

Disable extended (removes their containers):
```
docker compose -f deploy/docker/docker-compose.yml --profile extended down
```

List active services including profile:
```
docker compose -f deploy/docker/docker-compose.yml ls
```

--------------------------------------------------------------------------------
## 3. Build Targets (Manual)

Build Python API image:
```
docker build -f deploy/docker/Dockerfile.python -t python-api:dev .
```

Build Go mover image:
```
docker build -f deploy/docker/Dockerfile.go -t go-mover:dev .
```

Build Web UI image:
```
docker build -f deploy/docker/Dockerfile.web -t data-motion-web:dev .
```

Build CLI image:
```
docker build -f deploy/docker/Dockerfile.cli -t data-motion-cli:dev .
```

Multi-arch build example (requires buildx configured):
```
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f deploy/docker/Dockerfile.python \
  -t yourrepo/python-api:multiarch --push .
```

--------------------------------------------------------------------------------
## 4. Health Checks

Python API:
```
curl -s http://localhost:8080/health | jq
```

Go mover:
```
curl -s http://localhost:8090/health | jq
```

Web (Nginx + SPA):
```
curl -s http://localhost:8082/health
```

CLI (container):
```
docker compose -f deploy/docker/docker-compose.yml run --rm cli health
```

Redpanda Admin:
```
curl -s http://localhost:9644/v1/status/ready
```

### CLI Usage

The containerized CLI (`cli` service) wraps common API operations. You can run it on demand (preferred) or attach to the long‑running container if you change the compose command.

Run a one‑off command (image builds transparently if needed):
```
docker compose -f deploy/docker/docker-compose.yml run --rm cli datasets list
```

Examples:
```
# Health across services
docker compose run --rm cli health

# Create a dataset
docker compose run --rm cli datasets create \
  --name sample-dataset \
  --path-uri file:///shared_storage/ds1 \
  --size-bytes 1048576

# Post an access event
docker compose run --rm cli access post \
  --dataset-id 1 \
  --op read \
  --size-bytes 4096 \
  --client-lat-ms 12.3

# Plan a migration
docker compose run --rm cli migrate plan \
  --dataset-id 1 \
  --target-location file:///shared_storage/migrated/ds1 \
  --storage-class standard

# List jobs from Python API or Go mover
docker compose run --rm cli jobs list --source python
docker compose run --rm cli jobs list --source go

# Watch a job until terminal state
docker compose run --rm cli watch job --job-id <uuid> --source python --interval 2

# Retry a failed job (Go mover only)
docker compose run --rm cli jobs retry --job-id <uuid>
```

Shell into the CLI container if you changed its command to keep it running:
```
docker exec -it data_motion_cli /bin/bash
python /cli/data_motion_cli.py --help
```

Tip: Set output format to JSON:
```
docker compose run --rm -e CLI_FORMAT=json cli jobs list --source python | jq
```

MinIO (if enabled):
```
curl -s http://localhost:9000/minio/health/live
```

Redpanda Console (if enabled):
Open browser: http://localhost:8081

--------------------------------------------------------------------------------
## 5. Common Workflows

### 5.1 Simulate Access Events
Create a dataset:
```
curl -X POST http://localhost:8080/datasets \
  -H 'Content-Type: application/json' \
  -d '{"name":"sample-dataset","path_uri":"file:///shared_storage/ds1","size_bytes":1048576}'
```

List datasets:
```
curl -s http://localhost:8080/datasets | jq
```

Post access event:
```
curl -X POST http://localhost:8080/access-events \
  -H 'Content-Type: application/json' \
  -d '{"dataset_id":1,"op":"read","size_bytes":4096,"client_lat_ms":12.3}'
```

Get tier recommendations:
```
curl -s http://localhost:8080/recommendations?dataset_id=1 | jq
```

### 5.2 Plan Migration
```
curl -X POST http://localhost:8080/plan-migration \
  -H 'Content-Type: application/json' \
  -d '{"dataset_id":1,"target_location":"file:///shared_storage/migrated/ds1","storage_class":"standard"}' | jq
```

View jobs (Python side):
```
curl -s http://localhost:8080/jobs | jq
```

View jobs (Go mover side):
```
curl -s http://localhost:8090/jobs | jq
```

### 5.3 Retry Failed Job (Go mover)
```
curl -X POST http://localhost:8090/jobs/retry/<job_id>
```

--------------------------------------------------------------------------------
## 6. Live Code Iteration

Python API (reload disabled in container by default):
- For rapid dev: run locally with virtualenv:
  ```
  pip install fastapi uvicorn aiokafka pydantic
  uvicorn services.python_api.app.main:app --reload --port 8080
  ```
- Keep Kafka broker in Docker; set `KAFKA_BROKERS=localhost:9092`.

Go mover (local run):
```
go run services/go_api/main.go
```

To attach shell to a running container:
```
docker exec -it python_api /bin/bash
docker exec -it go_mover /bin/sh
```

--------------------------------------------------------------------------------
## 7. Environment Overrides

Override at run-time:
```
docker compose -f deploy/docker/docker-compose.yml run -e LOG_LEVEL=DEBUG python_api
```

Persist custom .env (optional):
Create `deploy/docker/.env`:
```
LOG_LEVEL=DEBUG
JOB_CONSUMER_POLL_INTERVAL_MS=250
```
Compose automatically loads it if placed next to compose file (depending on CLI version).

--------------------------------------------------------------------------------
## 8. Data & Volume Management

Inspect volumes:
```
docker volume ls | grep netapp-hackathon
```

Backup SQLite:
```
docker cp python_api:/data/metadata.db ./backup_metadata.db
```

Clean shared storage (CAUTION):
```
docker compose -f deploy/docker/docker-compose.yml exec python_api bash -c 'rm -rf /shared_storage/*'
```

--------------------------------------------------------------------------------
## 9. Troubleshooting

Issue: Kafka connection errors in Python API
- Ensure redpanda is healthy: `curl -s http://localhost:9644/v1/status/ready`
- Check logs: `docker compose -f deploy/docker/docker-compose.yml logs redpanda`
- Python service may downgrade to NullKafka; confirm via `/health` field `kafka_ready`.

Issue: Go mover not consuming jobs (stub mode)
- `DISABLE_KAFKA=true` forces stub; set to false and rebuild.
- Real Kafka integration pending; stub only logs transitions.

Issue: Ports already in use
- Adjust host ports in compose or stop conflicting services (`lsof -i :8080`).

--------------------------------------------------------------------------------
## 10. Cleanup

Remove all containers (keep volumes):
```
docker compose -f deploy/docker/docker-compose.yml down
```

Remove containers + volumes:
```
docker compose -f deploy/docker/docker-compose.yml down -v
```

Remove dangling images:
```
docker image prune -f
```

--------------------------------------------------------------------------------
## 11. Future Targets (Makefile Suggestion)

A Makefile could wrap common tasks:

```
# Example snippet (not yet added):
up:
\tdocker compose -f deploy/docker/docker-compose.yml up --build -d

down:
\tdocker compose -f deploy/docker/docker-compose.yml down

logs:
\tdocker compose -f deploy/docker/docker-compose.yml logs -f

python-shell:
\tdocker exec -it python_api /bin/bash

go-shell:
\tdocker exec -it go_mover /bin/sh
```

--------------------------------------------------------------------------------
## 12. Security Notes (Local Dev)

- Default credentials for MinIO are insecure; rotate if exposed.
- Store cloud credentials (GCP, AWS) outside the repo; mount as read-only secret volume.
- Add TLS / SASL for Kafka if moving beyond local simulation.

--------------------------------------------------------------------------------
## 13. License & Attribution

Prototype code for hackathon use; consider Apache-2.0 or MIT for final licensing.

--------------------------------------------------------------------------------
Happy building!