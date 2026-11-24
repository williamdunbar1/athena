"""Adiciona tabela `conversas` e coluna `conversa_id` em `mensagens` para multiplos chats e agentes diretos.

Revision ID: 004_conversas_metadados
Revises: 003_lab_propostas
Create Date: 2026-11-23 21:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from sqlalchemy import text

revision: str = "004_conversas_metadados"
down_revision: str | Sequence[str] | None = "003_lab_propostas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria tabela `conversas` e coluna `mensagens.conversa_id` de forma 100% idempotente."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS conversas (
            id TEXT PRIMARY KEY,
            titulo TEXT NOT NULL,
            agente_alvo TEXT NOT NULL DEFAULT 'athena',
            criado_em TEXT NOT NULL,
            atualizado_em TEXT NOT NULL
        )
        """
    )

    if not context.is_offline_mode():
        conn = op.get_bind()
        cols_msg = {row[1] for row in conn.execute(text("PRAGMA table_info(mensagens)")).fetchall()}
        if cols_msg and "conversa_id" not in cols_msg:
            op.execute("ALTER TABLE mensagens ADD COLUMN conversa_id TEXT NOT NULL DEFAULT 'principal'")

    op.execute("CREATE INDEX IF NOT EXISTS idx_mensagens_conversa_id ON mensagens (conversa_id)")


def downgrade() -> None:
    """Remove apenas a tabela de metadados de conversas (preserva mensagens historicas)."""
    op.execute("DROP TABLE IF EXISTS conversas")
