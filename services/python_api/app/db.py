"""
Database Module (SQLite + SQLAlchemy)
====================================

This module provides:
- Engine / Session management for a SQLite metadata store.
- Declarative ORM models for `Dataset` and `MigrationJob`.
- FastAPI dependency (`get_db`) for request-scoped sessions.
- Utility helpers for initialization and simple conversion to dicts.

Schema Notes (aligned with initial architecture README):
- datasets
    id (PK), name, path_uri, current_tier(hot|warm|cold), latency_slo_ms,
    size_bytes, owner, created_at, updated_at, last_access_ts
- migration_jobs
    job_id (PK, UUID/string), dataset_id (FK), source_uri, dest_uri,
    dest_storage_class, status (queued|copying|verifying|switching|completed|failed|enqueue_failed),
    error (nullable), job_key (unique idempotency key), submitted_at, updated_at

Future Extensions:
- Add tables for replicas, policies, recommendations, models.
- Introduce migrations (Alembic) for evolving schema.
- Replace epoch float timestamps with proper UTC DateTime columns if desired.

Thread Safety:
- A single engine is cached module-wide.
- SessionLocal is a factory; each request/task should acquire its own session.

Environment:
- SQLITE_PATH (default: /data/metadata.db)
"""

from __future__ import annotations

import os
import time
import uuid
import logging
from typing import Generator, Optional, Dict, Any

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    Text,
    Enum,
    ForeignKey,
    UniqueConstraint,
    event,
    Boolean,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
logger = logging.getLogger("db")
logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))

# ------------------------------------------------------------------------------
# Configuration / Engine Setup
# ------------------------------------------------------------------------------
SQLITE_PATH = os.getenv("SQLITE_PATH", "/data/metadata.db")

# Ensure directory exists (best-effort)
_db_dir = os.path.dirname(SQLITE_PATH)
if _db_dir and not os.path.exists(_db_dir):
    try:
        os.makedirs(_db_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not create db directory %s: %s", _db_dir, e)

DATABASE_URL = f"sqlite:///{SQLITE_PATH}"

# SQLite pragma optimizations can be added for performance tuning if needed.
_engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=_engine, autoflush=False, autocommit=False, future=True
)

Base = declarative_base()


# ------------------------------------------------------------------------------
# Timestamp Helpers
# ------------------------------------------------------------------------------
def now_ts() -> float:
    """Return current time as epoch float (UTC)."""
    return time.time()


# ------------------------------------------------------------------------------
# Mixins
# ------------------------------------------------------------------------------
class TimestampMixin:
    created_at = Column(Float, nullable=False, default=lambda: now_ts())
    updated_at = Column(Float, nullable=False, default=lambda: now_ts())

    def touch(self):
        self.updated_at = now_ts()


class DictMixin:
    def to_dict(self) -> Dict[str, Any]:
        out = {}
        for col in self.__table__.columns:  # type: ignore[attr-defined]
            out[col.name] = getattr(self, col.name)
        return out


# ------------------------------------------------------------------------------
# Enumerations
# ------------------------------------------------------------------------------
from enum import Enum as PyEnum  # Separate from SQLAlchemy Enum


