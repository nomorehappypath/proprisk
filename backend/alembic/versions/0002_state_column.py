"""Add the state to lookups and coverage gaps.

Every existing row was a Texas lookup, so the column defaults to TX. The
coverage-gap unique key gains the state, because county names repeat across
states (Williamson County is in Texas and Tennessee).

Revision ID: 0002_state_column
Revises: 0001_proprisk_baseline
Create Date: 2026-11-17
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_state_column"
down_revision = "0001_proprisk_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "site_check_runs",
        sa.Column("state", sa.String(2), nullable=False, server_default="TX"),
    )
    op.add_column(
        "site_check_coverage_gaps",
        sa.Column("state", sa.String(2), nullable=False, server_default="TX"),
    )
    op.drop_constraint(
        "uq_site_check_coverage_gaps_tenant_capability_county",
        "site_check_coverage_gaps", type_="unique",
    )
    op.create_unique_constraint(
        "uq_site_check_coverage_gaps_tenant_capability_state_county",
        "site_check_coverage_gaps", ["tenant_id", "capability", "state", "county"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_site_check_coverage_gaps_tenant_capability_state_county",
        "site_check_coverage_gaps", type_="unique",
    )
    op.create_unique_constraint(
        "uq_site_check_coverage_gaps_tenant_capability_county",
        "site_check_coverage_gaps", ["tenant_id", "capability", "county"],
    )
    op.drop_column("site_check_coverage_gaps", "state")
    op.drop_column("site_check_runs", "state")
