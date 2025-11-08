#!/usr/bin/env python3
"""
Data Motion CLI
================

A lightweight command-line interface for interacting with the Data in Motion
hackathon prototype services (Python API, Go mover, and Web UI).

Operations Covered:
- Datasets: list, create, get, update
- Access events: post simulated read/write activity
- Recommendations: fetch data tier/location suggestions
- Migration planning: create migration jobs
- Jobs: list / get from Python API and Go mover service
- Retry failed job (Go mover)
- Health: check health status of all services
- Watch: stream job status transitions (polling)
- Web: open dashboard (if running)

Environment Configuration:
- PY_API_BASE (default: http://localhost:8080)
- GO_API_BASE (default: http://localhost:8090)
- WEB_BASE   (default: http://localhost:8082)
- CLI_TIMEOUT (seconds, default: 10)
- CLI_FORMAT (json|table, default: table)

Examples:
    python data_motion_cli.py health
    python data_motion_cli.py datasets list
    python data_motion_cli.py datasets create --name sample --path-uri file:///shared_storage/sample --size-bytes 1048576
    python data_motion_cli.py access post --dataset-id 1 --op read --size-bytes 4096 --client-lat-ms 12.5
    python data_motion_cli.py recommendations list --dataset-id 1
    python data_motion_cli.py migrate plan --dataset-id 1 --target-location file:///shared_storage/migrated/sample --storage-class standard
    python data_motion_cli.py jobs list --source python
    python data_motion_cli.py jobs list --source go
    python data_motion_cli.py jobs get --source python --job-id <uuid>
    python data_motion_cli.py jobs retry --job-id <uuid>
    python data_motion_cli.py watch job --job-id <uuid> --interval 2 --source python

Exit Codes:
    0  success
    2  usage / argument error
    3  network / HTTP failure
    4  application-level error (e.g., not found)
    5  unexpected exception

Dependencies:
    - Tries to use httpx (async capable). Falls back to requests if httpx not installed.
      Install extras: pip install httpx rich tabulate (optional enhancements).

Future Enhancements:
    - Colorized output
    - Kafka direct consumption
    - Authentication tokens
    - Bulk operations / scripting mode

License: Prototype code (choose suitable license for final release).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import textwrap
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

# ------------------------------------------------------------------------------
# HTTP Client Abstraction (httpx preferred; fallback to requests)
# ------------------------------------------------------------------------------
try:
    import httpx  # type: ignore

    _USE_HTTPX = True
except ImportError:  # pragma: no cover
    import requests  # type: ignore

    _USE_HTTPX = False


# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------
PY_API_BASE = os.getenv("PY_API_BASE", "http://localhost:8080").rstrip("/")
GO_API_BASE = os.getenv("GO_API_BASE", "http://localhost:8090").rstrip("/")
WEB_BASE = os.getenv("WEB_BASE", "http://localhost:8082").rstrip("/")
CLI_TIMEOUT = float(os.getenv("CLI_TIMEOUT", "10"))
CLI_FORMAT = os.getenv("CLI_FORMAT", "table").lower()


# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------
def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def format_table(
    rows: List[Dict[str, Any]], columns: Optional[List[str]] = None
) -> str:
    if not rows:
        return "(no results)"
    if columns is None:
        # Derive union of keys preserving insertion order of first row
        columns = list(rows[0].keys())
        seen = set(columns)
        for r in rows[1:]:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    columns.append(k)

    # Compute column widths
    widths = {
        col: max(len(str(col)), *(len(str(r.get(col, ""))) for r in rows))
        for col in columns
    }
    sep = " | "
    header = sep.join(f"{col:<{widths[col]}}" for col in columns)
    divider = "-+-".join("-" * widths[col] for col in columns)
    lines = [header, divider]
    for r in rows:
        line = sep.join(f"{str(r.get(col, '')):<{widths[col]}}" for col in columns)
        lines.append(line)
    return "\n".join(lines)


def pretty_output(data: Any, single: bool = False) -> None:
    if CLI_FORMAT == "json":
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    # Table format or fallback
    if single and isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list) and all(isinstance(x, dict) for x in data):
        rows = data
    else:
        print(json.dumps(data, indent=2))
        return
    print(format_table(rows))


def parse_int(val: Optional[str]) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def exit_with(code: int, msg: Optional[str] = None) -> None:
    if msg:
        eprint(msg)
    sys.exit(code)


# ------------------------------------------------------------------------------
# Data Classes (lightweight local representations)
# ------------------------------------------------------------------------------
@dataclass
class Dataset:
    id: int
    name: str
    path_uri: str
    current_tier: Optional[str] = None
    size_bytes: Optional[int] = None
    owner: Optional[str] = None

    @staticmethod
    def from_api(obj: Dict[str, Any]) -> "Dataset":
        return Dataset(
            id=obj.get("id"),
            name=obj.get("name"),
            path_uri=obj.get("path_uri"),
            current_tier=obj.get("current_tier"),
            size_bytes=obj.get("size_bytes"),
            owner=obj.get("owner"),
        )


@dataclass
class MigrationJob:
    job_id: str
    dataset_id: int
    source_uri: str
    dest_uri: Optional[str]
    dest_storage_class: Optional[str]
    status: str
    error: Optional[str]
    submitted_at: Any
    updated_at: Any

    @staticmethod
    def from_api(obj: Dict[str, Any]) -> "MigrationJob":
        return MigrationJob(
            job_id=obj.get("job_id") or obj.get("id"),
            dataset_id=obj.get("dataset_id"),
            source_uri=obj.get("source_uri"),
            dest_uri=obj.get("dest_uri"),
            dest_storage_class=obj.get("dest_storage_class"),
            status=obj.get("status"),
            error=obj.get("error"),
            submitted_at=obj.get("submitted_at") or obj.get("submitted_at_ms"),
            updated_at=obj.get("updated_at") or obj.get("updated_at_ms"),
        )


# ------------------------------------------------------------------------------
# HTTP Operations
# ------------------------------------------------------------------------------
async def http_get(url: str, timeout: float = CLI_TIMEOUT) -> Tuple[int, Any]:
    if _USE_HTTPX:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return resp.status_code, _safe_json(resp)
    else:
        resp = requests.get(url, timeout=timeout)  # type: ignore
        return resp.status_code, _safe_json(resp)


async def http_post(
    url: str, json_body: Dict[str, Any], timeout: float = CLI_TIMEOUT
) -> Tuple[int, Any]:
    if _USE_HTTPX:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=json_body)
            return resp.status_code, _safe_json(resp)
    else:
        resp = requests.post(url, json=json_body, timeout=timeout)  # type: ignore
        return resp.status_code, _safe_json(resp)


async def http_patch(
    url: str, json_body: Dict[str, Any], timeout: float = CLI_TIMEOUT
) -> Tuple[int, Any]:
    if _USE_HTTPX:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.patch(url, json=json_body)
            return resp.status_code, _safe_json(resp)
    else:
        resp = requests.patch(url, json=json_body, timeout=timeout)  # type: ignore
        return resp.status_code, _safe_json(resp)


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


# ------------------------------------------------------------------------------
# Command Implementations
# ------------------------------------------------------------------------------
async def cmd_health(_: argparse.Namespace) -> None:
    targets = {
        "python_api": f"{PY_API_BASE}/health",
        "go_mover": f"{GO_API_BASE}/health",
        "web": f"{WEB_BASE}/health",
    }
    results = []
    for name, url in targets.items():
        try:
            status, body = await http_get(url)
            if isinstance(body, dict):
                body_summary = {
                    k: body.get(k)
                    for k in ("status", "app_name", "kafka_ready", "time")
                    if k in body
                }
            else:
                body_summary = {"raw": body}
            results.append({"service": name, "code": status, **body_summary})
        except Exception as e:  # noqa: BLE001
            results.append({"service": name, "code": -1, "error": str(e)})
    pretty_output(results)
    # Non-zero exit if any service unreachable
    if any(r["code"] not in (200, 204) for r in results):
        exit_with(3, "One or more services unhealthy/unreachable.")


async def cmd_datasets_list(_: argparse.Namespace) -> None:
    status, body = await http_get(f"{PY_API_BASE}/datasets")
    if status != 200:
        exit_with(3, f"Failed listing datasets (code={status})")
    if isinstance(body, list):
        pretty_output([asdict(Dataset.from_api(d)) for d in body])
    else:
        pretty_output(body)


async def cmd_datasets_create(ns: argparse.Namespace) -> None:
    payload = {
        "name": ns.name,
        "path_uri": ns.path_uri,
        "size_bytes": ns.size_bytes,
        "owner": ns.owner,
    }
    status, body = await http_post(f"{PY_API_BASE}/datasets", payload)
    if status not in (200, 201):
        exit_with(3, f"Create failed (code={status}) body={body}")
    pretty_output(asdict(Dataset.from_api(body)), single=True)


async def cmd_datasets_get(ns: argparse.Namespace) -> None:
    status, body = await http_get(f"{PY_API_BASE}/datasets/{ns.dataset_id}")
    if status == 404:
        exit_with(4, f"Dataset {ns.dataset_id} not found.")
    if status != 200:
        exit_with(3, f"Fetch failed (code={status})")
    pretty_output(asdict(Dataset.from_api(body)), single=True)


async def cmd_datasets_update(ns: argparse.Namespace) -> None:
    updates = {
        "name": ns.name,
        "path_uri": ns.path_uri,
        "size_bytes": ns.size_bytes,
        "owner": ns.owner,
    }
    # Remove None
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        exit_with(2, "No updates provided.")
    status, body = await http_patch(f"{PY_API_BASE}/datasets/{ns.dataset_id}", updates)
    if status != 200:
        exit_with(3, f"Update failed (code={status}) body={body}")
    pretty_output(asdict(Dataset.from_api(body)), single=True)


async def cmd_access_post(ns: argparse.Namespace) -> None:
    payload = {
        "dataset_id": ns.dataset_id,
        "op": ns.op,
        "size_bytes": ns.size_bytes,
        "client_lat_ms": ns.client_lat_ms,
    }
    status, body = await http_post(f"{PY_API_BASE}/access-events", payload)
    if status not in (200, 201):
        exit_with(3, f"Access event failed (code={status}) body={body}")
    pretty_output(body, single=True)


async def cmd_recommendations_list(ns: argparse.Namespace) -> None:
    qs = f"?dataset_id={ns.dataset_id}" if ns.dataset_id else ""
    status, body = await http_get(f"{PY_API_BASE}/recommendations{qs}")
    if status != 200:
        exit_with(3, f"Recommendations failed (code={status})")
    items = body if isinstance(body, list) else [body]
    if getattr(ns, "include_reason", False):
        flattened: List[Dict[str, Any]] = []
        for rec in items:
            if isinstance(rec, dict):
                flat = dict(rec)
                reason = flat.pop("reason", None)
                if isinstance(reason, dict):
                    for rk, rv in reason.items():
                        flat[f"reason_{rk}"] = rv
                flattened.append(flat)
            else:
                flattened.append({"raw": rec})
        items = flattened
    pretty_output(items)


async def cmd_migrate_plan(ns: argparse.Namespace) -> None:
    payload = {
        "dataset_id": ns.dataset_id,
        "target_location": ns.target_location,
        "storage_class": ns.storage_class,
    }
    status, body = await http_post(f"{PY_API_BASE}/plan-migration", payload)
    if status not in (200, 201):
        exit_with(3, f"Plan migration failed (code={status}) body={body}")
    pretty_output(body, single=True)


async def cmd_jobs_list(ns: argparse.Namespace) -> None:
    base = PY_API_BASE if ns.source == "python" else GO_API_BASE
    status, body = await http_get(f"{base}/jobs")
    if status != 200:
        exit_with(3, f"List jobs failed (code={status})")
    rows: List[Dict[str, Any]] = []
    if isinstance(body, list):
        for j in body:
            rows.append(asdict(MigrationJob.from_api(j)))
    else:
        rows.append({"raw": body})
    pretty_output(rows)


async def cmd_jobs_get(ns: argparse.Namespace) -> None:
    base = PY_API_BASE if ns.source == "python" else GO_API_BASE
    status, body = await http_get(f"{base}/jobs/{ns.job_id}")
    if status == 404:
        exit_with(4, f"Job {ns.job_id} not found.")
    if status != 200:
        exit_with(3, f"Get job failed (code={status})")
    pretty_output(asdict(MigrationJob.from_api(body)), single=True)


async def cmd_jobs_retry(ns: argparse.Namespace) -> None:
    # Only supported on Go mover at present
    status, body = await http_post(f"{GO_API_BASE}/jobs/retry/{ns.job_id}", {})
    if status != 200:
        exit_with(3, f"Retry failed (code={status}) body={body}")
    pretty_output(body, single=True)


async def cmd_watch_job(ns: argparse.Namespace) -> None:
    base = PY_API_BASE if ns.source == "python" else GO_API_BASE
    url = f"{base}/jobs/{ns.job_id}"
    last_status = None
    start = time.time()
    while True:
        status_code, body = await http_get(url)
        if status_code == 404:
            exit_with(4, f"Job {ns.job_id} not found.")
        if status_code != 200:
            eprint(f"Fetch error (code={status_code}) body={body}")
        else:
            job = MigrationJob.from_api(body)
            if job.status != last_status:
                print(
                    f"[{time.strftime('%H:%M:%S')}] status: {job.status} "
                    f"(updated_at={job.updated_at})"
                )
                last_status = job.status
            if job.status in ("completed", "failed"):
                print("Terminal status reached.")
                break
        if ns.max_seconds and (time.time() - start) > ns.max_seconds:
            print("Watch timeout reached.")
            break
        await asyncio.sleep(ns.interval)


# ------------------------------------------------------------------------------
# Argument Parser Construction
# ------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data-motion-cli",
        description="Interact with Data in Motion prototype services.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            f"""
            Service Bases:
              PY_API_BASE={PY_API_BASE}
              GO_API_BASE={GO_API_BASE}
              WEB_BASE={WEB_BASE}

            Output Format:
              Set CLI_FORMAT=json for raw JSON output.

            Examples:
              data-motion-cli health
              data-motion-cli datasets list
              data-motion-cli datasets create --name demo --path-uri file:///shared_storage/demo
              data-motion-cli watch job --job-id <uuid> --source python

            Exit Codes:
              0 success
              2 argument error
              3 network / HTTP failure
              4 not found / application condition
              5 unexpected exception
            """
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # health
    p_health = sub.add_parser("health", help="Check health of all services.")
    p_health.set_defaults(func=cmd_health)

    # datasets group
    p_datasets = sub.add_parser("datasets", help="Dataset operations.")
    ds_sub = p_datasets.add_subparsers(dest="ds_cmd", required=True)

    p_ds_list = ds_sub.add_parser("list", help="List datasets.")
    p_ds_list.set_defaults(func=cmd_datasets_list)

    p_ds_create = ds_sub.add_parser("create", help="Create dataset.")
    p_ds_create.add_argument("--name", required=True)
    p_ds_create.add_argument("--path-uri", required=True)
    p_ds_create.add_argument("--size-bytes", type=int, default=None)
    p_ds_create.add_argument("--owner", default=None)
    p_ds_create.set_defaults(func=cmd_datasets_create)

    p_ds_get = ds_sub.add_parser("get", help="Get dataset by ID.")
    p_ds_get.add_argument("--dataset-id", type=int, required=True)
    p_ds_get.set_defaults(func=cmd_datasets_get)

    p_ds_update = ds_sub.add_parser("update", help="Update dataset fields.")
    p_ds_update.add_argument("--dataset-id", type=int, required=True)
    p_ds_update.add_argument("--name")
    p_ds_update.add_argument("--path-uri")
    p_ds_update.add_argument("--size-bytes", type=int)
    p_ds_update.add_argument("--owner")
    p_ds_update.set_defaults(func=cmd_datasets_update)

    # access events (single command)
    p_access = sub.add_parser("access", help="Post an access event.")
    p_access.add_argument("--dataset-id", type=int, required=True)
    p_access.add_argument("--op", choices=["read", "write"], required=True)
    p_access.add_argument("--size-bytes", type=int, required=True)
    p_access.add_argument("--client-lat-ms", type=float, default=None)
    p_access.set_defaults(func=cmd_access_post)

    # recommendations
    p_reco = sub.add_parser(
        "recommendations", help="List storage tier recommendations."
    )
    p_reco.add_argument("--dataset-id", type=int, required=False)
    p_reco.add_argument(
        "--include-reason",
        action="store_true",
        help="Include flattened reason details in table output",
    )
    p_reco.set_defaults(func=cmd_recommendations_list)

    # migrate
    p_migrate = sub.add_parser("migrate", help="Migration planning.")
    mig_sub = p_migrate.add_subparsers(dest="migrate_cmd", required=True)
    p_plan = mig_sub.add_parser("plan", help="Plan a migration job.")
    p_plan.add_argument("--dataset-id", type=int, required=True)
    p_plan.add_argument("--target-location", required=True)
    p_plan.add_argument("--storage-class", required=True)
    p_plan.set_defaults(func=cmd_migrate_plan)

    # jobs
    p_jobs = sub.add_parser("jobs", help="Job operations.")
    jobs_sub = p_jobs.add_subparsers(dest="jobs_cmd", required=True)

    pj_list = jobs_sub.add_parser("list", help="List migration jobs.")
    pj_list.add_argument("--source", choices=["python", "go"], default="python")
    pj_list.set_defaults(func=cmd_jobs_list)

    pj_get = jobs_sub.add_parser("get", help="Get migration job.")
    pj_get.add_argument("--source", choices=["python", "go"], default="python")
    pj_get.add_argument("--job-id", required=True)
    pj_get.set_defaults(func=cmd_jobs_get)

    pj_retry = jobs_sub.add_parser("retry", help="Retry failed job (Go mover).")
    pj_retry.add_argument("--job-id", required=True)
    pj_retry.set_defaults(func=cmd_jobs_retry)

    # watch
    p_watch = sub.add_parser("watch", help="Watch entities for changes.")
    watch_sub = p_watch.add_subparsers(dest="watch_target", required=True)
    w_job = watch_sub.add_parser("job", help="Watch job status progress.")
    w_job.add_argument("--job-id", required=True)
    w_job.add_argument("--source", choices=["python", "go"], default="python")
    w_job.add_argument("--interval", type=float, default=2.0)
    w_job.add_argument("--max-seconds", type=float, default=None)
    w_job.set_defaults(func=cmd_watch_job)

    return parser


# ------------------------------------------------------------------------------
# Main Entrypoint
# ------------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        coro = args.func(args)
        if asyncio.iscoroutine(coro):
            asyncio.run(coro)
    except KeyboardInterrupt:  # noqa: D401
        exit_with(5, "Interrupted.")
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        exit_with(5, f"Unexpected error: {e}")


if __name__ == "__main__":
    main()
