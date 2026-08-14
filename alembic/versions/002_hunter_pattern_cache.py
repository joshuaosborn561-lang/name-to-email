from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002_hunter_pattern_cache"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column("people", sa.Column("pattern_source", sa.String(32), nullable=True))
    op.add_column("people", sa.Column("sighted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("people", sa.Column("hunter_confidence", sa.Integer(), nullable=True))
    op.create_table(
        "domain_email_patterns",
        sa.Column("domain", sa.String(255), primary_key=True),
        sa.Column("pattern", sa.String(64), nullable=True),
        sa.Column("organization", sa.String(255), nullable=True),
        sa.Column("sighted_emails", json_type, nullable=False),
        sa.Column("accept_all", sa.Boolean(), nullable=False),
        sa.Column("webmail", sa.Boolean(), nullable=False),
        sa.Column("hunter_confidence", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("domain_email_patterns")
    op.drop_column("people", "hunter_confidence")
    op.drop_column("people", "sighted")
    op.drop_column("people", "pattern_source")