class TierEnum(PyEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class JobStatusEnum(PyEnum):
    QUEUED = "queued"
    COPYING = "copying"
    VERIFYING = "verifying"
    SWITCHING = "switching"
    COMPLETED = "completed"
    FAILED = "failed"
    ENQUEUE_FAILED = "enqueue_failed"


# ------------------------------------------------------------------------------
# ORM Models
class Replica(Base, TimestampMixin, DictMixin):
    __tablename__ = "replicas"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(
        Integer,
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    location = Column(Text, nullable=False)
    storage_class = Column(String(64), nullable=True)
    checksum = Column(String(128), nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)

    dataset = relationship("Dataset", back_populates="replicas")


class Recommendation(Base, TimestampMixin, DictMixin):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(
        Integer,
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recommended_tier = Column(Enum(TierEnum), nullable=False)
    recommended_location = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)  # JSON string or free text
    confidence = Column(Float, nullable=False, default=0.0)

    dataset = relationship("Dataset", back_populates="recommendations")


class Policy(Base, TimestampMixin, DictMixin):
    __tablename__ = "policies"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(
        Integer,
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )  # NULL => global policy
    max_cost_per_gb_month = Column(Float, nullable=True)
    latency_slo_ms = Column(Integer, nullable=True)
    min_redundancy = Column(Integer, nullable=True)
    encryption_required = Column(Boolean, nullable=False, default=False)
    region_preferences = Column(Text, nullable=True)  # JSON list/obj

    dataset = relationship("Dataset", back_populates="policies")


# Extend existing relationships on Dataset
# (Removed early relationship assignments; relationships now defined inside Dataset class.)


# CRUD Helpers: Replica
def create_replica(
    db: Session,
    dataset_id: int,
    location: str,
    storage_class: Optional[str] = None,
    checksum: Optional[str] = None,
    is_primary: bool = False,
) -> Replica:
    rep = Replica(
        dataset_id=dataset_id,
        location=location,
        storage_class=storage_class,
        checksum=checksum,
        is_primary=is_primary,
    )
    db.add(rep)
    if is_primary:
        # Demote other primaries
        db.query(Replica).filter(
            Replica.dataset_id == dataset_id,
            Replica.is_primary == True,  # noqa: E712
        ).update({"is_primary": False})
    db.commit()
    db.refresh(rep)
    return rep


def list_replicas_for_dataset(db: Session, dataset_id: int) -> list[Replica]:
    return (
        db.query(Replica)
        .filter(Replica.dataset_id == dataset_id)
        .order_by(Replica.is_primary.desc(), Replica.id.asc())
        .all()
    )


def set_primary_replica(db: Session, replica_id: int) -> Optional[Replica]:
    rep = db.query(Replica).filter(Replica.id == replica_id).one_or_none()
    if not rep:
        return None
    # Demote others
    db.query(Replica).filter(
        Replica.dataset_id == rep.dataset_id,
        Replica.is_primary == True,  # noqa: E712
    ).update({"is_primary": False})
    rep.is_primary = True
    rep.touch()
    db.commit()
    db.refresh(rep)
    return rep


# CRUD Helpers: Recommendation
def create_recommendation(
    db: Session,
    dataset_id: int,
    recommended_tier: TierEnum,
    recommended_location: Optional[str],
    reason: Optional[str],
    confidence: float,
) -> Recommendation:
    reco = Recommendation(
        dataset_id=dataset_id,
        recommended_tier=recommended_tier,
        recommended_location=recommended_location,
        reason=reason,
        confidence=confidence,
    )
    db.add(reco)
    db.commit()
    db.refresh(reco)
    return reco


def list_recommendations(
    db: Session, dataset_id: Optional[int] = None, limit: int = 50
) -> list[Recommendation]:
    q = db.query(Recommendation)
    if dataset_id is not None:
        q = q.filter(Recommendation.dataset_id == dataset_id)
    return q.order_by(Recommendation.created_at.desc()).limit(limit).all()


# CRUD Helpers: Policy
def create_policy(
    db: Session,
    dataset_id: Optional[int],
    max_cost_per_gb_month: Optional[float] = None,
    latency_slo_ms: Optional[int] = None,
    min_redundancy: Optional[int] = None,
    encryption_required: bool = False,
    region_preferences: Optional[str] = None,
) -> Policy:
    pol = Policy(
        dataset_id=dataset_id,
        max_cost_per_gb_month=max_cost_per_gb_month,
        latency_slo_ms=latency_slo_ms,
        min_redundancy=min_redundancy,
        encryption_required=encryption_required,
        region_preferences=region_preferences,
    )
    db.add(pol)
    db.commit()
    db.refresh(pol)
    return pol


def get_policy(db: Session, policy_id: int) -> Optional[Policy]:
    return db.query(Policy).filter(Policy.id == policy_id).one_or_none()


def list_policies(db: Session, dataset_id: Optional[int] = None) -> list[Policy]:
    q = db.query(Policy)
    if dataset_id is not None:
        q = q.filter(Policy.dataset_id == dataset_id)
    return q.order_by(Policy.id.asc()).all()


# ------------------------------------------------------------------------------
class Dataset(Base, TimestampMixin, DictMixin):
    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True)
    path_uri = Column(Text, nullable=False)
    current_tier = Column(Enum(TierEnum), nullable=False, default=TierEnum.HOT)
    latency_slo_ms = Column(Integer, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    owner = Column(String(128), nullable=True)
    last_access_ts = Column(Float, nullable=True)

    # Relationships
    migration_jobs = relationship(
        "MigrationJob",
        back_populates="dataset",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    replicas = relationship(
        "Replica",
        back_populates="dataset",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    recommendations = relationship(
        "Recommendation",
        back_populates="dataset",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    policies = relationship(
        "Policy",
        back_populates="dataset",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MigrationJob(Base, TimestampMixin, DictMixin):
    __tablename__ = "migration_jobs"
    __table_args__ = (UniqueConstraint("job_key", name="uq_migration_jobs_job_key"),)

    job_id = Column(String(64), primary_key=True, index=True)
    job_key = Column(String(128), nullable=False)
    dataset_id = Column(
        Integer,
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_uri = Column(Text, nullable=False)
    dest_uri = Column(Text, nullable=True)
    dest_storage_class = Column(String(64), nullable=True)
    status = Column(Enum(JobStatusEnum), nullable=False, default=JobStatusEnum.QUEUED)
    error = Column(Text, nullable=True)
    submitted_at = Column(Float, nullable=False, default=lambda: now_ts())

    dataset = relationship("Dataset", back_populates="migration_jobs")

    def mark_status(self, new_status: JobStatusEnum, error: Optional[str] = None):
        self.status = new_status
        self.error = error
        self.touch()


# ------------------------------------------------------------------------------
# SQLAlchemy Events (auto-update updated_at)
# ------------------------------------------------------------------------------
@event.listens_for(Dataset, "before_update")
def _dataset_before_update(mapper, connection, target):  # noqa: D401
    target.updated_at = now_ts()


@event.listens_for(MigrationJob, "before_update")
def _migration_job_before_update(mapper, connection, target):  # noqa: D401
    target.updated_at = now_ts()


# ------------------------------------------------------------------------------
# Session / Dependency
# ------------------------------------------------------------------------------
def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency generator.
    Usage:
        def endpoint(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Initialization
# ------------------------------------------------------------------------------
def init_db() -> None:
    """
    Create all tables. Safe to call multiple times (idempotent).
    """
    logger.info("Initializing database (url=%s)", DATABASE_URL)
    Base.metadata.create_all(bind=_engine)


# ------------------------------------------------------------------------------
# Convenience CRUD Helpers (MVP)
# ------------------------------------------------------------------------------
def create_dataset(
    db: Session,
    name: str,
    path_uri: str,
    latency_slo_ms: Optional[int] = None,
    size_bytes: Optional[int] = None,
    owner: Optional[str] = None,
) -> Dataset:
    ds = Dataset(
        name=name,
        path_uri=path_uri,
        latency_slo_ms=latency_slo_ms,
        size_bytes=size_bytes,
        owner=owner,
        current_tier=TierEnum.HOT,
    )
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return ds


def get_dataset(db: Session, dataset_id: int) -> Optional[Dataset]:
    return db.query(Dataset).filter(Dataset.id == dataset_id).one_or_none()


def list_datasets(db: Session) -> list[Dataset]:
    return db.query(Dataset).order_by(Dataset.id.asc()).all()


def update_dataset(
    db: Session,
    dataset: Dataset,
    **updates,
) -> Dataset:
    for k, v in updates.items():
        if hasattr(dataset, k) and v is not None:
            setattr(dataset, k, v)
    dataset.touch()
    db.commit()
    db.refresh(dataset)
    return dataset


def create_migration_job(
    db: Session,
    dataset_id: int,
    source_uri: str,
    dest_uri: Optional[str],
    dest_storage_class: Optional[str],
    job_key: Optional[str] = None,
) -> MigrationJob:
    if job_key is None:
        job_key = f"dataset:{dataset_id}:ts:{int(now_ts())}"

    job = MigrationJob(
        job_id=str(uuid.uuid4()),
        job_key=job_key,
        dataset_id=dataset_id,
        source_uri=source_uri,
        dest_uri=dest_uri,
        dest_storage_class=dest_storage_class,
        status=JobStatusEnum.QUEUED,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_migration_job(db: Session, job_id: str) -> Optional[MigrationJob]:
    return db.query(MigrationJob).filter(MigrationJob.job_id == job_id).one_or_none()


def list_migration_jobs(
    db: Session, dataset_id: Optional[int] = None
) -> list[MigrationJob]:
    q = db.query(MigrationJob)
    if dataset_id is not None:
        q = q.filter(MigrationJob.dataset_id == dataset_id)
    return q.order_by(MigrationJob.submitted_at.desc()).all()


def update_migration_job_status(
    db: Session,
    job: MigrationJob,
    new_status: JobStatusEnum,
    error: Optional[str] = None,
) -> MigrationJob:
    job.mark_status(new_status, error)
    db.commit()
    db.refresh(job)
    return job


# ------------------------------------------------------------------------------
# Module Initialization (Optional eager init)
# ------------------------------------------------------------------------------
if os.getenv("DB_AUTO_INIT", "1") == "1":
    try:
        init_db()
    except Exception as e:  # noqa: BLE001
        logger.error("Database init failed: %s", e)
