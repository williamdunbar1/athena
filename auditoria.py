"""Trilha de Auditoria Unificada ("Activity") e Permissoes por Pasta revogaveis (SQLite WAL).

Recursos:
- `registrar_atividade` / `listar_atividades`: registra acoes reais executadas pelos agentes
  (Windows, Faxina, Podman, Athena Lab, Rotinas Dinamicas) com mascaramento OWASP via `guardrails`.
- `aprovar_pasta` / `listar_pastas_aprovadas` / `revogar_pasta`: CRUD seguro e revogavel de pastas autorizadas.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from guardrails import mascarar_segredos_e_pii
from memoria import obter_db_path

_PASTAS_SISTEMA_PROIBIDAS: tuple[str, ...] = (
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/system",
)


def _garantir_tabelas_auditoria(c: sqlite3.Connection) -> None:
    c.execute(
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
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_atividade_agentes_criado_em
        ON atividade_agentes (criado_em DESC)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_atividade_agentes_agente
        ON atividade_agentes (agente)
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS pastas_aprovadas (
            caminho TEXT PRIMARY KEY,
            somente_leitura INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL
        )
        """
    )


@contextmanager
def _conn_auditoria(db_path: str | Path | None = None):
    alvo = obter_db_path(db_path)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(alvo, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        _garantir_tabelas_auditoria(c)
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _row_para_atividade(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": str(row[0]),
        "agente": str(row[1]),
        "acao": str(row[2]),
        "alvo": str(row[3]),
        "status": str(row[4]),
        "detalhes": str(row[5] or ""),
        "criado_em": str(row[6]),
    }


def registrar_atividade(
    agente: str,
    acao: str,
    alvo: str,
    status: str,
    detalhes: str = "",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Grava um evento na tabela `atividade_agentes`, mascarando segredos/PII em `detalhes` e `alvo`."""
    ag_limpo = (" ".join((agente or "Athena").split()) or "Athena")[:80]
    ac_limpa = (" ".join((acao or "acao").split()) or "acao")[:80]
    alvo_seg, _ = mascarar_segredos_e_pii(" ".join((alvo or "-").split())[:300])
    st_limpo = (" ".join((status or "ok").split()) or "ok")[:40]
    det_seg, _ = mascarar_segredos_e_pii((detalhes or "").strip()[:1200])

    act_id = f"act_{uuid.uuid4().hex[:12]}"
    agora = datetime.now().isoformat(timespec="microseconds")

    with _conn_auditoria(db_path) as c:
        c.execute(
            """
            INSERT INTO atividade_agentes (id, agente, acao, alvo, status, detalhes, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (act_id, ag_limpo, ac_limpa, alvo_seg, st_limpo, det_seg, agora),
        )
        row = c.execute(
            "SELECT id, agente, acao, alvo, status, detalhes, criado_em FROM atividade_agentes WHERE id = ?",
            (act_id,),
        ).fetchone()
    return _row_para_atividade(row)


def listar_atividades(
    limite: int = 80,
    agente: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Lista atividades em ordem cronologica reversa com filtro opcional por agente."""
    lim = max(1, min(int(limite or 80), 300))
    ag_filtro = (agente or "").strip()
    with _conn_auditoria(db_path) as c:
        if ag_filtro:
            rows = c.execute(
                """
                SELECT id, agente, acao, alvo, status, detalhes, criado_em
                FROM atividade_agentes
                WHERE LOWER(agente) = LOWER(?)
                ORDER BY criado_em DESC, rowid DESC
                LIMIT ?
                """,
                (ag_filtro, lim),
            ).fetchall()
        else:
            rows = c.execute(
                """
                SELECT id, agente, acao, alvo, status, detalhes, criado_em
                FROM atividade_agentes
                ORDER BY criado_em DESC, rowid DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
    return [_row_para_atividade(r) for r in rows]


def _normalizar_caminho_pasta(caminho: str) -> str:
    bruto = (caminho or "").strip()
    if not bruto or "\x00" in bruto:
        raise ValueError("Caminho de pasta invalido.")
    resolvido = str(Path(bruto).expanduser().resolve())
    low = resolvido.lower().rstrip("\\/")
    for proibido in _PASTAS_SISTEMA_PROIBIDAS:
        if low == proibido or low.startswith(proibido + "\\") or low.startswith(proibido + "/"):
            raise ValueError(f"Pasta de sistema bloqueada por seguranca: '{resolvido}'.")
    return resolvido


def aprovar_pasta(
    caminho: str,
    somente_leitura: bool = True,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Registra ou atualiza uma pasta aprovada em `pastas_aprovadas` e grava na trilha de auditoria."""
    caminho_norm = _normalizar_caminho_pasta(caminho)
    ro_int = 1 if bool(somente_leitura) else 0
    agora = datetime.now().isoformat(timespec="seconds")

    with _conn_auditoria(db_path) as c:
        c.execute(
            """
            INSERT INTO pastas_aprovadas (caminho, somente_leitura, criado_em)
            VALUES (?, ?, ?)
            ON CONFLICT(caminho) DO UPDATE SET
                somente_leitura = excluded.somente_leitura,
                criado_em = excluded.criado_em
            """,
            (caminho_norm, ro_int, agora),
        )
        row = c.execute(
            "SELECT caminho, somente_leitura, criado_em FROM pastas_aprovadas WHERE caminho = ?",
            (caminho_norm,),
        ).fetchone()

    registrar_atividade(
        agente="Permissoes",
        acao="aprovar_pasta",
        alvo=caminho_norm,
        status="sucesso",
        detalhes="somente_leitura" if ro_int else "leitura_e_escrita",
        db_path=db_path,
    )
    return {
        "caminho": str(row[0]),
        "somente_leitura": bool(row[1]),
        "criado_em": str(row[2]),
    }


def listar_pastas_aprovadas(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Lista todas as pastas aprovadas em `pastas_aprovadas`."""
    with _conn_auditoria(db_path) as c:
        rows = c.execute(
            "SELECT caminho, somente_leitura, criado_em FROM pastas_aprovadas ORDER BY criado_em DESC, caminho ASC"
        ).fetchall()
    return [
        {
            "caminho": str(r[0]),
            "somente_leitura": bool(r[1]),
            "criado_em": str(r[2]),
        }
        for r in rows
    ]


def revogar_pasta(caminho: str, db_path: str | Path | None = None) -> bool:
    """Revoga a permissao de uma pasta em `pastas_aprovadas` e registra na auditoria."""
    bruto = (caminho or "").strip()
    if not bruto:
        return False
    candidatos = {bruto}
    try:
        candidatos.add(str(Path(bruto).expanduser().resolve()))
    except OSError:
        pass

    removidos = 0
    with _conn_auditoria(db_path) as c:
        for cand in candidatos:
            cur = c.execute("DELETE FROM pastas_aprovadas WHERE caminho = ?", (cand,))
            removidos += int(cur.rowcount or 0)

    if removidos > 0:
        registrar_atividade(
            agente="Permissoes",
            acao="revogar_pasta",
            alvo=bruto,
            status="sucesso",
            detalhes="Permissao de pasta revogada pelo usuario.",
            db_path=db_path,
        )
        return True
    return False
