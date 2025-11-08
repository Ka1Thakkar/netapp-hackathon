import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

# Defensive: ensure Request is defined even if not explicitly imported above
try:
    from fastapi import Request as _FastAPIRequest  # type: ignore
except Exception:
    try:
        from starlette.requests import Request as _FastAPIRequest  # type: ignore
    except Exception:
        _FastAPIRequest = object  # type: ignore
Request = _FastAPIRequest
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

# DB layer (SQLAlchemy models & CRUD helpers)
from .db import (
    JobStatusEnum,
    TierEnum,
    create_recommendation,
    get_db,
)
from .db import (
    create_dataset as db_create_dataset,
)
from .db import (
    create_migration_job as db_create_migration_job,
)
from .db import (
    get_dataset as db_get_dataset,
)
from .db import (
    get_migration_job as db_get_migration_job,
)
from .db import (
    list_datasets as db_list_datasets,
)
from .db import (
    list_migration_jobs as db_list_migration_jobs,
)
from .db import (
    update_dataset as db_update_dataset,
)
from .db import (
    update_migration_job_status as db_update_migration_job_status,
)

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("python_api")

# ------------------------------------------------------------------------------
# Env and Constants
# ------------------------------------------------------------------------------
KAFKA_BROKERS = os.getenv("KAFKA_BROKERS", "localhost:9092")
KAFKA_ACCESS_EVENTS_TOPIC = os.getenv("KAFKA_ACCESS_EVENTS_TOPIC", "access_events")
KAFKA_MIGRATION_JOBS_TOPIC = os.getenv("KAFKA_MIGRATION_JOBS_TOPIC", "migration_jobs")
KAFKA_MIGRATION_STATUS_TOPIC = os.getenv(
    "KAFKA_MIGRATION_STATUS_TOPIC", "migration_status"
)
KAFKA_GROUP = os.getenv("KAFKA_GROUP", "python_api_group")

SQLITE_PATH = os.getenv("SQLITE_PATH", "/data/metadata.db")
APP_NAME = os.getenv("APP_NAME", "data-in-motion-python-api")
APP_VERSION = os.getenv("APP_VERSION", "0.1.0")
# Pub/Sub toggle (optional; falls back to NullKafka behavior if enabled)
USE_PUBSUB = os.getenv("USE_PUBSUB", "false").lower() == "true"
PUBSUB_PROJECT_ID = os.getenv("PUBSUB_PROJECT_ID", "")
PUBSUB_ACCESS_EVENTS_TOPIC = os.getenv("PUBSUB_ACCESS_EVENTS_TOPIC", "access-events")
PUBSUB_MIGRATION_JOBS_TOPIC = os.getenv("PUBSUB_MIGRATION_JOBS_TOPIC", "migration-jobs")
PUBSUB_MIGRATION_STATUS_TOPIC = os.getenv(
    "PUBSUB_MIGRATION_STATUS_TOPIC", "migration-status"
)


# ------------------------------------------------------------------------------
# Models (Pydantic)
# ------------------------------------------------------------------------------
class DatasetCreate(BaseModel):
    name: str
    path_uri: str
    latency_slo_ms: Optional[int] = None
    size_bytes: Optional[int] = None
    owner: Optional[str] = None
    auto_seed: Optional[bool] = False


class DatasetUpdate(BaseModel):
    name: Optional[str] = None
    path_uri: Optional[str] = None
    latency_slo_ms: Optional[int] = None
    size_bytes: Optional[int] = None
    owner: Optional[str] = None
    current_tier: Optional[str] = Field(None, pattern="^(hot|warm|cold)$")


class DatasetOut(BaseModel):
    id: int
    name: str
    path_uri: str
    current_tier: str
    latency_slo_ms: Optional[int]
    size_bytes: Optional[int]
    owner: Optional[str]
    created_at: float
    updated_at: float
    last_access_ts: Optional[float] = None
    seed_attempted: Optional[bool] = None
    seed_created: Optional[bool] = None
    seed_error: Optional[str] = None


class AccessEvent(BaseModel):
    dataset_id: int
    op: str = Field(..., pattern="^(read|write)$")
    size_bytes: int = Field(..., ge=0)
    client_lat_ms: Optional[float] = Field(None, ge=0)
    timestamp: Optional[float] = None


class TrainResponse(BaseModel):
    status: str
    model_version: str


class RecommendationOut(BaseModel):
    dataset_id: int
    recommended_tier: str
    recommended_location: Optional[str] = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    reason: Dict[str, Any]


class MigrationPlanIn(BaseModel):
    dataset_id: int
    target_location: Optional[str] = None
    storage_class: Optional[str] = None


class JobOut(BaseModel):
    job_id: str
    status: str
    dataset_id: int
    submitted_at: float


# ------------------------------------------------------------------------------
# In-memory stores (MVP scaffolding; swap to SQLite/DAO layer later)
# ------------------------------------------------------------------------------
DATASETS: Dict[int, DatasetOut] = {}
NEXT_DATASET_ID: int = 1

JOBS: Dict[str, JobOut] = {}
MODEL_VERSION: str = "bootstrap-0"


