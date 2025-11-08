# Data in Motion – Live Demo Guide

This guide walks you (the presenter) and observers through demonstrating the hackathon prototype using both:

1. CLI (containerized `data_motion_cli`)
2. Web UI (React SPA behind Nginx reverse proxy)

It also includes a roadmap placeholder for adding real-time streaming (Server-Sent Events / WebSockets) and guidance on instrumenting metrics.

---

## 1. High-Level Architecture Recap

```
Clients / CLI / Web
        │
        │ (HTTP REST)
        v
+--------------------+        Kafka Streams (access_events, migration_jobs, migration_status)
|  Python FastAPI    |<------------------------------------------------------------------+
|  Policy + ML       |-- emits migration_jobs --> Kafka                                 |
|  Metadata SQLite   |-- reads migration_status <-- Kafka (Go mover)                    |
+----------+---------+                                                                  |
           |                                                                            |
           | REST / HTTP                                                                |
           v                                                                            |
+--------------------+                                                                  |
|  Go Migration      |  consumes migration_jobs, simulates copy/verify/switch, emits status
|  Orchestrator      |------------------------------------------------------------------+
+--------------------+
           │
           │ Reverse-proxied via /py and /go
           v
+--------------------+
|  Web UI (Nginx +   |
|  React SPA)        |
+--------------------+
```

- **python_api**: Stores datasets + migration jobs (initial schema) & simulates recommendations.
- **go_mover**: Simulates migration lifecycle transitions, producing status back to Kafka (stub or real depending on DISABLE_KAFKA).
- **web**: Nginx serves React build and proxies `/py/*` → python_api, `/go/*` → go_mover.
- **cli**: Executes common operations quickly for scripted demo.
- **redpanda**: Kafka-compatible broker powering simulated streaming/event flows.

---

## 2. Prerequisites

- Docker & Docker Compose v2 installed.
- Port availability:
  - 8080 (python_api), 8090 (go_mover), 8082 (web), 9092 (Kafka), 9644 (Redpanda admin), 8081 (console if extended).
- Optional: `jq` for formatting JSON.
- OS: Linux / macOS / WSL.

---

## 3. Start the Core Stack

```bash
# From repo root
docker compose -f deploy/docker/docker-compose.yml up --build -d
docker compose -f deploy/docker/docker-compose.yml ps
```

(Extended services: add `--profile extended` to include MinIO, Prometheus, Grafana, Console.)

---

## 4. Quick Health Verification

```bash
curl -s http://localhost:8080/health | jq
curl -s http://localhost:8090/health | jq
curl -s http://localhost:8082/health
docker compose -f deploy/docker/docker-compose.yml run --rm cli health
```

Expected:
- python_api status: ok
- go_mover status: ok
- web health static “OK”
- CLI aggregates health; web may show error if not yet built or running.

---

## 5. Web UI Overview

Navigate to: http://localhost:8082

Initial page displays:
- Build timestamp
- Config base paths for APIs
- Next steps instructions

Talking points:
- Reverse proxy simplifies relative API calls.
- UI currently scaffolded; ready for dashboards (datasets, jobs, streaming).
- Future real-time panel (SSE/WebSocket placeholder described below).

---

## 6. CLI Demonstration Flow (Suggested Order)

### 6.1 Create a Dataset

```bash
docker compose run --rm cli datasets create \
  --name demo-dataset \
  --path-uri file:///shared_storage/demo-dataset \
  --size-bytes 1048576
```

### 6.2 List Datasets

```bash
docker compose run --rm cli datasets list
```

### 6.3 Post Access Event (Simulated reads/writes driving usage patterns)

```bash
docker compose run --rm cli access \
  --dataset-id 1 \
  --op read \
  --size-bytes 8192 \
  --client-lat-ms 11.4
```

Repeat a few times with varied sizes/latencies.

### 6.4 Fetch Recommendations

```bash
docker compose run --rm cli recommendations --dataset-id 1
```

Explain:
- Prototype recommendation logic is heuristic/stub (room for ML model upgrade).
- Schema supports storing tier/location recommendations with confidence scores.

