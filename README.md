# Data in Motion — Intelligent Multi-Cloud Data Management (Hackathon)

This repository contains a blueprint and scaffolding for a prototype that dynamically analyzes, tiers, and moves data across storage environments while processing real-time data streams for continuous insights.

This README covers:
- Architecture overview and rationale
- Features and components
- Data model, streaming topics, and placement approach
- APIs (Python and Go) and their responsibilities
- Consistency, failure handling, and security considerations
- Quickstart for local simulation with Docker
- Cloud simulation plan on GCP Free Tier (verify current quotas)
- Roadmap for enhancements

References:
- Problem statement lives in: netapp-hackathon/Problem Statement/

--------------------------------------------------------------------------------

## Architecture Overview

High-level components:
- Python Backend (Policy + Analytics API)
  - REST API for datasets, policies, and recommendations
  - Kafka producers/consumers for access events and recommendations
  - SQLite for metadata, policies, and registry/state
- Go Backend (Data Mover + Orchestrator)
  - High-throughput migration service (pulls jobs from Kafka, executes transfers)
  - Verifies integrity (checksums), switches pointers atomically, and cleans up source
  - Exposes REST for job status and operational metrics
- Data Layer (SQLite)
  - Lightweight, embedded metadata store to track datasets, replicas, policies, jobs, event aggregates, and model snapshots
- Streaming (Kafka-compatible)
  - Topics: access_events, migration_jobs, migration_status, metrics, recommendations
- Storage Abstraction
  - Pluggable drivers: local file system (for simulation), S3-compatible (MinIO), and GCP Cloud Storage (optional if credentials are available)
- Observability
  - Structured logs, basic metrics counters (requests, latencies, job state transitions)
- Web UI and CLI
  - React Web UI served at http://localhost:8082 with panes for Create Dataset (auto-seed), Access Events, Training & Recommendations, Datasets, Jobs, and Metrics
  - Containerized CLI to interact with the APIs (run on demand): docker compose run --rm cli &lt;command&gt;

Data flow summary:
1. Applications publish access events (reads/writes) to Kafka (topic: access_events).
2. Python service consumes events and aggregates usage to classify dataset temperature and predict future access.
3. Python service emits recommended moves to Kafka (topic: migration_jobs) and persists decisions to SQLite.
4. Go mover consumes migration_jobs, performs copy-verify-switch-delete, and emits migration_status updates.
5. Python service updates SQLite with status and serves the dashboard/CLI.

--------------------------------------------------------------------------------

## Features

- Automated data placement decisions based on:
  - Access frequency and recency
  - Latency SLOs and storage throughput characteristics
  - Cost per GB and egress estimates
  - Predicted future access (hot/warm/cold)
- Real-time streaming integration (Kafka)
  - Event-driven recommendations and migrations
  - Go mover now consumes migration_jobs and produces migration_status
- Multi-cloud ready by design
  - Pluggable storage drivers for on-prem (POSIX), S3/MinIO, and GCP Cloud Storage
- Strong consistency during migration
  - Copy → Verify checksum → Atomic pointer switch → Retire old copy
  - Idempotent job handling with exactly-once semantics at application level
  - Status transitions persisted to SQLite
- Policy-aware planning
  - Basic evaluation of latency SLO and max cost policies when choosing storage_class / target location
- Failure injection & resilience
  - Endpoint to inject failures for testing; retry logic scaffolded
- Enhanced metrics
  - In-memory Prometheus-style counters for requests, job status transitions, bytes migrated, failures injected
- Unified interface
  - REST endpoints for policy, datasets, jobs, recommendations, training, migrations
  - Optional CLI or Web UI (React scaffold + static page)

--------------------------------------------------------------------------------

## Repository Layout (implemented)

- netapp-hackathon/Problem Statement/
  - prompts.md (original challenge)
