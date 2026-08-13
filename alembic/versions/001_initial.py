from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("cost_ceiling", sa.Numeric(12, 6), nullable=True),
        sa.Column("stats", json_type, nullable=False),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_table(
        "people",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("first", sa.String(255), nullable=False),
        sa.Column("last", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("norm_first", sa.String(255), nullable=False),
        sa.Column("norm_last", sa.String(255), nullable=False),
        sa.Column("norm_domain", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("pattern_used", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("verifier", sa.String(64), nullable=True),
        sa.Column("domain_is_catchall", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=True),
        sa.Column("passthrough", json_type, nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.create_index("ix_people_run_id", "people", ["run_id"])
    op.create_index("ix_people_status", "people", ["status"])
    op.create_index("ix_people_run_domain", "people", ["run_id", "norm_domain"])
    op.create_index("ix_people_run_status", "people", ["run_id", "status"])
    op.create_table(
        "domain_patterns",
        sa.Column("domain", sa.String(255), primary_key=True),
        sa.Column("pattern", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("is_catchall", sa.Boolean(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("catchall_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "verifications",
        sa.Column("email", sa.String(320), primary_key=True),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("verifier", sa.String(64), nullable=False),
        sa.Column("cost", sa.Numeric(12, 6), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", json_type, nullable=True),
    )
    op.create_index("ix_verifications_verdict", "verifications", ["verdict"])


def downgrade() -> None:
    op.drop_table("verifications")
    op.drop_table("domain_patterns")
    op.drop_table("people")
    op.drop_table("runs")