### 6.5 Plan a Migration

```bash
docker compose run --rm cli migrate plan \
  --dataset-id 1 \
  --target-location file:///shared_storage/migrated/demo-dataset \
  --storage-class hot
```

### 6.6 View Migration Jobs

Python API view:
```bash
docker compose run --rm cli jobs list --source python
```

Go mover view:
```bash
docker compose run --rm cli jobs list --source go
```

### 6.7 Watch a Job Progress (Polling)

Copy a job_id from python_api list results.

```bash
docker compose run --rm cli watch job --job-id <job_id> --source go --interval 2
```

Expected transitions (simulated): queued → copying → verifying → switching → completed

### 6.8 (Optional) Retry a Failed Job

If you inject failure (future toggle), then:
```bash
docker compose run --rm cli jobs retry --job-id <failed_job_id>
```

---

## 7. Makefile Shortcuts (If Presenter Prefers)

Ensure `Makefile` is present at repo root.

Examples:
```bash
make up
make seed-dataset
make seed-access
make list-datasets
make list-jobs-go
make cli-run ARGS="recommendations --dataset-id 1"
make health
make down
```

---

## 8. Prometheus & Metrics (Extended Profile)

Enable extended services:
```bash
docker compose -f deploy/docker/docker-compose.yml --profile extended up -d
```

Prometheus targets in `deploy/docker/prometheus/prometheus.yml`:
- python_api: `/metrics`
- web (Nginx stub_status): `/nginx_status`

Grafana (if included): http://localhost:3000 (login admin/admin)

Explain:
- python_api can expose application metrics (requests, latency, job counts).
- Nginx stub_status gives basic active connections; consider exporter for richer metrics.
- Extend: Add streaming consumer lag metrics, job stage counters, recommendation confidence histograms.

---

## 9. Real-Time Dashboard Placeholder (SSE / WebSocket Roadmap)

### Why:
Polling for job status is adequate in a demo but not scalable. Streaming updates allows instant UI reflection of migration stage transitions, access events, and recommendations.

### Option A: Server-Sent Events (SSE)
- Simpler (unidirectional: server → client).
- Endpoint example (python_api or go_mover):
  - `GET /events` returns `Content-Type: text/event-stream`
  - Emits lines like:
    ```
    event: job_status
    data: {"job_id":"<id>","status":"copying","updated_at":1699999999}
    ```
- React integration:
  ```ts
  useEffect(() => {
    const es = new EventSource('/py/events');
    es.addEventListener('job_status', (e) => {
      const payload = JSON.parse(e.data);
      // update local state/redux/query cache
    });
    return () => es.close();
  }, []);
  ```

### Option B: WebSocket
- Full duplex (future: manual control, cancel migrations, push access simulations).
- Endpoint example: `ws://localhost:8080/ws`
- Protocol: JSON messages:
  - Server: `{"type":"job_status","job_id":"...","status":"copying"}`
  - Client: `{"type":"subscribe","stream":"jobs"}`

### Implementation Placeholder Notes:
1. Add an in-memory pub-sub in python_api: on job status change, publish to channel.
2. SSE endpoint listens on that channel, flushes events.
3. For resiliency, keep last N events to replay on reconnect.
4. Web UI: Add a “Live Jobs” panel with colored status badges updated in real-time.

### Security & Scaling Considerations:
- Production: use Kafka consumer group or Redis pub/sub for fan-out.
- Backpressure: SSE auto-reconnect; WebSocket needs heartbeat/ping.
- Authentication: Add token-based gating once multi-tenant concerns arise.

---

## 10. Demo Narrative (Suggested Script)

1. Open with challenge context (multi-cloud intelligent data placement).
2. Show architecture diagram (this guide’s ASCII).
3. Run `make up` (or compose up).
4. Health endpoints confirm services & Kafka.
5. Create dataset; simulate access events.
6. Show recommendations list.
7. Plan migration; inspect jobs in both services.
8. Watch job status streaming (currently polling).
9. Display metrics (optional Prometheus/Grafana).
10. Discuss roadmap: real predictive ML, SSE/WebSockets, cost-based tiering, multi-cloud integration (MinIO → AWS/GCP).
11. Wrap with CLI vs Web synergy and extensibility.