# ------------------------------------------------------------------------------
# Kafka Manager (scaffolding)
# ------------------------------------------------------------------------------
class NullKafka:
    """Fallback Kafka adapter that logs messages instead of sending/consuming."""

    def __init__(
        self,
        on_migration_status: Optional[
            Callable[[Dict[str, Any]], Awaitable[None]]
        ] = None,
    ):
        self.started = False
        self.on_migration_status = on_migration_status

    async def start(self):
        self.started = True
        logger.warning(
            "Kafka disabled (aiokafka not installed or brokers unavailable). Using NullKafka."
        )

    async def stop(self):
        self.started = False
        logger.info("NullKafka stopped.")

    async def produce(
        self, topic: str, value: Dict[str, Any], key: Optional[str] = None
    ):
        logger.info(
            "[NullKafka] produce topic=%s key=%s value=%s",
            topic,
            key,
            json.dumps(value),
        )

    async def run_status_consumer(self):
        # No-op consumer loop
        while self.started:
            await asyncio.sleep(2.0)


class KafkaManager:
    """Async Kafka producer/consumer scaffolding with graceful startup/shutdown."""

    def __init__(
        self,
        brokers: str,
        status_topic: str,
        group_id: str,
        on_migration_status: Optional[
            Callable[[Dict[str, Any]], Awaitable[None]]
        ] = None,
    ):
        self.brokers = brokers
        self.status_topic = status_topic
        self.group_id = group_id
        self.on_migration_status = on_migration_status

        self._producer = None
        self._consumer = None
        self._consume_task: Optional[asyncio.Task] = None
        self._use_null = False

    async def start(self):
        try:
            from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # type: ignore
        except Exception as e:
            logger.warning("aiokafka not available: %s", e)
            self._use_null = True

        if self._use_null:
            # Downgrade to NullKafka behavior
            self._producer = NullKafka(on_migration_status=self.on_migration_status)
            await self._producer.start()
            self._consume_task = asyncio.create_task(
                self._producer.run_status_consumer()
            )
            return

        # Real aiokafka setup
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # type: ignore

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.brokers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if k is not None else None,
        )
        await self._producer.start()
        logger.info("Kafka producer started (brokers=%s)", self.brokers)

        self._consumer = AIOKafkaConsumer(
            self.status_topic,
            bootstrap_servers=self.brokers,
            group_id=self.group_id,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            key_deserializer=lambda k: k.decode("utf-8") if k else None,
            enable_auto_commit=True,
        )
        await self._consumer.start()
        logger.info(
            "Kafka consumer started (topic=%s, group=%s)",
            self.status_topic,
            self.group_id,
        )

        self._consume_task = asyncio.create_task(self._consume_loop())

    async def stop(self):
        # Stop consumer task first
        if self._consume_task:
            self._consume_task.cancel()
            try:
                await self._consume_task
            except asyncio.CancelledError:
                pass
            self._consume_task = None

        # Stop consumer
        if self._consumer:
            try:
                await self._consumer.stop()
            except Exception as e:
                logger.warning("Error stopping Kafka consumer: %s", e)
            self._consumer = None

        # Stop producer
        if self._producer:
            try:
                await self._producer.stop()
            except Exception as e:
                logger.warning("Error stopping Kafka producer: %s", e)
            self._producer = None

        logger.info("KafkaManager stopped.")

    async def produce(
        self, topic: str, value: Dict[str, Any], key: Optional[str] = None
    ):
        if isinstance(self._producer, NullKafka):
            await self._producer.produce(topic, value, key)
            return

        if self._producer is None:
            raise RuntimeError("Kafka producer is not started.")
        try:
            await self._producer.send_and_wait(topic, value=value, key=key)
            logger.debug("Produced message to %s key=%s", topic, key)
        except Exception as e:
            logger.error("Failed to produce to %s: %s", topic, e)
            raise

    async def _consume_loop(self):
        # Only valid for real aiokafka consumer
        assert self._consumer is not None
        try:
            async for msg in self._consumer:
                try:
                    payload = msg.value
                    if self.on_migration_status:
                        await self.on_migration_status(payload)
                    else:
                        logger.info("Migration status received: %s", payload)
                except Exception as inner:
                    logger.exception("Error handling consumed message: %s", inner)
        except asyncio.CancelledError:
            logger.info("Kafka consumer task cancelled.")
        except Exception as e:
            logger.exception("Kafka consumer loop error: %s", e)


# ------------------------------------------------------------------------------
# FastAPI App
# ------------------------------------------------------------------------------
tags_metadata = [
    {"name": "health", "description": "Service health and info."},
    {"name": "datasets", "description": "Manage datasets metadata."},
    {"name": "events", "description": "Ingest access events and stream to Kafka."},
    {"name": "ml", "description": "Training and recommendation endpoints."},
    {"name": "migrations", "description": "Plan and track migration jobs."},
]

app = FastAPI(
    title=f"{APP_NAME}",
    version=APP_VERSION,
    openapi_tags=tags_metadata,
)

# ------------------------------------------------------------------------------
# Metrics / Instrumentation (lightweight in-memory; exposed via /metrics)
# ------------------------------------------------------------------------------
from fastapi.responses import StreamingResponse


# In-memory pub/sub for SSE
class EventBus:
    def __init__(self):
        self._subs: "set[asyncio.Queue[str]]" = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: dict) -> None:
        """
        Publish an event to all subscribers as SSE lines.
        """
        msg = "event: job_status\ndata: " + json.dumps(event) + "\n\n"
        async with self._lock:
            queues = list(self._subs)
        for q in queues:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # Drop oldest and try again (best-effort)
                try:
                    _ = q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subs.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subs.discard(q)


EVENT_BUS = EventBus()


@app.get("/events", tags=["sse"])
async def sse_events(request: Request):
    """
    Server-Sent Events endpoint streaming job_status updates.
    """
    q = await EVENT_BUS.subscribe()

    async def gen():
        try:
            # Initial comment to establish stream
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield msg
                except asyncio.TimeoutError:
                    # Keep-alive comment to prevent idle timeouts
                    yield ": keepalive\n\n"
        finally:
            await EVENT_BUS.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