- netapp-hackathon/services/python_api/
  - app/main.py (FastAPI REST, Kafka manager, metrics, failure injection)
  - app/db.py (SQLAlchemy models: datasets, replicas, policies, recommendations, migration_jobs, models)
  - ml/train.py (training script; persists model artifacts + registry row)
  - storage/localfs.py (local filesystem driver)
- netapp-hackathon/services/go_api/
  - main.go (migration mover, Kafka consumer/producer, checksum file copy, simulation)
  - storage.go (SQLite schema + persistence for jobs/datasets)
  - go.mod (includes sqlite + kafka-go)
- netapp-hackathon/deploy/docker/
  - Dockerfile.python (FastAPI image)
  - Dockerfile.go (Go mover multi-stage build)
  - docker-compose.yml (Python API, Go mover, Redpanda, MinIO, Prometheus, Grafana, Console)
  - prometheus/prometheus.yml (scrape config for python_api)
- netapp-hackathon/ui/
  - index.html (minimal UI)
  - react/ (React + Vite scaffold)
- netapp-hackathon/deploy/gcp/
  - README.md & deploy script (Cloud Run / Artifact Registry guidance)

Note: Initial blueprint has been realized; remaining items tracked in Roadmap.

--------------------------------------------------------------------------------

## Data Model (SQLite)

Core tables (minimal viable schema):
- datasets
  - id (PK), name, path_uri, current_tier (hot|warm|cold), latency_slo_ms, size_bytes, owner, created_at, updated_at
