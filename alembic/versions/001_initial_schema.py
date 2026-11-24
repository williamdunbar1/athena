"""Schema inicial idempotente do memoria.db (mensagens, fatos, telemetria, webauthn_credenciais).

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-11-23 13:25:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from sqlalchemy import text

revision: str = "001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria tabelas iniciais de forma 100% idempotente, preservando todos os dados ja existentes."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mensagens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            conteudo TEXT NOT NULL,
            trilha_json TEXT DEFAULT '[]',
            criado_em TEXT NOT NULL,
            meta_json TEXT DEFAULT '{}'
        )
        """
    )

    if not context.is_offline_mode():
        conn = op.get_bind()
        cols_msg = {row[1] for row in conn.execute(text("PRAGMA table_info(mensagens)")).fetchall()}
        if "meta_json" not in cols_msg:
            op.execute("ALTER TABLE mensagens ADD COLUMN meta_json TEXT DEFAULT '{}'")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fatos (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL,
            atualizado_em TEXT NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT NOT NULL,
            modelo TEXT NOT NULL,
            duracao_ms INTEGER NOT NULL,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            agentes_usados TEXT DEFAULT '',
            sucesso INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webauthn_credenciais (
            credential_id TEXT PRIMARY KEY,
            public_key BLOB NOT NULL,
            sign_count INTEGER NOT NULL,
            nome TEXT,
            criado_em TEXT
        )
        """
    )


def downgrade() -> None:
    """Migration inicial preserva dados historicos do usuario por seguranca (no-op)."""
    pass
