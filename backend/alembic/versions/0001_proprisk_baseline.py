"""PropRisk baseline schema.

Creates every table PropRisk keeps (office, owner, audit log, AI usage, the
job queue, and the Public Data tables), exactly matching app/models.py, and
inserts the one fixed office row and one fixed owner row every request
resolves to (see app/core/local_identity.py). There is no earlier migration
to build on: this is the only one.

Revision ID: 0001_proprisk_baseline
Revises:
Create Date: 2026-11-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_proprisk_baseline"
down_revision = None
branch_labels = None
depends_on = None

LOCAL_TENANT_ID = "00000000-0000-0000-0000-000000000001"
LOCAL_USER_ID = "00000000-0000-0000-0000-000000000002"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("monthly_budget_cents", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="owner"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("entity_type", sa.String(24), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_tenant_created", "audit_log", ["tenant_id", "created_at"])

    op.create_table(
        "ai_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("app_slug", sa.String(40), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reserved_cents", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_cents", sa.Integer(), nullable=True),
        sa.Column("key_source", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ai_usage_tenant_created", "ai_usage", ["tenant_id", "created_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("progress", postgresql.JSONB(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(120), nullable=True),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_status_kind_created", "jobs", ["status", "kind", "created_at"])
    op.create_index(
        "uq_jobs_active_retry_per_run",
        "jobs",
        [sa.text("(payload ->> 'run_id')")],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'site_check' AND status IN ('queued', 'running') "
            "AND jsonb_exists(payload, 'retry_source_ids')"
        ),
    )

    op.create_table(
        "site_check_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("address", sa.String(512), nullable=False),
        sa.Column("parcel_id", sa.String(64), nullable=True),
        sa.Column("normalized_address", sa.String(512), nullable=False),
        sa.Column("county", sa.String(120), nullable=False, server_default=""),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("geocode_source", sa.String(40), nullable=True),
        sa.Column("anchor_kind", sa.String(12), nullable=True),
        sa.Column("anchor_quality", sa.String(16), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(8), nullable=False, server_default="check"),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sources_queried", postgresql.JSONB(), nullable=True),
        sa.Column("sources_failed", postgresql.JSONB(), nullable=True),
        sa.Column("adapter_results", postgresql.JSONB(), nullable=True),
        sa.Column("derived", postgresql.JSONB(), nullable=True),
        sa.Column("cards_schema_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_site_check_runs_normalized_address", "site_check_runs", ["normalized_address"])
    op.create_index(
        "ix_site_check_runs_tenant_created", "site_check_runs", ["tenant_id", "created_at"]
    )
    op.create_index(
        "uq_site_check_runs_active_subject",
        "site_check_runs",
        ["tenant_id", "normalized_address", sa.text("coalesce(parcel_id, '')")],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "site_check_source_cache",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.String(255), nullable=False),
        sa.Column("producer_version", sa.Integer(), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("source_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_id", "scope_key", "producer_version",
            name="uq_site_check_source_cache_scope",
        ),
    )
    op.create_index("ix_site_check_source_cache_key", "site_check_source_cache", ["cache_key"])

    op.create_table(
        "site_check_provider_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "run_id", sa.String(36),
            sa.ForeignKey("site_check_runs.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("service", sa.String(60), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("billable_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retried_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_microdollars", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id", "provider", "service",
            name="uq_site_check_provider_usage_run_provider_service",
        ),
    )
    op.create_index(
        "ix_site_check_provider_usage_tenant_created",
        "site_check_provider_usage", ["tenant_id", "created_at"],
    )

    op.create_table(
        "site_check_coverage_gaps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("capability", sa.String(60), nullable=False),
        sa.Column("county", sa.String(120), nullable=False),
        sa.Column("reason", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "capability", "county",
            name="uq_site_check_coverage_gaps_tenant_capability_county",
        ),
    )
    op.create_index("ix_site_check_coverage_gaps_tenant_id", "site_check_coverage_gaps", ["tenant_id"])

    # The one office and one owner every request resolves to (app/api/deps.py).
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, name, monthly_budget_cents, created_at) "
            "VALUES (:id, 'Local', 2000, now())"
        ).bindparams(id=LOCAL_TENANT_ID)
    )
    op.execute(
        sa.text(
            "INSERT INTO users (id, tenant_id, display_name, role, created_at) "
            "VALUES (:id, :tenant_id, 'Local owner', 'owner', now())"
        ).bindparams(id=LOCAL_USER_ID, tenant_id=LOCAL_TENANT_ID)
    )


def downgrade() -> None:
    op.drop_table("site_check_coverage_gaps")
    op.drop_table("site_check_provider_usage")
    op.drop_table("site_check_source_cache")
    op.drop_table("site_check_runs")
    op.drop_table("jobs")
    op.drop_table("ai_usage")
    op.drop_table("audit_log")
    op.drop_table("users")
    op.drop_table("tenants")