- replicas
  - id (PK), dataset_id (FK), location (e.g., local, s3://bucket, gs://bucket), storage_class, checksum, is_primary (bool), created_at, updated_at
- policies
  - id (PK), dataset_id (FK or global), max_cost_per_gb_month, latency_slo_ms, min_redundancy, encryption_required (bool), region_preferences (JSON), created_at, updated_at
- access_aggregates
  - id (PK), dataset_id (FK), window_start, window_end, read_count, write_count, last_access_ts
- recommendations
  - id (PK), dataset_id (FK), recommended_tier, recommended_location, reason (text/json), confidence, created_at
- migration_jobs
  - id (PK), dataset_id (FK), source_uri, dest_uri, dest_storage_class, status (queued|copying|verifying|switching|completed|failed), job_key (unique idempotency key), error, created_at, updated_at
- models
  - id (PK), name, version, algo, params (JSON), metrics (JSON), created_at

--------------------------------------------------------------------------------

## Kafka Topics

- access_events
  - Produced by Python API on /access-events; (future: direct ingestion option)
- migration_jobs
  - Produced by Python API on /plan-migration (policy + heuristic); consumed by Go mover (kafka-go)
- migration_status
  - Produced by Go mover on each state transition; consumed by Python API to persist status in SQLite
- recommendations (future optional)
  - Could emit model-driven recommendations; currently persisted in DB only
- metrics (future optional external topic)
  - Present metrics exposed via /metrics HTTP endpoint instead of topic

--------------------------------------------------------------------------------

## Placement Logic

- Features
  - Aggregated read_count, write_count, bytes_read, bytes_written, last_access_age, variance in inter-arrival, client latency percentiles, current tier, size, cost targets from policy
- Labels / Targets
  - Temperature class: hot | warm | cold
  - Or regression for predicted read rate next window
- Model
  - Default: scikit-learn (e.g., RandomForestClassifier or SGDClassifier for online updates)
  - Persist model params and metrics in SQLite; keep model file on disk within python_api container
- Training cadence
  - Batch retrain daily or on-demand; lightweight online updates after each window
- Inference
  - On each window or on policy changes, produce a recommended tier/location with confidence

--------------------------------------------------------------------------------

## REST API Contracts (Implemented)

Python API
- GET /health
- POST /datasets
- GET /datasets
- GET /datasets/{id}
- PATCH /datasets/{id}
- POST /datasets/{id}/seed
- POST /access-events
- GET /recommendations?dataset_id=...
- POST /policies (policy creation; evaluation applied during /plan-migration)
- GET /policies (list)
- POST /train (invoke ml/train.py)
- GET /events (SSE job status stream)
- POST /plan-migration (policy-aware planning; enqueues migration_jobs)
- GET /jobs (list migration jobs)
- GET /jobs/{job_id}
- POST /jobs/{job_id}/inject-failure (simulate failure for testing)
- GET /metrics (Prometheus-style metrics)

Go API (migration mover)
- GET /health
- GET /jobs (aggregated from memory and SQLite)
- GET /jobs/{id}
- POST /jobs/retry/{id} (reset failed job to queued)

--------------------------------------------------------------------------------

## Consistency, Failures, and Idempotency

- Migration lifecycle
  - copying → verifying → switching → completed|failed (status events persisted)
  - Local file:// paths: streaming copy + SHA-256 checksum validation
  - Future: directory recursion and multi-object verification
- Idempotency
  - Deterministic `job_key` for duplicates; consumer dedupe on JobID/JobKey
- Failure handling
  - Manual injection endpoint for chaos tests
  - Retry endpoint resets failed job to queued
  - Planned: automatic exponential backoff + DLQ
- Data integrity
  - SHA-256 checksum computed and re-validated before switch
  - Planned: ETag / multipart verification for object stores
- Availability
  - Source remains primary until verification succeeds
  - Planned: replica promotion and rollback logic

--------------------------------------------------------------------------------

## Security and Compliance (Optional Enhancements)

- Encryption
  - At-rest: use storage-native encryption for S3/GCS; local simulation via envelope encryption
  - In-flight: TLS for APIs; SASL/SSL for Kafka when available
- Access control
  - API key or OAuth proxy in front of services (out of scope for MVP)
- Auditing
  - Log policy decisions, job transitions, and admin actions

--------------------------------------------------------------------------------

## Observability

- Structured logs (INFO/WARN/ERROR) with job IDs
- Metrics (/metrics): request counts, job status transitions, access event bytes, migrated bytes, failure injections, uptime
- Future: histograms, latency separators, OpenTelemetry traces

--------------------------------------------------------------------------------

## Cloud Simulation (GCP Free Tier)

- Target: Google Cloud Platform (GCP) Always Free for Cloud Storage (limits subject to change)
  - Use a small bucket in an eligible region for realistic e2e tests
  - Verify current free tier quotas and regions in official GCP docs before running tests
- Local alternative for multi-cloud simulation:
  - MinIO (S3-compatible) for a local object store
  - Local filesystem driver for on-prem simulation
- Credentials
  - If using GCP: set `GOOGLE_APPLICATION_CREDENTIALS` to a service account JSON
  - Bucket naming and region selection should respect free tier regions if applicable

--------------------------------------------------------------------------------

## Quickstart (Local, Docker-based)

Prerequisites:
- Docker & Docker Compose

Start the stack (build + up):
```
docker compose -f deploy/docker/docker-compose.yml up --build -d
# or with extras (Prometheus, Grafana, MinIO, Console):
docker compose -f deploy/docker/docker-compose.yml --profile extended up --build -d
```

Core service URLs:
- Web UI: http://localhost:8082
- Python API: http://localhost:8080 (Swagger at /docs)
- Go mover: http://localhost:8090
- Redpanda Console: http://localhost:8081
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin / admin)

End-to-end via Web UI:
1) Open the Web UI at http://localhost:8082
2) Create Dataset: fill name, file:///shared_storage/&lt;name&gt;, size, and check “Auto‑seed” (creation fails if local file creation fails)
3) Access Events: post a read/write event for that dataset (updates last_access_ts immediately)
4) Train Model: run training (robust to small samples; logs available in python_api)
5) Recommendations: optionally filter by dataset id and list recommendations
6) Plan Migration: choose dataset, target location, and storage class; enqueue
7) Observe Jobs: watch live SSE status stream in the UI; details available per job
8) Metrics: check /py/metrics and Nginx status in the UI panel

