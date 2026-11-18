# SQLAlchemy models for the tables PropRisk keeps: one fixed office (Tenant)
# and owner (User), the audit log, AI usage metering, the background job
# queue, and the Public Data (site-check) tables. Every table removed with
# PropDesk's other features (accounts, properties, categories, checks,
# bills, push) is dropped here too; the baseline migration (see
# alembic/versions/0001_proprisk_baseline.py) creates exactly this schema.
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Tenant(Base):
    """The single local office. One fixed row, created by the baseline
    migration; PropRisk has no sign-up and never creates a second one."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    monthly_budget_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    """The single local owner. One fixed row; there is no sign-in, so there
    is no password, email or activation-key state to keep."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(16), default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(40))  # e.g. secret.set, secret.delete
    entity_type: Mapped[str] = mapped_column(String(24))
    entity_id: Mapped[str] = mapped_column(String(36))
    detail: Mapped[dict | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AiUsage(Base):
    __tablename__ = "ai_usage"
    __table_args__ = (Index("ix_ai_usage_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[str] = mapped_column(String(36))
    app_slug: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    reserved_cents: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    cost_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    key_source: Mapped[str] = mapped_column(String(12))  # always "tenant" in PropRisk
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Job(Base):
    """Generic background job. One worker process claims queued rows
    atomically (FOR UPDATE SKIP LOCKED + guarded UPDATE); every worker-side
    write is fenced by lease_token. Terminal states (succeeded / failed /
    cancelled) are never overwritten."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_kind_created", "status", "kind", "created_at"),
        Index(
            "uq_jobs_active_retry_per_run",
            text("(payload ->> 'run_id')"),
            unique=True,
            postgresql_where=text(
                "kind = 'site_check' AND status IN ('queued', 'running') "
                "AND jsonb_exists(payload, 'retry_source_ids')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # site_check
    payload: Mapped[dict | None] = mapped_column(JSONB, default=None)
    status: Mapped[str] = mapped_column(String(12), default="queued")
    progress: Mapped[dict | None] = mapped_column(JSONB, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer)
    claimed_by: Mapped[str | None] = mapped_column(String(120), default=None)
    lease_token: Mapped[str | None] = mapped_column(String(36), default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error_code: Mapped[str | None] = mapped_column(String(40), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)


class SiteCheckRun(Base):
    """One Public Data lookup. Status machine: queued | running | completed
    | failed | cancelled. The partial unique index is the one-active-run-
    per-subject backstop; the subject is
    (tenant_id, normalized_address, coalesce(parcel_id, ''))."""

    __tablename__ = "site_check_runs"
    __table_args__ = (
        Index("ix_site_check_runs_tenant_created", "tenant_id", "created_at"),
        Index(
            "uq_site_check_runs_active_subject",
            "tenant_id",
            "normalized_address",
            func.coalesce(Column("parcel_id", String(64)), ""),
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    address: Mapped[str] = mapped_column(String(512))
    parcel_id: Mapped[str | None] = mapped_column(String(64), default=None)
    normalized_address: Mapped[str] = mapped_column(String(512), index=True)
    county: Mapped[str] = mapped_column(String(120), default="")
    # Two-letter state code; "TX" when nothing named a state.
    state: Mapped[str] = mapped_column(String(2), default="TX", server_default="TX")
    lat: Mapped[float | None] = mapped_column(Float, default=None)
    lng: Mapped[float | None] = mapped_column(Float, default=None)
    geocode_source: Mapped[str | None] = mapped_column(String(40), default=None)
    anchor_kind: Mapped[str | None] = mapped_column(String(12), default=None)
    anchor_quality: Mapped[str | None] = mapped_column(String(16), default=None)
    status: Mapped[str] = mapped_column(String(12), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(40), default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)
    mode: Mapped[str] = mapped_column(String(8), default="check")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    sources_queried: Mapped[list | None] = mapped_column(JSONB, default=None)
    sources_failed: Mapped[list | None] = mapped_column(JSONB, default=None)
    adapter_results: Mapped[dict | None] = mapped_column(JSONB, default=None)
    derived: Mapped[dict | None] = mapped_column(JSONB, default=None)
    cards_schema_version: Mapped[int] = mapped_column(Integer, default=2)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class SiteCheckSourceCache(Base):
    """Global read-through cache of PUBLIC source facts — the sole
    non-tenant domain table. Payloads hold public facts only."""

    __tablename__ = "site_check_source_cache"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "scope_key", "producer_version",
            name="uq_site_check_source_cache_scope",
        ),
        Index("ix_site_check_source_cache_key", "cache_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(String(64))
    scope_key: Mapped[str] = mapped_column(String(255))
    producer_version: Mapped[int] = mapped_column(Integer)
    cache_key: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    source_metadata: Mapped[dict | None] = mapped_column(JSONB, default=None)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SiteCheckProviderUsage(Base):
    """Actual upstream provider-call aggregates — one row per
    (run, provider, service), incremented atomically (ON CONFLICT upsert)."""

    __tablename__ = "site_check_provider_usage"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "provider", "service",
            name="uq_site_check_provider_usage_run_provider_service",
        ),
        Index("ix_site_check_provider_usage_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("site_check_runs.id", ondelete="SET NULL"), default=None
    )
    provider: Mapped[str] = mapped_column(String(40))
    service: Mapped[str] = mapped_column(String(60))
    calls: Mapped[int] = mapped_column(Integer, default=0)
    billable_units: Mapped[int] = mapped_column(Integer, default=0)
    retried_calls: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_microdollars: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SiteCheckCoverageGap(Base):
    """Operator-facing demand signal: a capability we could not serve for a
    county. Tenant-deduped — one row per (tenant, capability, state, county)."""

    __tablename__ = "site_check_coverage_gaps"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "capability", "state", "county",
            name="uq_site_check_coverage_gaps_tenant_capability_state_county",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    capability: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(2), default="TX", server_default="TX")
    county: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