---

## 11. Troubleshooting Cheat Sheet

| Symptom | Likely Cause | Quick Fix |
|---------|--------------|-----------|
| CLI “web unreachable” in health | Web not started | `docker compose up -d web` |
| Nginx 403/502 | Bad proxy path or container not healthy | Check `/py/health`, `/go/health` |
| Go mover not consuming jobs | DISABLE_KAFKA true | Set `DISABLE_KAFKA=false`, rebuild go_mover |
| Python API kafka_ready false | Broker not healthy | `curl -s http://localhost:9644/v1/status/ready` |
| Web build fails (tsc not found) | devDependencies skipped | Ensure Dockerfile.web doesn’t set NODE_ENV=production in build stage |
| “no configuration file provided” | Wrong compose invocation | Use `-f deploy/docker/docker-compose.yml` or set COMPOSE_FILE env var |

---

## 12. Extensibility Roadmap Quick Hits

| Feature | Approach |
|---------|----------|
| Advanced ML predictive tiering | Periodic batch job + feature store (access frequency, latency history, cost map) feeding scikit-learn model |
| Multi-cloud replication | Integrate AWS S3 / GCP Storage SDKs; add replica records & policy engine |
| Policy-driven migrations | Evaluate policies table, trigger jobs when SLA or cost breach predicted |
| Encryption & access control | Pluggable crypto at copy stage (Go mover) + dataset ACL metadata |
| Alerting | Prometheus alert rules or Python background task publishing notifications |
| Real-time UI | SSE/WebSocket integration described above |

---

## 13. Clean Up After Demo

```bash
docker compose -f deploy/docker/docker-compose.yml down
# Or full clean:
docker compose -f deploy/docker/docker-compose.yml down -v
docker image prune -f
```

---

## 14. Fast Reference: Core Commands

| Task | Command |
|------|---------|
| Start stack | `docker compose -f deploy/docker/docker-compose.yml up --build -d` |
| Health (CLI) | `docker compose run --rm cli health` |
| Create dataset | `docker compose run --rm cli datasets create --name demo --path-uri file:///shared_storage/demo --size-bytes 1048576` |
| Access event | `docker compose run --rm cli access --dataset-id 1 --op read --size-bytes 4096` |
| Recommendations | `docker compose run --rm cli recommendations --dataset-id 1` |
| Plan migration | `docker compose run --rm cli migrate plan --dataset-id 1 --target-location file:///shared_storage/migrated/demo --storage-class hot` |
| Jobs (Python) | `docker compose run --rm cli jobs list --source python` |
| Jobs (Go) | `docker compose run --rm cli jobs list --source go` |
| Watch job | `docker compose run --rm cli watch job --job-id <uuid> --source go --interval 2` |

---

## 15. Presenter Tips

- Pre-build images before the live audience to avoid waiting on npm/pip.
- Seed a dataset and at least one migration job before starting screen share—then re-run steps for clarity.
- Use `jq` to format JSON (install if missing).
- Keep a second terminal open with `make logs` to show live transitions.
- Highlight that modules (storage.go / db.py) are intentionally minimal yet extensible.

---

## 16. Placeholder TODO Markers (Searchable)

Use these strings in code to locate upgrade points:

- `TODO:SSE` – planned Server-Sent Events publisher
- `TODO:WS` – WebSocket stream
- `TODO:ML_MODEL_V2` – advanced predictive model hook
- `TODO:CLOUD_PROVIDER_INTEGRATION` – AWS/GCP driver integration
- `TODO:POLICY_ENGINE_EXTEND` – enrichment for cost/latency policies

---

## 17. License / Attribution (Prototype)

Consider adopting Apache-2.0 or MIT for full open-source distribution. Current code serves hackathon demonstration purposes.

---

### End of Demo Guide

Feel free to copy sections into a presentation deck or speaker notes. For any enhancements (auth, multi-tenant isolation, benchmarking), integrate them incrementally and update this document.
