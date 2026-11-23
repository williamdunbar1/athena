"""Adiciona tabela idempotente `lab_propostas` para o pipeline Athena Lab (R&D).

Revision ID: 003_lab_propostas
Revises: 002_resumos_conversa
Create Date: 2026-11-23 16:15:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "003_lab_propostas"
down_revision: str | Sequence[str] | None = "002_resumos_conversa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria tabela `lab_propostas` de forma 100% idempotente sem afetar dados existentes."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS lab_propostas (
            id TEXT PRIMARY KEY,
            titulo TEXT NOT NULL,
            categoria TEXT NOT NULL DEFAULT '',
            resumo TEXT NOT NULL DEFAULT '',
            ganho_estimado TEXT NOT NULL DEFAULT '',
            debate_json TEXT NOT NULL DEFAULT '{}',
            branch TEXT NOT NULL DEFAULT '',
            diff_texto TEXT NOT NULL DEFAULT '',
            checks_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pendente',
            criado_em TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    """Remove apenas a tabela auxiliar do Athena Lab."""
    op.execute("DROP TABLE IF EXISTS lab_propostas")
