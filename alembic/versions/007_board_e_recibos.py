"""Adiciona tabelas `board_tarefas` (The Board + Handoff + Checker) e `recibos_semanais` (Friday Receipt).

Revision ID: 007_board_e_recibos
Revises: 006_rotinas_dinamicas_e_auditoria
Create Date: 2026-11-23 23:48:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "007_board_e_recibos"
down_revision: str | Sequence[str] | None = "006_rotinas_dinamicas_e_auditoria"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria tabelas `board_tarefas` e `recibos_semanais` de forma idempotente."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS board_tarefas (
            id TEXT PRIMARY KEY,
            titulo TEXT NOT NULL,
            dono TEXT NOT NULL,
            status TEXT NOT NULL,
            criterio_done TEXT,
            handoff_json TEXT,
            provas_json TEXT,
            tentativas_checker INTEGER NOT NULL DEFAULT 0,
            criado_em TEXT NOT NULL,
            atualizado_em TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_board_tarefas_status
        ON board_tarefas (status, atualizado_em DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS recibos_semanais (
            id TEXT PRIMARY KEY,
            periodo TEXT NOT NULL,
            resumo_md TEXT NOT NULL,
            metricas_json TEXT NOT NULL,
            criado_em TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_recibos_semanais_criado_em
        ON recibos_semanais (criado_em DESC)
        """
    )


def downgrade() -> None:
    """Remove tabelas `board_tarefas` e `recibos_semanais`."""
    op.execute("DROP INDEX IF EXISTS idx_recibos_semanais_criado_em")
    op.execute("DROP TABLE IF EXISTS recibos_semanais")
    op.execute("DROP INDEX IF EXISTS idx_board_tarefas_status")
    op.execute("DROP TABLE IF EXISTS board_tarefas")