from collections import defaultdict

from fastapi import Request

REQUEST_COUNT: dict[str, int] = defaultdict(int)
JOB_STATUS_TRANSITIONS: dict[str, int] = defaultdict(int)
EVENT_BYTES_TOTAL: int = 0
JOB_BYTES_TOTAL: int = 0
JOB_FAILURES_INJECTED: int = 0
START_TIME: float = time.time()


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        REQUEST_COUNT[request.url.path] += 1
    except Exception:
        pass
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------------------
# Helpers / State
# ------------------------------------------------------------------------------
def now_ts() -> float:
    return time.time()


def make_dataset_out(id_: int, data: DatasetCreate) -> DatasetOut:
    ts = now_ts()
    return DatasetOut(
        id=id_,
        name=data.name,
        path_uri=data.path_uri,
        current_tier="hot",
        latency_slo_ms=data.latency_slo_ms,
        size_bytes=data.size_bytes,
        owner=data.owner,
        created_at=ts,
        updated_at=ts,
        last_access_ts=ts,
    )


async def handle_migration_status(payload: Dict[str, Any]):
    global JOB_BYTES_TOTAL
    """
    Migration status consumer callback. Expected payload example:
    {
        "job_id": "...",
        "dataset_id": 1,
        "status": "copying|verifying|switching|completed|failed",
        "error": null,
        "ts": 1234567890.12
    }
    """
    job_id = payload.get("job_id")
    status = payload.get("status")
    dataset_id = payload.get("dataset_id")
    if not job_id or status is None:
        logger.warning("Malformed migration status payload: %s", payload)
        return

    # Publish SSE and update in-memory view
    try:
        await EVENT_BUS.publish(
            {
                "type": "job_status",
                "job_id": job_id,
                "dataset_id": dataset_id,
                "status": status,
                "error": payload.get("error"),
                "job_key": payload.get("job_key"),
                "dest_uri": payload.get("dest_uri"),
                "ts": now_ts(),
            }
        )
    except Exception:
        # Do not fail the status handler due to SSE issues
        pass

    job = JOBS.get(job_id)
    JOB_STATUS_TRANSITIONS[str(status)] += 1
    if status == "completed":
        # Attempt to approximate migrated bytes (file:// destination size)
        try:
            from os import stat

            db_local = None
            try:
                gen2 = get_db()
                db_local = next(gen2)
                db_job2 = db_get_migration_job(db_local, job_id)
                if (
                    db_job2
                    and db_job2.dest_uri
                    and db_job2.dest_uri.startswith("file://")
                ):
                    path_local = db_job2.dest_uri[len("file://") :]
                    st = stat(path_local)
                    JOB_BYTES_TOTAL += st.st_size
            except Exception:
                pass
            finally:
                if db_local:
                    try:
                        db_local.close()
                    except Exception:
                        pass
        except Exception:
            pass
    if job:
        JOBS[job_id] = JobOut(
            job_id=job.job_id,
            status=status,
            dataset_id=job.dataset_id,
            submitted_at=job.submitted_at,
        )
    else:
        logger.info(
            "Received status for unknown job_id=%s (dataset_id=%s)", job_id, dataset_id
        )

    # Persist transition to DB
    db = None
    try:
        gen = get_db()
        db = next(gen)
    except Exception as e:
        logger.warning("DB session create failed while handling status: %s", e)
    if db is not None:
        try:
            db_job = db_get_migration_job(db, job_id)
            if db_job:
                status_map = {
                    "queued": JobStatusEnum.QUEUED,
                    "copying": JobStatusEnum.COPYING,
                    "verifying": JobStatusEnum.VERIFYING,
                    "switching": JobStatusEnum.SWITCHING,
                    "completed": JobStatusEnum.COMPLETED,
                    "failed": JobStatusEnum.FAILED,
                    "enqueue_failed": JobStatusEnum.ENQUEUE_FAILED,
                    "cancelled": JobStatusEnum.FAILED,
                }
                enum_val = status_map.get(str(status))
                if enum_val is None:
                    logger.warning("Unknown status '%s' for job %s", status, job_id)
                else:
                    db_update_migration_job_status(
                        db, db_job, enum_val, payload.get("error")
                    )
                    logger.info("Persisted job %s status -> %s", job_id, status)
            else:
                logger.info("Job %s not found in DB when persisting status", job_id)
        except Exception as e:
            logger.exception("Failed to persist job status for %s: %s", job_id, e)
        finally:
            try:
                db.close()
            except Exception:
                pass


# ------------------------------------------------------------------------------
# Startup / Shutdown
# ------------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    # Initialize streaming backend
    if USE_PUBSUB:
        logger.info(
            "USE_PUBSUB enabled (project_id=%s). Using NullKafka adapter as placeholder.",
            PUBSUB_PROJECT_ID,
        )
        app.state.kafka = NullKafka(on_migration_status=handle_migration_status)
        await app.state.kafka.start()
    else:
        try:
            app.state.kafka = KafkaManager(
                brokers=KAFKA_BROKERS,
                status_topic=KAFKA_MIGRATION_STATUS_TOPIC,
                group_id=KAFKA_GROUP,
                on_migration_status=handle_migration_status,
            )
            await app.state.kafka.start()
        except Exception as e:
            logger.error("KafkaManager start failed; falling back to NullKafka: %s", e)
            app.state.kafka = NullKafka(on_migration_status=handle_migration_status)
            await app.state.kafka.start()
    logger.info("Service startup complete.")


