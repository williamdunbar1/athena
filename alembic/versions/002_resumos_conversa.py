"""Adiciona tabela idempotente `resumos_conversa` para compactacao inteligente de contexto.

Revision ID: 002_resumos_conversa
Revises: 001_initial_schema
Create Date: 2026-11-23 13:55:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "002_resumos_conversa"
down_revision: str | Sequence[str] | None = "001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria tabela `resumos_conversa` de forma 100% idempotente sem afetar tabelas existentes."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS resumos_conversa (
            conversa_id TEXT PRIMARY KEY,
            resumo TEXT NOT NULL,
            mensagens_compactadas INTEGER NOT NULL,
            atualizado_em TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    """Remove apenas a tabela auxiliar de resumos compactados."""
    op.execute("DROP TABLE IF EXISTS resumos_conversa")