CLI alternatives (containerized):
- Health: docker compose run --rm cli health
- Create dataset: docker compose run --rm cli dataset create --name ds1 --path file:///shared_storage/ds1 --size 1048576 --auto-seed
- Post access: docker compose run --rm cli access post --dataset 1 --op read --bytes 4096
- Train model: docker compose run --rm cli train
- Plan migration: docker compose run --rm cli migrate plan --dataset 1 --class cold --target file:///shared_storage/ds1_cold

cURL equivalents:
1) Create dataset (auto-seed):
   POST /datasets {"name":"ds1","path_uri":"file:///shared_storage/ds1","size_bytes":1048576,"auto_seed":true}
2) Access event:
   POST /access-events {"dataset_id":1,"op":"read","size_bytes":4096}
3) Train model:
   POST /train
4) List recommendations:
   GET /recommendations?dataset_id=1
5) Plan migration:
   POST /plan-migration {"dataset_id":1,"storage_class":"cold","target_location":"file:///shared_storage/ds1_cold"}
6) Jobs and metrics:
   GET /jobs, GET /jobs/{job_id}, GET /metrics

--------------------------------------------------------------------------------

## Example Usage Flow (End-to-End)

- Web UI path: Create Dataset (auto‑seed) → Access Events → Train Model → List Recommendations → Plan Migration → Observe Jobs → Metrics
- API/CLI path:
  - POST /datasets (with "auto_seed": true) or POST /datasets/{id}/seed
  - POST /access-events
  - POST /train
  - GET /recommendations?dataset_id=...
  - POST /plan-migration
  - GET /jobs and GET /jobs/{id}; live SSE at GET /events
  - GET /metrics

--------------------------------------------------------------------------------

## Storage Drivers (Pluggable)

- LocalFS: file:// paths under a configured root
- S3/MinIO: s3://bucket/prefix using AWS SDKs or MinIO client; for local, MinIO endpoint
- GCS: gs://bucket/prefix using Google Cloud Storage client (used if credentials provided)

--------------------------------------------------------------------------------

## Testing Strategy

- Unit tests
  - Policy engine decisions (Python)
  - Storage driver operations with temp dirs (Go and Python)
- Integration tests
  - Kafka topics wiring: produce events → consume recommendations → complete migration
  - End-to-end: create dataset, simulate traffic, assert resulting tier and location
- Property-based tests (optional) for migration state machine

--------------------------------------------------------------------------------

## Roadmap

Done:
- Dockerfiles & docker-compose (Python API, Go mover, Redpanda, MinIO, Prometheus, Grafana, Console)
- FastAPI + SQLAlchemy models
- Go mover with kafka-go consumer/producer and checksum copy
- Basic policy evaluation (latency / cost) in /plan-migration
- Metrics endpoint & failure injection

Next:
- Persist replica promotion & dataset tier updates on completion
- Advanced policy engine (redundancy, regions, encryption)
- Model inference integration into recommendations (load latest trained model)
- Directory / multi-file migration & partial resume
- Retry / backoff & DLQ for failed jobs
- React UI pages (datasets, jobs, recommendations, policies)
- Authentication & authorization (API keys / OIDC)
- Structured JSON logging + correlation IDs
- More storage drivers (S3/MinIO full, GCS)
- Kubernetes manifests / Helm chart

--------------------------------------------------------------------------------

## Contribution and Next Steps

- Issues / ideas: open tickets describing desired policy rules, storage drivers, or UI components.
- PR suggestions:
  - Enhance policy engine (evaluation module + test cases)
  - Add model inference to /recommendations
  - Expand metrics (histograms, per-endpoint latency)
  - Implement replica promotion logic & dataset tier update on completion
  - Integrate S3/GCS drivers (checksum + multipart)
- Demo script: contribute a synthetic traffic generator (access events + auto-train + plan migrations).

--------------------------------------------------------------------------------

## License

TBD (MIT or Apache-2.0 are suitable defaults for hackathons).
