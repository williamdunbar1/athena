"""Adiciona tabela `fatos_temporais` e indice composto para Memoria Temporal em Grafo.

Revision ID: 005_memoria_temporal
Revises: 004_conversas_metadados
Create Date: 2026-11-23 22:18:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "005_memoria_temporal"
down_revision: str | Sequence[str] | None = "004_conversas_metadados"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria tabela `fatos_temporais` e indice `(sujeito, relacao, valido_ate)` de forma idempotente."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fatos_temporais (
            id TEXT PRIMARY KEY,
            sujeito TEXT NOT NULL,
            relacao TEXT NOT NULL,
            objeto TEXT NOT NULL,
            categoria TEXT NOT NULL DEFAULT 'geral',
            origem TEXT NOT NULL DEFAULT 'chat',
            valido_de TEXT NOT NULL,
            valido_ate TEXT,
            criado_em TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fatos_temporais_sujeito_relacao_valido
        ON fatos_temporais (sujeito, relacao, valido_ate)
        """
    )


def downgrade() -> None:
    """Remove a tabela `fatos_temporais` e seu indice."""
    op.execute("DROP INDEX IF EXISTS idx_fatos_temporais_sujeito_relacao_valido")
    op.execute("DROP TABLE IF EXISTS fatos_temporais")
