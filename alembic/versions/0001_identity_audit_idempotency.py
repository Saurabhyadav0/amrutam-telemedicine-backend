"""identity, consents, tokens, idempotency, partitioned hash-chained audit log

Revision ID: 0001
Revises:
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
UUID = postgresql.UUID(as_uuid=True)

# ---- audit log: monthly range partitions, append-only, hash-chained ---------------------
AUDIT_DDL = [
    """
    CREATE TABLE audit_logs (
        id            bigint       NOT NULL,
        occurred_at   timestamptz  NOT NULL,
        actor_id      uuid,
        actor_role    varchar(16),
        action        varchar(64)  NOT NULL,
        resource_type varchar(64),
        resource_id   varchar(64),
        ip            varchar(45),
        user_agent    varchar(512),
        request_id    varchar(64),
        metadata      jsonb        NOT NULL DEFAULT '{}'::jsonb,
        prev_hash     varchar(64)  NOT NULL,
        hash          varchar(64)  NOT NULL,
        CONSTRAINT pk_audit_logs PRIMARY KEY (id, occurred_at)
    ) PARTITION BY RANGE (occurred_at)
    """,
    "CREATE SEQUENCE audit_logs_id_seq OWNED BY audit_logs.id",
    "CREATE INDEX ix_audit_logs_actor ON audit_logs (actor_id, occurred_at DESC)",
    "CREATE INDEX ix_audit_logs_resource ON audit_logs (resource_type, resource_id, occurred_at DESC)",
    "CREATE INDEX ix_audit_logs_action ON audit_logs (action, occurred_at DESC)",
    # Safety net only: the maintenance job keeps partitions created months ahead.
    "CREATE TABLE audit_logs_default PARTITION OF audit_logs DEFAULT",
    """
    CREATE OR REPLACE FUNCTION create_monthly_partitions(parent text, months_ahead int)
    RETURNS void LANGUAGE plpgsql AS $$
    DECLARE
        month_start date := date_trunc('month', now() AT TIME ZONE 'UTC')::date;
        from_month  date;
    BEGIN
        FOR i IN 0..months_ahead LOOP
            from_month := (month_start + make_interval(months => i))::date;
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                parent || '_' || to_char(from_month, 'YYYY_MM'),
                parent,
                from_month::text || ' 00:00:00+00',
                (from_month + interval '1 month')::date::text || ' 00:00:00+00'
            );
        END LOOP;
    END;
    $$
    """,
    "SELECT create_monthly_partitions('audit_logs', 3)",
    """
    CREATE OR REPLACE FUNCTION forbid_audit_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_logs is append-only (% blocked)', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END;
    $$
    """,
    """
    CREATE TRIGGER audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation()
    """,
    """
    CREATE TABLE audit_chain_head (
        id        smallint    PRIMARY KEY CHECK (id = 1),
        last_id   bigint      NOT NULL,
        last_hash varchar(64) NOT NULL
    )
    """,
    "INSERT INTO audit_chain_head (id, last_id, last_hash) VALUES (1, 0, repeat('0', 64))",
]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("mfa_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("mfa_secret_enc", sa.Text),
        sa.Column("mfa_last_used_step", sa.BigInteger),
        sa.Column("failed_login_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", TS),
        sa.Column("last_login_at", TS),
        sa.Column("deleted_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("role IN ('patient','doctor','admin','support')", name="ck_users_role"),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_users_status"),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_lower"),
    )
    op.create_index("ix_users_role", "users", ["role"])
    op.create_index(
        "ix_users_created_at", "users", [sa.text("created_at DESC"), sa.text("id DESC")]
    )

    op.create_table(
        "profiles",
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("phone_enc", sa.Text),
        sa.Column("phone_bidx", sa.String(64)),
        sa.Column("date_of_birth_enc", sa.Text),
        sa.Column("gender", sa.String(16)),
        sa.Column("preferred_language", sa.String(16), nullable=False, server_default="en"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("phone_bidx", name="uq_profiles_phone_bidx"),
    )

    op.create_table(
        "consents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("granted", sa.Boolean, nullable=False),
        sa.Column("ip", sa.String(45)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_consents_user_purpose", "consents", ["user_id", "purpose", sa.text("created_at DESC")]
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", UUID, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("amr", sa.String(64), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("rotated_at", TS),
        sa.Column("revoked_at", TS),
        sa.Column("revoke_reason", sa.String(32)),
        sa.Column("ip", sa.String(45)),
        sa.Column("user_agent", sa.String(512)),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("used_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mfa_recovery_codes_user_id", "mfa_recovery_codes", ["user_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("owner", sa.String(64), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response_status", sa.Integer),
        sa.Column("response_headers", postgresql.JSONB),
        sa.Column("response_body", sa.LargeBinary),
        sa.Column("locked_until", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TS, nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress','completed')", name="ck_idempotency_keys_status"
        ),
    )
    # Supports the purge job: DELETE .. WHERE expires_at < now().
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])

    # asyncpg runs one statement per call, so the raw DDL is a list.
    for statement in AUDIT_DDL:
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE IF EXISTS audit_chain_head",
        "DROP TABLE IF EXISTS audit_logs CASCADE",
        "DROP FUNCTION IF EXISTS forbid_audit_mutation()",
        "DROP FUNCTION IF EXISTS create_monthly_partitions(text, int)",
    ):
        op.execute(statement)
    for table in (
        "idempotency_keys",
        "mfa_recovery_codes",
        "refresh_tokens",
        "consents",
        "profiles",
        "users",
    ):
        op.drop_table(table)
