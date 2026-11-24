"""Adiciona tabelas `rotinas_dinamicas`, `atividade_agentes` e `pastas_aprovadas` (OpenBot-style).

Revision ID: 006_rotinas_dinamicas_e_auditoria
Revises: 005_memoria_temporal
Create Date: 2026-11-23 23:06:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "006_rotinas_dinamicas_e_auditoria"
down_revision: str | Sequence[str] | None = "005_memoria_temporal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria tabelas `rotinas_dinamicas`, `atividade_agentes` e `pastas_aprovadas` de forma idempotente."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rotinas_dinamicas (
            id TEXT PRIMARY KEY,
            nome TEXT NOT NULL,
            instrucao TEXT NOT NULL,
            cron_expr TEXT NOT NULL,
            agente_alvo TEXT NOT NULL DEFAULT 'athena',
            ativa INTEGER NOT NULL DEFAULT 1,
            falhas_consecutivas INTEGER NOT NULL DEFAULT 0,
            ultima_execucao TEXT,
            ultimo_resultado TEXT,
            criado_em TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS atividade_agentes (
            id TEXT PRIMARY KEY,
            agente TEXT NOT NULL,
            acao TEXT NOT NULL,
            alvo TEXT NOT NULL,
            status TEXT NOT NULL,
            detalhes TEXT,
            criado_em TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_atividade_agentes_criado_em
        ON atividade_agentes (criado_em DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_atividade_agentes_agente
        ON atividade_agentes (agente)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pastas_aprovadas (
            caminho TEXT PRIMARY KEY,
            somente_leitura INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    """Remove tabelas de auditoria, permissoes por pasta e rotinas dinamicas."""
    op.execute("DROP TABLE IF EXISTS pastas_aprovadas")
    op.execute("DROP INDEX IF EXISTS idx_atividade_agentes_agente")
    op.execute("DROP INDEX IF EXISTS idx_atividade_agentes_criado_em")
    op.execute("DROP TABLE IF EXISTS atividade_agentes")
    op.execute("DROP TABLE IF EXISTS rotinas_dinamicas")