@app.on_event("shutdown")
async def on_shutdown():
    # Stop Kafka
    try:
        if hasattr(app.state, "kafka") and app.state.kafka:
            await app.state.kafka.stop()
    finally:
        logger.info("Service shutdown complete.")


# ------------------------------------------------------------------------------
# Endpoints: Health
# ------------------------------------------------------------------------------
@app.get("/health", tags=["health"])
async def health():
    kafka_ready = hasattr(app.state, "kafka") and app.state.kafka is not None
    return {
        "status": "ok",
        "app": APP_NAME,
        "version": APP_VERSION,
        "kafka_brokers": KAFKA_BROKERS,
        "kafka_ready": kafka_ready,
        "datasets_count": len(DATASETS),
        "jobs_count": len(JOBS),
        "model_version": MODEL_VERSION,
        "time": now_ts(),
    }


# ------------------------------------------------------------------------------
# Endpoints: Datasets
# ------------------------------------------------------------------------------
@app.post("/datasets", response_model=DatasetOut, tags=["datasets"])
async def create_dataset(payload: DatasetCreate, db: Session = Depends(get_db)):
    """
    Create a dataset. If auto_seed==True and path_uri targets local shared storage,
    attempt to create (or resize) the backing file immediately.

    Improvements:
      - Stronger duplicate detection (409)
      - Normalizes local path
      - Returns seeding status via log + sets size_bytes if file created
      - Silently skips seeding if path is not a supported file:// target
    """
    # Duplicate name check (409 Conflict)
    try:
        for existing in db_list_datasets(db):
            if existing.name == payload.name:
                raise HTTPException(
                    status_code=409,
                    detail=f"Dataset name '{payload.name}' already exists",
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.debug("Dataset duplication check skipped: %s", e)

    ds = db_create_dataset(
        db,
        name=payload.name,
        path_uri=payload.path_uri,
        latency_slo_ms=payload.latency_slo_ms,
        size_bytes=payload.size_bytes,
        owner=payload.owner,
    )
    # Initialize last_access_ts at creation if missing
    try:
        if ds.last_access_ts is None:
            ds = db_update_dataset(db, ds, last_access_ts=ds.created_at)
    except Exception as e:
        logger.warning("Failed to initialize last_access_ts: %s", e)

    # Auto-seed (demo) when requested and path is local shared storage
    seed_attempted = False
    seed_created = False
    seed_error = None
    if (
        getattr(payload, "auto_seed", False)
        and isinstance(payload.path_uri, str)
        and payload.path_uri.startswith("file:///shared_storage/")
    ):
        seed_attempted = True
        # Normalize path: remove file:// prefix
        path_local = payload.path_uri[len("file://") :]
        try:
            os.makedirs(os.path.dirname(path_local), exist_ok=True)
            size = int(payload.size_bytes or (5 * 1024 * 1024))
            # If file exists but different size, resize (truncate)
            with open(path_local, "wb") as f:
                f.truncate(size)
            seed_created = True
            # Update persisted size_bytes if not set or different
            try:
                if ds.size_bytes is None or int(ds.size_bytes) != int(size):
                    ds = db_update_dataset(db, ds, size_bytes=int(size))
            except Exception as _e:
                logger.warning("Failed to persist size_bytes after seeding: %s", _e)
            logger.info(
                "Auto-seeded dataset file name=%s path=%s size=%d",
                payload.name,
                path_local,
                size,
            )
        except Exception as e:
            seed_error = str(e)
            logger.warning(
                "Auto-seed failed dataset=%s path=%s error=%s",
                payload.name,
                path_local,
                e,
            )

    # Return canonical representation; include seed status; fail if auto-seed was attempted and failed
    if seed_attempted and not seed_created:
        raise HTTPException(
            status_code=500, detail=f"Auto-seed failed: {seed_error or 'unknown error'}"
        )
    return DatasetOut(
        id=ds.id,
        name=ds.name,
        path_uri=ds.path_uri,
        current_tier=ds.current_tier.value
        if hasattr(ds.current_tier, "value")
        else ds.current_tier,
        latency_slo_ms=ds.latency_slo_ms,
        size_bytes=ds.size_bytes,
        owner=ds.owner,
        created_at=ds.created_at,
        updated_at=ds.updated_at,
        last_access_ts=ds.last_access_ts,
        seed_attempted=seed_attempted,
        seed_created=seed_created,
        seed_error=seed_error,
    )


@app.post("/datasets/{dataset_id}/seed", tags=["datasets"])
async def seed_dataset_file(
    dataset_id: int,
    size_bytes: Optional[int] = None,
    overwrite: bool = True,
    db: Session = Depends(get_db),
):
    """
    Explicitly create (or resize) the local file backing a dataset whose path_uri
    uses file:///shared_storage/.

    Parameters:
      size_bytes: desired size (defaults to dataset.size_bytes or 5MB)
      overwrite: if False and file exists, do nothing

    Returns JSON with seed status.
    """
    dsm = db_get_dataset(db, dataset_id)
    if not dsm:
        raise HTTPException(status_code=404, detail="Dataset not found")
    if not dsm.path_uri.startswith("file:///shared_storage/"):
        raise HTTPException(
            status_code=400,
            detail="Seeding supported only for file:///shared_storage/* paths",
        )

    path_local = dsm.path_uri[len("file://") :]
    desired = int(
        size_bytes
        or (dsm.size_bytes if dsm.size_bytes is not None else (5 * 1024 * 1024))
    )

    status = "skipped"
    reason = ""
    try:
        os.makedirs(os.path.dirname(path_local), exist_ok=True)
        if os.path.exists(path_local):
            if not overwrite:
                status = "exists"
            else:
                with open(path_local, "r+b") as f:
                    f.truncate(desired)
                status = "resized"
        else:
            with open(path_local, "wb") as f:
                f.truncate(desired)
            status = "created"
    except Exception as e:
        status = "error"
        reason = str(e)
        logger.error(
            "Explicit seed failed dataset_id=%s path=%s error=%s",
            dataset_id,
            path_local,
            e,
        )

    return {
        "dataset_id": dataset_id,
        "path": path_local,
        "status": status,
        "size_bytes": desired,
        "error": reason or None,
    }


@app.get("/datasets", response_model=List[DatasetOut], tags=["datasets"])
async def list_datasets(db: Session = Depends(get_db)):
    items = []
    for ds in db_list_datasets(db):
        items.append(
            DatasetOut(
                id=ds.id,
                name=ds.name,
                path_uri=ds.path_uri,
                current_tier=ds.current_tier.value
                if hasattr(ds.current_tier, "value")
                else ds.current_tier,
                latency_slo_ms=ds.latency_slo_ms,
                size_bytes=ds.size_bytes,
                owner=ds.owner,
                created_at=ds.created_at,
                updated_at=ds.updated_at,
                last_access_ts=ds.last_access_ts,
            )
        )
    return items


@app.get("/datasets/{dataset_id}", response_model=DatasetOut, tags=["datasets"])
async def get_dataset(dataset_id: int, db: Session = Depends(get_db)):
    dsm = db_get_dataset(db, dataset_id)
    if not dsm:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return DatasetOut(
        id=dsm.id,
        name=dsm.name,
        path_uri=dsm.path_uri,
        current_tier=dsm.current_tier.value
        if hasattr(dsm.current_tier, "value")
        else dsm.current_tier,
        latency_slo_ms=dsm.latency_slo_ms,
        size_bytes=dsm.size_bytes,
        owner=dsm.owner,
        created_at=dsm.created_at,
        updated_at=dsm.updated_at,
        last_access_ts=dsm.last_access_ts,
    )


@app.patch("/datasets/{dataset_id}", response_model=DatasetOut, tags=["datasets"])
async def update_dataset(
    dataset_id: int, payload: DatasetUpdate, db: Session = Depends(get_db)
):
    dsm = db_get_dataset(db, dataset_id)
    if not dsm:
        raise HTTPException(status_code=404, detail="Dataset not found")
    updates = {k: v for k, v in payload.dict(exclude_unset=True).items()}
    dsm = db_update_dataset(db, dsm, **updates)
    return DatasetOut(
        id=dsm.id,
        name=dsm.name,
        path_uri=dsm.path_uri,
        current_tier=dsm.current_tier.value
        if hasattr(dsm.current_tier, "value")
        else dsm.current_tier,
        latency_slo_ms=dsm.latency_slo_ms,
        size_bytes=dsm.size_bytes,
        owner=dsm.owner,
        created_at=dsm.created_at,
        updated_at=dsm.updated_at,
        last_access_ts=dsm.last_access_ts,
    )


# ------------------------------------------------------------------------------
# Endpoints: Events (Kafka)
# ------------------------------------------------------------------------------
@app.post("/access-events", tags=["events"])
async def post_access_event(
    ev: AccessEvent, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    dsm = db_get_dataset(db, ev.dataset_id)
    if not dsm:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Update last access timestamp
    ts = ev.timestamp or now_ts()
    dsm = db_update_dataset(db, dsm, last_access_ts=ts)
    logger.info("Updated last_access_ts dataset_id=%s ts=%.6f", dsm.id, ts)
    global EVENT_BYTES_TOTAL
    EVENT_BYTES_TOTAL += ev.size_bytes

    # Produce to stream
    event_payload = ev.dict()
    if event_payload.get("timestamp") is None:
        event_payload["timestamp"] = ts

    async def _produce():
        try:
            await app.state.kafka.produce(
                KAFKA_ACCESS_EVENTS_TOPIC, value=event_payload, key=str(ev.dataset_id)
            )
        except Exception as e:
            logger.error("Failed to send access event to stream: %s", e)

    background_tasks.add_task(_produce)

    return {
        "status": "queued",
        "dataset_id": ev.dataset_id,
        "timestamp": event_payload["timestamp"],
    }


# ------------------------------------------------------------------------------
# Endpoints: ML (Training & Recommendations)
# ------------------------------------------------------------------------------
@app.post("/train", response_model=TrainResponse, tags=["ml"])
async def train_model():
    """
    Invokes the ML training script as a subprocess.
    The script persists artifacts under MODEL_DIR and records metadata in SQLite.
    Automatically lowers --min-samples threshold for demo scenarios with few datasets.

    On failure, surfaces a concise portion of stdout/stderr for easier debugging.
    """
    model_version = str(int(now_ts()))
    py = os.getenv("PYTHON_BIN", "python")
    sqlite_path = os.getenv("SQLITE_PATH", "/data/metadata.db")
    model_dir = os.getenv("MODEL_DIR", "/models")
    cmd = [
        py,
        "-u",
        "ml/train.py",
        "--sqlite-path",
        sqlite_path,
        "--model-dir",
        model_dir,
        "--version",
        model_version,
    ]
    # Adaptive min-samples: if current dataset count < 20, lower threshold to 2 for demo training
    try:
        from .db import get_db as _get_db
        from .db import list_datasets as db_list_datasets

        gen = _get_db()
        db_local = next(gen)
        ds_count = len(db_list_datasets(db_local))
        if ds_count < 20:
            cmd += ["--min-samples", "2"]
            logger.info(
                "Adaptive training: dataset_count=%d < 20, adding --min-samples 2",
                ds_count,
            )
    except Exception as _e:
        logger.warning("Adaptive min-samples check failed: %s", _e)
    finally:
        if "db_local" in locals():
            try:
                db_local.close()
            except Exception:
                pass
    try:
        logger.info("Starting training subprocess: %s", " ".join(cmd))
        proc = subprocess.run(
            cmd, cwd=os.getcwd(), capture_output=True, text=True, check=False
        )
        logger.info("train.py stdout:\n%s", proc.stdout)
        if proc.returncode != 0:
            logger.error("train.py stderr:\n%s", proc.stderr)
            # Build error detail snippet (stderr preferred, fallback to stdout)
            stderr_snip = (proc.stderr or "").strip()
            stdout_snip = (proc.stdout or "").strip()
            merged = stderr_snip if stderr_snip else stdout_snip
            if len(merged) > 800:
                merged = merged[:800] + "...(truncated)"
            raise HTTPException(
                status_code=500,
                detail=f"Training failed (rc={proc.returncode}) snippet: {merged}",
            )
    except Exception as e:
        logger.exception("Training subprocess error: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Training error: {type(e).__name__}: {str(e)[:600]}",
        )
    return TrainResponse(status="ok", model_version=model_version)


@app.get("/recommendations", response_model=List[RecommendationOut], tags=["ml"])
async def get_recommendations(
    dataset_id: Optional[int] = None, db: Session = Depends(get_db)
):
    """
    Return storage tier recommendations.

    Order of attempt:
      1. Use latest trained ML model (if available and inference succeeds).
      2. Fallback to heuristic (recency-based) per dataset.

    Both paths persist a recommendation row (best-effort).
    """
    # Collect dataset objects
    if dataset_id:
        datasets = [db_get_dataset(db, dataset_id)]
    else:
        datasets = db_list_datasets(db)
    datasets = [d for d in datasets if d]

    out: List[RecommendationOut] = []
    nowt = now_ts()

    # Try load latest model
    model = None
    model_version = None
    feature_extraction_error = None
    try:
        import glob

        import joblib

        model_dir = os.getenv("MODEL_DIR", "/models")
        paths = sorted(
            glob.glob(os.path.join(model_dir, "temperature_*.joblib")), reverse=True
        )
        if paths:
            model_path = paths[0]
            model_version = os.path.splitext(os.path.basename(model_path))[0].split(
                "_"
            )[-1]
            model = joblib.load(model_path)
            logger.info("Loaded ML model for recommendations: %s", model_path)
    except Exception as e:
        logger.info("No ML model available (fallback to heuristic): %s", e)

    def heuristic(dsm) -> tuple[str, float, dict]:
        if dsm.last_access_ts is None:
            return (
                "cold",
                0.55,
                {
                    "last_access": None,
                    "rule": "no_recent_access",
                    "source": "heuristic",
                },
            )
        age = nowt - float(dsm.last_access_ts)
        if age <= 60:
            return (
                "hot",
                0.8,
                {
                    "last_access_age_s": age,
                    "threshold_s": 60,
                    "rule": "last_access<=60s",
                    "source": "heuristic",
                },
            )
        if age <= 600:
            return (
                "warm",
                0.7,
                {
                    "last_access_age_s": age,
                    "threshold_s": 600,
                    "rule": "60s<last_access<=10m",
                    "source": "heuristic",
                },
            )
        return (
            "cold",
            0.75,
            {
                "last_access_age_s": age,
                "threshold_s": 600,
                "rule": "last_access>10m",
                "source": "heuristic",
            },
        )

    # Attempt ML inference if model loaded
    ml_predictions: dict[int, tuple[str, float, dict]] = {}
    if model and datasets:
        try:
            # Build minimal feature vectors consistent enough for demo:
            # [size_bytes, latency_slo_ms (or 0), age_since_last_access_s (or large)]
            X = []
            idx_map = []
            for d in datasets:
                age = (
                    nowt - float(d.last_access_ts)
                    if d.last_access_ts is not None
                    else 1e9
                )
                size = int(d.size_bytes or 0)
                lat = int(d.latency_slo_ms or 0)
                X.append([size, lat, age])
                idx_map.append(d.id)
            import numpy as np  # fastapi image has numpy installed

            arr = np.array(X, dtype="float64")
            try:
                preds = model.predict(arr)
                # Optional probabilities
                try:
                    probs = model.predict_proba(arr)
                except Exception:
                    probs = None
                for i, d_id in enumerate(idx_map):
                    raw = str(preds[i]).lower()
                    if raw not in ("hot", "warm", "cold"):
                        # Normalize unknown labels
                        raw = "warm"
                    conf = 0.0
                    if probs is not None:
                        # Map probability if class ordering known
                        try:
                            class_index = list(model.classes_).index(raw)
                            conf = float(probs[i][class_index])
                        except Exception:
                            conf = 0.5
                    reason = {
                        "source": "ml",
                        "model_version": model_version,
                        "features": {
                            "size_bytes": X[i][0],
                            "latency_slo_ms": X[i][1],
                            "age_last_access_s": X[i][2],
                        },
                    }
                    ml_predictions[d_id] = (raw, conf, reason)
            except Exception as e:
                feature_extraction_error = e
                logger.warning("Model predict failed (fallback to heuristic): %s", e)
                ml_predictions = {}
        except Exception as e:
            logger.warning("ML feature preparation failed: %s", e)

    for dsm in datasets:
        if dsm.id in ml_predictions:
            tier, conf, reason = ml_predictions[dsm.id]
        else:
            tier, conf, reason = heuristic(dsm)
            if feature_extraction_error and "source" not in reason:
                reason["ml_error"] = str(feature_extraction_error)
        tier_enum = {"hot": TierEnum.HOT, "warm": TierEnum.WARM, "cold": TierEnum.COLD}[
            tier
        ]
        try:
            create_recommendation(
                db=db,
                dataset_id=dsm.id,
                recommended_tier=tier_enum,
                recommended_location=None,
                reason=json.dumps(reason),
                confidence=conf,
            )
        except Exception as e:
            logger.debug("Persist recommendation failed (dataset=%s): %s", dsm.id, e)
        out.append(
            RecommendationOut(
                dataset_id=dsm.id,
                recommended_tier=tier,
                recommended_location=None,
                confidence=conf,
                reason=reason,
            )
        )
    return out


# ------------------------------------------------------------------------------
# Endpoints: Migrations (plan job => Kafka)
# ------------------------------------------------------------------------------
@app.post("/plan-migration", response_model=JobOut, tags=["migrations"])
async def plan_migration(plan: MigrationPlanIn, db: Session = Depends(get_db)):
    dsm = db_get_dataset(db, plan.dataset_id)
    if not dsm:
        raise HTTPException(status_code=404, detail="Dataset not found")
    submitted_at = now_ts()

    # Policy evaluation: enforce basic latency and cost constraints based on policies
    try:
        from .db import list_policies as db_list_policies  # lazy import to avoid cycles

        # Dataset-specific policies
        policies = db_list_policies(db, dataset_id=plan.dataset_id)
        # Global policies (dataset_id=None) — append
        policies += [p for p in db_list_policies(db, dataset_id=None)]
    except Exception as e:
        logger.warning("Failed to load policies; proceeding without enforcement: %s", e)
        policies = []

    target_location = plan.target_location
    storage_class = plan.storage_class

    # Simple catalogs (demo values) for storage classes
    class_latency_ms = {"hot": 10, "warm": 100, "cold": 500}
    class_cost_per_gb_month = {"hot": 0.10, "warm": 0.02, "cold": 0.005}

    # Aggregate effective constraints
    eff_latency_slo = None
    max_cost = None
    for pol in policies:
        if getattr(pol, "latency_slo_ms", None) is not None:
            if eff_latency_slo is None:
                eff_latency_slo = pol.latency_slo_ms
            else:
                eff_latency_slo = min(eff_latency_slo, pol.latency_slo_ms)
        if getattr(pol, "max_cost_per_gb_month", None) is not None:
            if max_cost is None:
                max_cost = pol.max_cost_per_gb_month
            else:
                max_cost = min(max_cost, pol.max_cost_per_gb_month)

    # Fall back to dataset latency SLO if policy not provided
    if eff_latency_slo is None and getattr(dsm, "latency_slo_ms", None) is not None:
        eff_latency_slo = dsm.latency_slo_ms

    # Choose or validate storage_class
    if storage_class is None:
        candidates = ["hot", "warm", "cold"]
        if eff_latency_slo is not None:
            candidates = [
                c for c in candidates if class_latency_ms.get(c, 1e9) <= eff_latency_slo
            ]
        if max_cost is not None:
            candidates = [
                c for c in candidates if class_cost_per_gb_month.get(c, 1e9) <= max_cost
            ]
        if not candidates:
            raise HTTPException(
                status_code=400, detail="No storage class satisfies policy constraints"
            )
        # pick the cheapest among candidates
        storage_class = sorted(
            candidates, key=lambda c: class_cost_per_gb_month.get(c, 1e9)
        )[0]
    else:
        if storage_class not in class_latency_ms:
            raise HTTPException(
                status_code=400, detail=f"Unknown storage_class: {storage_class}"
            )
        if (
            eff_latency_slo is not None
            and class_latency_ms[storage_class] > eff_latency_slo
        ):
            raise HTTPException(
                status_code=400,
                detail=f"storage_class '{storage_class}' violates latency SLO {eff_latency_slo}ms",
            )
        if max_cost is not None and class_cost_per_gb_month[storage_class] > max_cost:
            raise HTTPException(
                status_code=400,
                detail=f"storage_class '{storage_class}' exceeds max cost {max_cost}/GB-month",
            )

    # If no target_location provided, propose a simple default based on class (local FS demo)
    if not target_location:
        suffix = {"hot": "", "warm": "_warm", "cold": "_cold"}[storage_class]
        target_location = dsm.path_uri + suffix

    job = db_create_migration_job(
        db=db,
        dataset_id=plan.dataset_id,
        source_uri=dsm.path_uri,
        dest_uri=target_location,
        dest_storage_class=storage_class,
        job_key=f"dataset:{plan.dataset_id}:ts:{int(submitted_at)}",
    )
    # Produce job to stream
    payload = {
        "job_id": job.job_id,
        "job_key": job.job_key,
        "dataset_id": plan.dataset_id,
        "source_uri": dsm.path_uri,
        "dest_uri": target_location,
        "dest_storage_class": storage_class,
        "status": "queued",
        "ts": submitted_at,
    }
    try:
        await app.state.kafka.produce(
            KAFKA_MIGRATION_JOBS_TOPIC, value=payload, key=str(plan.dataset_id)
        )
    except Exception as e:
        logger.error("Failed to enqueue migration job to stream: %s", e)
    return JobOut(
        job_id=job.job_id,
        status=job.status.value if hasattr(job.status, "value") else job.status,
        dataset_id=job.dataset_id,
        submitted_at=job.submitted_at,
    )


# ------------------------------------------------------------------------------
# Optional: simple jobs view
# ------------------------------------------------------------------------------
@app.get("/jobs", response_model=List[JobOut], tags=["migrations"])
async def list_jobs(db: Session = Depends(get_db)):
    items = []
    for j in db_list_migration_jobs(db):
        items.append(
            JobOut(
                job_id=j.job_id,
                status=j.status.value if hasattr(j.status, "value") else j.status,
                dataset_id=j.dataset_id,
                submitted_at=j.submitted_at,
            )
        )
    return items


@app.get("/jobs/{job_id}", response_model=JobOut, tags=["migrations"])
async def get_job(job_id: str, db: Session = Depends(get_db)):
    j = db_get_migration_job(db, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobOut(
        job_id=j.job_id,
        status=j.status.value if hasattr(j.status, "value") else j.status,
        dataset_id=j.dataset_id,
        submitted_at=j.submitted_at,
    )


@app.post("/jobs/cancel/{job_id}", tags=["migrations"])
async def cancel_job(job_id: str, db: Session = Depends(get_db)):
    """
    Cancel a migration job (demo stub).

    For the demo: emit a 'failed' status with error='cancelled_by_user'.
    If producing to the status topic fails, directly invoke the status handler.
    """
    j = db_get_migration_job(db, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")

    payload = {
        "job_id": j.job_id,
        "dataset_id": j.dataset_id,
        "status": "cancelled",
        "error": "cancelled_by_user",
        "ts": now_ts(),
    }
    try:
        await app.state.kafka.produce(
            KAFKA_MIGRATION_STATUS_TOPIC, value=payload, key=str(j.dataset_id)
        )
    except Exception:
        # Fallback: directly update status in-process
        await handle_migration_status(payload)

    return {"status": "ok", "job_id": j.job_id, "new_status": "cancelled"}


@app.post("/jobs/{job_id}/inject-failure", tags=["migrations"])
async def inject_failure(
    job_id: str, reason: Optional[str] = None, db: Session = Depends(get_db)
):
    global JOB_FAILURES_INJECTED
    j = db_get_migration_job(db, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    j = db_update_migration_job_status(
        db, j, JobStatusEnum.FAILED, reason or "injected failure"
    )
    JOB_FAILURES_INJECTED += 1
    JOB_STATUS_TRANSITIONS["failed"] += 1
    # Emit status to stream so other consumers see the failure
    try:
        await app.state.kafka.produce(
            KAFKA_MIGRATION_STATUS_TOPIC,
            value={
                "job_id": j.job_id,
                "dataset_id": j.dataset_id,
                "status": "failed",
                "error": j.error,
                "ts": now_ts(),
            },
            key=str(j.dataset_id),
        )
    except Exception as e:
        logger.warning("Failed to produce injected failure status: %s", e)
    return JobOut(
        job_id=j.job_id,
        status="failed",
        dataset_id=j.dataset_id,
        submitted_at=j.submitted_at,
    )


# ------------------------------------------------------------------------------
# Entry point (local dev)
# ------------------------------------------------------------------------------
@app.get("/metrics", tags=["health"])
async def metrics():
    """
    Prometheus-style metrics exposition (text format).
    In-memory counters only; reset on restart.
    """
    up_time = time.time() - START_TIME
    lines = []
    # Build info
    lines.append("# HELP app_build_info Build info")
    lines.append("# TYPE app_build_info gauge")
    lines.append(f'app_build_info{{app="{APP_NAME}",version="{APP_VERSION}"}} 1')
    # Uptime
    lines.append("# HELP app_uptime_seconds Service uptime in seconds")
    lines.append("# TYPE app_uptime_seconds gauge")
    lines.append(f"app_uptime_seconds {up_time:.3f}")
    # Requests
    lines.append("# HELP request_count_total Total HTTP requests by path")
    lines.append("# TYPE request_count_total counter")
    for p, c in REQUEST_COUNT.items():
        lines.append(f'request_count_total{{path="{p}"}} {c}')
    # Job status transitions
    lines.append(
        "# HELP job_status_transitions_total Total job status transitions by status"
    )
    lines.append("# TYPE job_status_transitions_total counter")
    for st, c in JOB_STATUS_TRANSITIONS.items():
        lines.append(f'job_status_transitions_total{{status="{st}"}} {c}')
    # Bytes metrics
    lines.append(
        "# HELP access_event_bytes_total Total bytes reported via access events"
    )
    lines.append("# TYPE access_event_bytes_total counter")
    lines.append(f"access_event_bytes_total {EVENT_BYTES_TOTAL}")
    lines.append(
        "# HELP migration_job_bytes_total Total bytes of completed migration jobs (approximate)"
    )
    lines.append("# TYPE migration_job_bytes_total counter")
    lines.append(f"migration_job_bytes_total {JOB_BYTES_TOTAL}")
    # Failures injected
    lines.append(
        "# HELP migration_job_failures_injected_total Number of manually injected job failures"
    )
    lines.append("# TYPE migration_job_failures_injected_total counter")
    lines.append(f"migration_job_failures_injected_total {JOB_FAILURES_INJECTED}")
    content = "\n".join(lines) + "\n"
    return Response(
        content=content, media_type="text/plain; version=0.0.4; charset=utf-8"
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        reload=bool(int(os.getenv("RELOAD", "0"))),
    )
