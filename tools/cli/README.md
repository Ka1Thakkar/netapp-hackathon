# Data Motion CLI

A lightweight command-line interface for interacting with the hackathon prototype services:
- Python API (policy, analytics, recommendations)
- Go Mover (migration orchestrator)
- Web UI (static dashboard, optional)

This CLI lets you:
- Manage datasets (list/create/get/update)
- Post simulated access events
- Fetch recommendations
- Plan migrations (create migration jobs)
- Inspect jobs across services and watch job progress
- Check health across components

Path: tools/cli/data_motion_cli.py

---

## Prerequisites

- Python 3.9+ (tested with 3.11)
- Optional packages (recommended):
  - httpx (async HTTP client, preferred)
  - rich, tabulate (for nicer CLI output; not strictly required)

Without httpx the CLI falls back to requests (if available). If neither is installed, install httpx:

```
pip install httpx
```

---

## Quick Start

1) Start the stack (from repo root):
```
docker compose -f deploy/docker/docker-compose.yml up --build -d
```

2) Verify services:
```
python tools/cli/data_motion_cli.py health
```

3) Create a dataset:
```
python tools/cli/data_motion_cli.py datasets create \
  --name sample-dataset \
  --path-uri file:///shared_storage/ds1 \
  --size-bytes 1048576
```

4) Post an access event:
```
python tools/cli/data_motion_cli.py access post \
  --dataset-id 1 \
  --op read \
  --size-bytes 4096 \
  --client-lat-ms 12.5
```

5) Get recommendations:
```
python tools/cli/data_motion_cli.py recommendations --dataset-id 1
```

6) Plan a migration:
```
python tools/cli/data_motion_cli.py migrate plan \
  --dataset-id 1 \
  --target-location file:///shared_storage/migrated/ds1 \
  --storage-class standard
```

7) List jobs (Python API or Go Mover):
```
python tools/cli/data_motion_cli.py jobs list --source python
python tools/cli/data_motion_cli.py jobs list --source go
```

8) Watch a job progress (polling):
```
python tools/cli/data_motion_cli.py watch job --job-id <uuid> --source python --interval 2
```

---

## Usage

```
python tools/cli/data_motion_cli.py --help
```

Top-level commands:
- health
- datasets (list, create, get, update)
- access post
- recommendations
- migrate plan
- jobs (list, get, retry)
- watch job

Examples:
- List datasets
  ```
  python tools/cli/data_motion_cli.py datasets list
  ```

- Get a dataset
  ```
  python tools/cli/data_motion_cli.py datasets get --dataset-id 1
  ```

- Update a dataset
  ```
  python tools/cli/data_motion_cli.py datasets update \
    --dataset-id 1 \
    --name my-updated-name \
    --owner alice@example.com
  ```

- Retry a failed job (Go Mover)
  ```
  python tools/cli/data_motion_cli.py jobs retry --job-id <uuid>
  ```

- Watch a job
  ```
  python tools/cli/data_motion_cli.py watch job \
    --job-id <uuid> \
    --source python \
    --interval 2 \
    --max-seconds 120
  ```

---

## Environment Variables

- PY_API_BASE (default: http://localhost:8080)
- GO_API_BASE (default: http://localhost:8090)
- WEB_BASE    (default: http://localhost:8082)
- CLI_TIMEOUT (seconds, default: 10)
- CLI_FORMAT  (json|table, default: table)

Example:
```
export PY_API_BASE=http://localhost:8080
export GO_API_BASE=http://localhost:8090
export WEB_BASE=http://localhost:8082
export CLI_FORMAT=json
python tools/cli/data_motion_cli.py health
```

Tip: With `CLI_FORMAT=json`, pipe into jq:
```
python tools/cli/data_motion_cli.py jobs list --source python | jq
```

---

## Exit Codes

- 0: success
- 2: usage/argument error
- 3: network/HTTP failure
- 4: application-level error (e.g., not found)
- 5: unexpected exception/interrupted

---

## Making It Executable (optional)

```
chmod +x tools/cli/data_motion_cli.py
./tools/cli/data_motion_cli.py health
```

On Windows (PowerShell):
```
py tools/cli/data_motion_cli.py health
```

---

## Notes

- The CLI assumes the local Docker stack from `deploy/docker/docker-compose.yml` is running.
- Dataset paths like `file:///shared_storage/...` map to the shared volume mounted in containers.
- The Go Mover `jobs retry` endpoint is only available on the Go service.
- For production or CI usage, consider packaging the CLI and pinning dependencies with a requirements file.

Happy hacking!