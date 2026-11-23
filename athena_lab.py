"""Athena Lab — Pipeline multi-agente de R&D e auto-evolucao controlada.

Fluxo seguro:
1. Agente Scout (arquitetura + erros Logfire + busca DuckDuckGo) -> propoe 1 melhoria objetiva.
2. Agente Arquiteto -> elabora plano tecnico detalhado e alteracoes cirurgicas.
3. Agente Critico (Revisor Adversarial) -> avalia risco, CI Linux, seguranca e custo/beneficio.
   So avanca se `aprovado=True` e `nota >= 7`.
4. Validacao em `git worktree` isolado (`sandbox_files/worktrees/<id>`, branch `lab/<id>`):
   bloqueia arquivos sensiveis, aplica patch e executa `ruff check .`, `pyright` e `pytest -v`
   (no maximo 1 retry de correcao).
5. Somente propostas 100% verdes nos 3 checks sao salvas como `pendente` em `lab_propostas`
   para revisao e aprovacao humana explicita na UI (NUNCA faz `git push` automatico).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.capabilities import WebSearch

from agentes import MODELO, _criar_retry_503
from eficiencia import compactar_saida_tool
from ferramentas_logfire import buscar_erros_logfire, consultar_erros_logfire_estruturados
from logging_config import obter_logger
from memoria import obter_db_path

_log = obter_logger("athena_lab")

BASE_DIR = Path(__file__).resolve().parent
WORKTREES_DIR = BASE_DIR / "sandbox_files" / "worktrees"
MAX_RETRIES_CORRECAO = 1
TIMEOUT_CHECK_SEGUNDOS = 180
NOTA_MINIMA_APROVACAO = 7

ARQUIVOS_SENSIVEIS_EXATOS: frozenset[str] = frozenset(
    {
        ".env",
        ".gitignore",
        ".gitleaks.toml",
        "credentials.json",
        "token.json",
        "ulife_cookies.json",
        "apis_ulife_descobertas.json",
    }
)

SEGMENTOS_BLOQUEADOS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "qdrant_db",
        ".logfire",
        "sandbox_files",
    }
)

ESTADO_LAB: dict[str, Any] = {
    "ativo": False,
    "etapa": "ocioso",
    "foco": "",
    "iniciado_em": "",
    "ultima_execucao": "",
    "ultimo_resultado": None,
}
_LAB_LOCK = asyncio.Lock()


# ==============================================================================
# MODELOS ESTRUTURADOS DO DEBATE MULTI-AGENTE
# ==============================================================================


class PropostaScout(BaseModel):
    titulo: str = Field(..., min_length=3, max_length=140)
    categoria: str = Field(default="eficiencia")
    problema: str
    solucao: str
    ganho_estimado: str
    arquivos_alvo: list[str] = Field(default_factory=list)


class AlteracaoArquivo(BaseModel):
    caminho: str
    modo: Literal["substituir", "criar"] = "substituir"
    alvo: str = ""
    novo_conteudo: str = ""


class PlanoArquiteto(BaseModel):
    resumo_tecnico: str
    riscos: list[str] = Field(default_factory=list)
    alteracoes: list[AlteracaoArquivo] = Field(default_factory=list)
    testes_sugeridos: list[str] = Field(default_factory=list)


class VereditoCritico(BaseModel):
    aprovado: bool
    nota: int = Field(..., ge=0, le=10)
    criticas: list[str] = Field(default_factory=list)
    justificativa: str


# ==============================================================================
# 3 AGENTES ESPECIALIZADOS DO ATHENA LAB (PYDANTIC AI)
# ==============================================================================


agente_scout: Agent[None, PropostaScout] = Agent(
    MODELO,
    output_type=PropostaScout,
    capabilities=[
        _criar_retry_503(max_attempts=3, initial_delay=2.0, max_delay=6.0),
        WebSearch(native=False, local="duckduckgo"),
    ],
    instructions=(
        "Voce e o Agente Scout do Athena Lab (em portugues do Brasil). "
        "Analise o resumo da arquitetura da Athena, eventuais erros recentes do Logfire e, se util, "
        "pesquise boas praticas atuais via DuckDuckGo. "
        "Proponha EXATAMENTE 1 melhoria objetiva, cirurgica e testavel offline (foco em economia de tokens, "
        "velocidade, seguranca ou qualidade de codigo). "
        "NUNCA proponha editar `.env`, `.gitignore`, `.gitleaks.toml`, `credentials.json`, `token.json` "
        "nem adicionar dependencias pesadas que quebrem o CI Linux."
    ),
)

agente_arquiteto: Agent[None, PlanoArquiteto] = Agent(
    MODELO,
    output_type=PlanoArquiteto,
    capabilities=[_criar_retry_503(max_attempts=3, initial_delay=2.0, max_delay=6.0)],
    instructions=(
        "Voce e o Agente Arquiteto do Athena Lab (em portugues do Brasil). "
        "A partir da proposta do Scout e dos trechos reais de codigo fornecidos, produza um plano tecnico "
        "conciso e seguro com alteracoes cirurgicas (`modo='substituir'` com `alvo` exato do arquivo atual, "
        "ou `modo='criar'` para novo modulo/teste). "
        "Mantenha compatibilidade total com Python 3.12, Ruff, Pyright (Windows e Linux) e Pytest offline. "
        "NUNCA altere arquivos sensiveis (`.env`, `.gitignore`, `.gitleaks.toml`, `credentials.json`, `token.json`)."
    ),
)

agente_critico: Agent[None, VereditoCritico] = Agent(
    MODELO,
    output_type=VereditoCritico,
    capabilities=[_criar_retry_503(max_attempts=3, initial_delay=2.0, max_delay=6.0)],
    instructions=(
        "Voce e o Agente Critico (Revisor Adversarial) do Athena Lab (em portugues do Brasil). "
        "Avalie rigorosamente a proposta do Scout e o plano do Arquiteto considerando: "
        "1) custo/beneficio real; 2) riscos de seguranca ou regressao; 3) compatibilidade com CI Linux "
        "(sem APIs exclusivas de Windows sem guard, sem Playwright/rede em testes); 4) simplicidade. "
        "Atribua `nota` de 0 a 10 e `aprovado=True` SOMENTE se a proposta for segura, clara e valer a pena (nota >= 7)."
    ),
)


# ==============================================================================
# SEGURANCA DE ARQUIVOS E PERSISTENCIA SQLITE (`lab_propostas`)
# ==============================================================================


def eh_arquivo_sensivel(caminho: str) -> bool:
    """Bloqueia arquivos sensiveis, bancos locais, segredos e qualquer tentativa de path traversal."""
    bruto = (caminho or "").strip().replace("\\", "/")
    if not bruto or "\x00" in bruto:
        return True
    if bruto.startswith("/") or re.match(r"^[a-zA-Z]:", bruto):
        return True

    partes = [p for p in bruto.split("/") if p]
    if not partes or any(p in (".", "..") for p in partes):
        return True

    nome = partes[-1].lower()
    if nome in ARQUIVOS_SENSIVEIS_EXATOS or nome.startswith(".env.") or nome.startswith("memoria.db"):
        return True

    return any(seg.lower() in SEGMENTOS_BLOQUEADOS for seg in partes)


def validar_caminhos_alteracoes(alteracoes: list[dict[str, Any]]) -> None:
    """Valida todas as alteracoes antes de abrir worktree ou tocar no disco."""
    if not alteracoes:
        raise ValueError("Nenhuma alteracao de arquivo fornecida no plano.")
    for item in alteracoes:
        cam = str(item.get("caminho") or "").strip()
        if eh_arquivo_sensivel(cam):
            raise ValueError(
                f"Edicao bloqueada por seguranca: arquivo sensivel ou caminho invalido ('{cam}')."
            )


@contextmanager
def _conn_lab(db_path: str | Path | None = None):
    alvo = obter_db_path(db_path)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(alvo, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute(
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
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _row_para_proposta(row: tuple) -> dict[str, Any]:
    try:
        debate = json.loads(row[5] or "{}")
    except Exception:
        debate = {}
    try:
        checks = json.loads(row[8] or "{}")
    except Exception:
        checks = {}
    return {
        "id": row[0],
        "titulo": row[1],
        "categoria": row[2],
        "resumo": row[3],
        "ganho_estimado": row[4],
        "debate": debate,
        "branch": row[6],
        "diff_texto": row[7],
        "checks": checks,
        "status": row[9],
        "criado_em": row[10],
    }


def salvar_proposta_lab(
    *,
    proposta_id: str,
    titulo: str,
    categoria: str,
    resumo: str,
    ganho_estimado: str,
    debate: dict[str, Any],
    branch: str,
    diff_texto: str,
    checks: dict[str, Any],
    status: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn_lab(db_path) as c:
        c.execute(
            """
            INSERT OR REPLACE INTO lab_propostas
                (id, titulo, categoria, resumo, ganho_estimado, debate_json, branch, diff_texto, checks_json, status, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposta_id,
                titulo[:200],
                categoria[:60],
                resumo[:2000],
                ganho_estimado[:300],
                json.dumps(debate, ensure_ascii=False),
                branch[:120],
                diff_texto[:50000],
                json.dumps(checks, ensure_ascii=False),
                status,
                agora,
            ),
        )
        row = c.execute(
            """
            SELECT id, titulo, categoria, resumo, ganho_estimado, debate_json, branch, diff_texto, checks_json, status, criado_em
            FROM lab_propostas WHERE id = ?
            """,
            (proposta_id,),
        ).fetchone()
    return _row_para_proposta(row)


def listar_propostas_lab(
    status: str | None = None,
    *,
    limite: int = 30,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limite), 100))
    with _conn_lab(db_path) as c:
        if status:
            rows = c.execute(
                """
                SELECT id, titulo, categoria, resumo, ganho_estimado, debate_json, branch, diff_texto, checks_json, status, criado_em
                FROM lab_propostas WHERE status = ? ORDER BY criado_em DESC LIMIT ?
                """,
                (status, lim),
            ).fetchall()
        else:
            rows = c.execute(
                """
                SELECT id, titulo, categoria, resumo, ganho_estimado, debate_json, branch, diff_texto, checks_json, status, criado_em
                FROM lab_propostas ORDER BY criado_em DESC LIMIT ?
                """,
                (lim,),
            ).fetchall()
    return [_row_para_proposta(r) for r in rows]


def obter_proposta_lab(proposta_id: str, *, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with _conn_lab(db_path) as c:
        row = c.execute(
            """
            SELECT id, titulo, categoria, resumo, ganho_estimado, debate_json, branch, diff_texto, checks_json, status, criado_em
            FROM lab_propostas WHERE id = ?
            """,
            (proposta_id,),
        ).fetchone()
    return _row_para_proposta(row) if row else None


def _atualizar_status_proposta(proposta_id: str, status: str, *, db_path: str | Path | None = None) -> None:
    with _conn_lab(db_path) as c:
        c.execute("UPDATE lab_propostas SET status = ? WHERE id = ?", (status, proposta_id))


# ==============================================================================
# VALIDACAO EM GIT WORKTREE ISOLADO (`ruff`, `pyright`, `pytest`)
# ==============================================================================


def _rodar_cmd(cmd: list[str], *, cwd: Path, timeout: int = TIMEOUT_CHECK_SEGUNDOS) -> tuple[int, str]:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
        saida = "\n".join(p for p in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if p)
        return int(proc.returncode), compactar_saida_tool(saida, max_chars=2400)
    except subprocess.TimeoutExpired:
        return 124, f"Tempo limite excedido ({timeout}s) ao executar {' '.join(cmd)}"
    except Exception as exc:
        return 1, f"Erro ao executar {' '.join(cmd)}: {exc}"


def _resolver_ferramenta_venv(nome_bin: str, repo_dir: Path) -> list[str]:
    if sys.platform == "win32":
        cand = repo_dir / ".venv" / "Scripts" / f"{nome_bin}.exe"
    else:
        cand = repo_dir / ".venv" / "bin" / nome_bin
    if cand.is_file():
        return [str(cand)]
    achado = shutil.which(nome_bin)
    if achado:
        return [achado]
    return [sys.executable, "-m", nome_bin]


def _limpar_worktree_e_branch(
    repo_dir: Path,
    worktree_dir: Path | None,
    branch_nome: str,
    *,
    remover_branch: bool = True,
) -> None:
    if worktree_dir is not None and worktree_dir.exists():
        _rodar_cmd(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=repo_dir, timeout=60)
        if worktree_dir.exists():
            with suppress(OSError):
                shutil.rmtree(worktree_dir, ignore_errors=True)
    _rodar_cmd(["git", "worktree", "prune"], cwd=repo_dir, timeout=30)
    if remover_branch and branch_nome:
        _rodar_cmd(["git", "branch", "-D", branch_nome], cwd=repo_dir, timeout=30)


def _aplicar_alteracoes_em_diretorio(destino_dir: Path, alteracoes: list[dict[str, Any]]) -> None:
    validar_caminhos_alteracoes(alteracoes)
    base_res = destino_dir.resolve()

    for item in alteracoes:
        cam_rel = str(item.get("caminho") or "").strip().replace("\\", "/")
        modo = str(item.get("modo") or "substituir").strip().lower()
        alvo_txt = str(item.get("alvo") or "")
        novo_txt = str(item.get("novo_conteudo") or "")

        arq = (base_res / cam_rel).resolve()
        if base_res not in arq.parents:
            raise ValueError(f"Caminho fora do worktree bloqueado: '{cam_rel}'.")

        if modo == "criar":
            arq.parent.mkdir(parents=True, exist_ok=True)
            arq.write_text(novo_txt, encoding="utf-8")
        else:
            if not arq.is_file():
                raise ValueError(f"Arquivo alvo nao existe para substituicao: '{cam_rel}'.")
            atual = arq.read_text(encoding="utf-8")
            if not alvo_txt or alvo_txt not in atual:
                raise ValueError(f"Trecho `alvo` nao encontrado exatamente em '{cam_rel}'.")
            arq.write_text(atual.replace(alvo_txt, novo_txt, 1), encoding="utf-8")


def validar_em_worktree(
    branch_nome: str,
    alteracoes: list[dict[str, Any]],
    *,
    repo_dir: Path | None = None,
    worktree_base: Path | None = None,
    timeout_check: int = TIMEOUT_CHECK_SEGUNDOS,
) -> dict[str, Any]:
    """Valida alteracoes em um `git worktree` isolado rodando `ruff`, `pyright` e `pytest`.

    - Bloqueia imediatamente arquivos sensiveis (`.env`, `.gitignore`, `.gitleaks.toml`, `credentials.json`, `token.json`).
    - Nunca altera a working tree principal.
    - Remove o worktree e a branch se qualquer check falhar.
    """
    validar_caminhos_alteracoes(alteracoes)

    repo = (repo_dir or BASE_DIR).resolve()
    branch = branch_nome.strip()
    if not branch.startswith("lab/"):
        branch = f"lab/{branch}"
    slug_wt = re.sub(r"[^a-zA-Z0-9_-]+", "_", branch.split("/", 1)[-1]).strip("_") or uuid.uuid4().hex[:8]
    base_wt = (worktree_base or (repo / "sandbox_files" / "worktrees")).resolve()
    base_wt.mkdir(parents=True, exist_ok=True)
    wt_dir = base_wt / slug_wt

    _limpar_worktree_e_branch(repo, wt_dir, branch, remover_branch=True)

    rc_wt, out_wt = _rodar_cmd(["git", "worktree", "add", "-b", branch, str(wt_dir), "HEAD"], cwd=repo, timeout=60)
    if rc_wt != 0:
        return {
            "ok": False,
            "erro": f"Falha ao criar git worktree isolado: {out_wt}",
            "branch": branch,
            "worktree": str(wt_dir),
            "diff": "",
            "checks": {},
        }

    checks: dict[str, dict[str, Any]] = {}
    diff_txt = ""
    try:
        _aplicar_alteracoes_em_diretorio(wt_dir, alteracoes)

        # Captura diff completo (incluindo arquivos novos marcados com git add -N)
        _rodar_cmd(["git", "add", "-N", "."], cwd=wt_dir, timeout=30)
        _rc_diff, diff_txt = _rodar_cmd(["git", "diff", "HEAD"], cwd=wt_dir, timeout=30)
        if not diff_txt.strip():
            raise ValueError("As alteracoes nao produziram nenhuma mudanca real no git diff.")

        # 1) Ruff
        cmd_ruff = [*_resolver_ferramenta_venv("ruff", repo), "check", "."]
        rc_ruff, out_ruff = _rodar_cmd(cmd_ruff, cwd=wt_dir, timeout=timeout_check)
        checks["ruff"] = {"ok": rc_ruff == 0, "saida": out_ruff}

        # 2) Pyright
        cmd_pyright = _resolver_ferramenta_venv("pyright", repo)
        rc_pyr, out_pyr = _rodar_cmd(cmd_pyright, cwd=wt_dir, timeout=timeout_check)
        checks["pyright"] = {"ok": rc_pyr == 0, "saida": out_pyr}

        # 3) Pytest (com basetemp local no worktree para evitar erro de symlink no Windows)
        pytest_tmp = wt_dir / ".pytest_lab_tmp"
        cmd_pytest = [*_resolver_ferramenta_venv("pytest", repo), "-v", f"--basetemp={pytest_tmp}"]
        rc_pyt, out_pyt = _rodar_cmd(cmd_pytest, cwd=wt_dir, timeout=timeout_check)
        with suppress(OSError):
            shutil.rmtree(pytest_tmp, ignore_errors=True)
        checks["pytest"] = {"ok": rc_pyt == 0, "saida": out_pyt}

        todos_ok = all(bool(checks[k]["ok"]) for k in ("ruff", "pyright", "pytest"))
        if not todos_ok:
            _limpar_worktree_e_branch(repo, wt_dir, branch, remover_branch=True)
            falhas = [k for k in ("ruff", "pyright", "pytest") if not checks[k]["ok"]]
            return {
                "ok": False,
                "erro": f"Checks falharam no worktree isolado: {', '.join(falhas)}",
                "branch": branch,
                "worktree": str(wt_dir),
                "diff": diff_txt,
                "checks": checks,
            }

        # Commit isolado na branch lab/<id> para permitir merge limpo na aprovacao humana
        _rodar_cmd(["git", "add", "."], cwd=wt_dir, timeout=30)
        rc_cm, out_cm = _rodar_cmd(
            ["git", "commit", "-m", f"Athena Lab ({branch}): melhoria validada em CI local"],
            cwd=wt_dir,
            timeout=30,
        )
        if rc_cm != 0:
            _limpar_worktree_e_branch(repo, wt_dir, branch, remover_branch=True)
            return {
                "ok": False,
                "erro": f"Falha ao registrar commit na branch isolada {branch}: {out_cm}",
                "branch": branch,
                "worktree": str(wt_dir),
                "diff": diff_txt,
                "checks": checks,
            }

        return {
            "ok": True,
            "branch": branch,
            "worktree": str(wt_dir),
            "diff": diff_txt,
            "checks": checks,
        }
    except Exception as exc:
        _limpar_worktree_e_branch(repo, wt_dir, branch, remover_branch=True)
        return {
            "ok": False,
            "erro": str(exc),
            "branch": branch,
            "worktree": str(wt_dir),
            "diff": diff_txt,
            "checks": checks,
        }


# ==============================================================================
# APROVACAO E REJEICAO HUMANA (NUNCA FAZ GIT PUSH)
# ==============================================================================


def aprovar_proposta_lab(
    proposta_id: str,
    *,
    repo_dir: Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Aplica via merge limpo a branch validada `lab/<id>` apos confirmar working tree limpa (sem git push)."""
    prop = obter_proposta_lab(proposta_id, db_path=db_path)
    if prop is None:
        return {"ok": False, "erro": f"Proposta '{proposta_id}' nao encontrada."}
    if prop["status"] != "pendente":
        return {"ok": False, "erro": f"Proposta '{proposta_id}' nao esta pendente (status: {prop['status']})."}

    repo = (repo_dir or BASE_DIR).resolve()
    branch = str(prop.get("branch") or "").strip()
    if not branch:
        return {"ok": False, "erro": "Proposta sem branch associada para merge."}

    # Confirma working tree limpa antes de aplicar merge
    rc_st, out_st = _rodar_cmd(["git", "status", "--porcelain"], cwd=repo, timeout=30)
    if rc_st != 0:
        return {"ok": False, "erro": f"Nao foi possivel verificar git status: {out_st}"}
    if out_st.strip():
        return {
            "ok": False,
            "erro": "Working tree principal possui alteracoes nao commitadas. Faca commit/stash antes de aprovar a proposta.",
        }

    # Remove o worktree temporario mantendo a branch para o merge
    slug_wt = re.sub(r"[^a-zA-Z0-9_-]+", "_", branch.split("/", 1)[-1]).strip("_")
    wt_dir = repo / "sandbox_files" / "worktrees" / slug_wt
    _limpar_worktree_e_branch(repo, wt_dir, branch, remover_branch=False)

    rc_mg, out_mg = _rodar_cmd(
        ["git", "merge", "--no-ff", branch, "-m", f"Athena Lab: aplicar proposta {proposta_id} ({prop['titulo']})"],
        cwd=repo,
        timeout=60,
    )
    if rc_mg != 0:
        _rodar_cmd(["git", "merge", "--abort"], cwd=repo, timeout=30)
        return {"ok": False, "erro": f"Falha ao fazer merge da branch '{branch}': {out_mg}"}

    _rodar_cmd(["git", "branch", "-d", branch], cwd=repo, timeout=30)
    _atualizar_status_proposta(proposta_id, "aprovada", db_path=db_path)
    _log.info("athena_lab_proposta_aprovada", proposta_id=proposta_id, branch=branch)
    return {
        "ok": True,
        "mensagem": f"Proposta '{prop['titulo']}' aplicada localmente via merge (sem git push).",
        "proposta": obter_proposta_lab(proposta_id, db_path=db_path),
    }


def rejeitar_proposta_lab(
    proposta_id: str,
    *,
    repo_dir: Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Descarta a branch e o worktree temporario de uma proposta e marca como `rejeitada`."""
    prop = obter_proposta_lab(proposta_id, db_path=db_path)
    if prop is None:
        return {"ok": False, "erro": f"Proposta '{proposta_id}' nao encontrada."}

    repo = (repo_dir or BASE_DIR).resolve()
    branch = str(prop.get("branch") or "").strip()
    if branch:
        slug_wt = re.sub(r"[^a-zA-Z0-9_-]+", "_", branch.split("/", 1)[-1]).strip("_")
        wt_dir = repo / "sandbox_files" / "worktrees" / slug_wt
        _limpar_worktree_e_branch(repo, wt_dir, branch, remover_branch=True)

    _atualizar_status_proposta(proposta_id, "rejeitada", db_path=db_path)
    _log.info("athena_lab_proposta_rejeitada", proposta_id=proposta_id, branch=branch)
    return {
        "ok": True,
        "mensagem": f"Proposta '{prop['titulo']}' rejeitada e branch temporaria removida.",
        "proposta": obter_proposta_lab(proposta_id, db_path=db_path),
    }


# ==============================================================================
# ORQUESTRACAO DO CICLO R&D (`rodar_ciclo_rd`)
# ==============================================================================


def _coletar_contexto_arquitetura(repo_dir: Path) -> str:
    partes: list[str] = []
    arqs_py = sorted(p.name for p in repo_dir.glob("*.py"))
    partes.append("Modulos Python na raiz: " + ", ".join(arqs_py))

    readme = repo_dir / "README.md"
    if readme.is_file():
        partes.append("=== README (resumo) ===\n" + readme.read_text(encoding="utf-8", errors="ignore")[:3200])

    if (os.getenv("LOGFIRE_READ_TOKEN") or "").strip():
        with suppress(Exception):
            erros_lf = buscar_erros_logfire(minutos=1440, limite=5)
            partes.append("=== LOGFIRE (ultimas 24h) ===\n" + erros_lf)

    return compactar_saida_tool("\n\n".join(partes), max_chars=4500)


def _ler_trechos_arquivos_alvo(repo_dir: Path, arquivos_alvo: list[str]) -> str:
    blocos: list[str] = []
    for cam in (arquivos_alvo or [])[:4]:
        if eh_arquivo_sensivel(cam):
            continue
        arq = (repo_dir / cam).resolve()
        if repo_dir.resolve() in arq.parents and arq.is_file():
            conteudo = arq.read_text(encoding="utf-8", errors="ignore")[:5000]
            blocos.append(f"=== ARQUIVO: {cam} ===\n{conteudo}")
    return compactar_saida_tool("\n\n".join(blocos), max_chars=9000)


def obter_status_lab() -> dict[str, Any]:
    return dict(ESTADO_LAB)


async def rodar_ciclo_rd(
    foco: str = "eficiencia e tokens",
    *,
    categoria: str | None = None,
    db_path: str | Path | None = None,
    repo_dir: Path | None = None,
) -> dict[str, Any]:
    """Executa um ciclo completo do Athena Lab (Scout -> Arquiteto -> Critico -> git worktree CI)."""
    if _LAB_LOCK.locked():
        return {"ok": False, "erro": "Ja existe um ciclo do Athena Lab em andamento."}

    async with _LAB_LOCK:
        repo = (repo_dir or BASE_DIR).resolve()
        proposta_id = uuid.uuid4().hex[:10]
        branch = f"lab/{proposta_id}"
        foco_limpo = (foco or "eficiencia e tokens").strip()[:160]
        t0 = time.perf_counter()

        ESTADO_LAB.update(
            {
                "ativo": True,
                "etapa": "Coletando contexto e acionando Agente Scout...",
                "foco": foco_limpo,
                "iniciado_em": datetime.now().isoformat(timespec="seconds"),
            }
        )
        _log.info("athena_lab_ciclo_iniciado", proposta_id=proposta_id, foco=foco_limpo)

        try:
            ctx_arq = await asyncio.to_thread(_coletar_contexto_arquitetura, repo)

            # 1) Scout
            res_scout = await agente_scout.run(
                f"Foco deste ciclo R&D: {foco_limpo}\n\n"
                f"Contexto atual da Athena:\n{ctx_arq}\n\n"
                "Proponha 1 melhoria concreta, segura e mensuravel."
            )
            proposta: PropostaScout = res_scout.output

            # 2) Arquiteto
            ESTADO_LAB["etapa"] = f"Agente Arquiteto planejando: {proposta.titulo}"
            trechos_codigo = await asyncio.to_thread(_ler_trechos_arquivos_alvo, repo, proposta.arquivos_alvo)
            res_arq = await agente_arquiteto.run(
                f"Proposta do Scout:\n{proposta.model_dump_json(indent=2)}\n\n"
                f"Trechos reais dos arquivos alvo:\n{trechos_codigo or '(nenhum arquivo alvo lido)'}\n\n"
                "Produza um plano tecnico seguro e cirurgico."
            )
            plano: PlanoArquiteto = res_arq.output

            # 3) Critico Adversarial
            ESTADO_LAB["etapa"] = "Agente Critico (Revisor Adversarial) avaliando proposta..."
            res_crit = await agente_critico.run(
                f"Proposta do Scout:\n{proposta.model_dump_json(indent=2)}\n\n"
                f"Plano do Arquiteto:\n{plano.model_dump_json(indent=2)}\n\n"
                "Avalie rigorosamente custo/beneficio, seguranca, compatibilidade com CI Linux e simplicidade."
            )
            veredito: VereditoCritico = res_crit.output

            debate = {
                "foco": foco_limpo,
                "scout": proposta.model_dump(),
                "arquiteto": plano.model_dump(),
                "critico": veredito.model_dump(),
            }

            categoria_final = (categoria or proposta.categoria or "eficiencia").strip()[:60]

            if (not veredito.aprovado) or int(veredito.nota) < NOTA_MINIMA_APROVACAO:
                salva = salvar_proposta_lab(
                    proposta_id=proposta_id,
                    titulo=proposta.titulo,
                    categoria=categoria_final,
                    resumo=f"{proposta.problema} -> {proposta.solucao}",
                    ganho_estimado=proposta.ganho_estimado,
                    debate=debate,
                    branch="",
                    diff_texto="",
                    checks={"critico": {"ok": False, "nota": veredito.nota, "justificativa": veredito.justificativa}},
                    status="descartada",
                    db_path=db_path,
                )
                dur_ms = max(1, int((time.perf_counter() - t0) * 1000))
                _log.info(
                    "athena_lab_descartada_critico",
                    proposta_id=proposta_id,
                    nota=veredito.nota,
                    aprovado=veredito.aprovado,
                    latencia_ms=dur_ms,
                )
                res_final = {
                    "ok": True,
                    "status": "descartada",
                    "motivo": f"Reprovada pelo Agente Critico (nota {veredito.nota}/10): {veredito.justificativa}",
                    "proposta": salva,
                }
                ESTADO_LAB["ultimo_resultado"] = res_final
                return res_final

            # 4) Validacao isolada em git worktree (com no maximo 1 retry de correcao)
            alteracoes_dicts = [alt.model_dump() for alt in plano.alteracoes]
            validacao: dict[str, Any] = {"ok": False, "erro": "Nao executada", "diff": "", "checks": {}}

            for tentativa in range(MAX_RETRIES_CORRECAO + 1):
                ESTADO_LAB["etapa"] = (
                    f"Validando em git worktree isolado (tentativa {tentativa + 1}/{MAX_RETRIES_CORRECAO + 1})..."
                )
                try:
                    validacao = await asyncio.to_thread(
                        validar_em_worktree,
                        branch,
                        alteracoes_dicts,
                        repo_dir=repo,
                    )
                except ValueError as exc_seg:
                    validacao = {"ok": False, "erro": str(exc_seg), "diff": "", "checks": {}}
                    break

                if validacao.get("ok"):
                    break

                if tentativa < MAX_RETRIES_CORRECAO:
                    ESTADO_LAB["etapa"] = "Corrigindo falhas de CI com Agente Arquiteto (retry unico)..."
                    res_retry = await agente_arquiteto.run(
                        f"As alteracoes anteriores falharam nos checks do worktree:\n"
                        f"Erro: {validacao.get('erro')}\n"
                        f"Checks: {json.dumps(validacao.get('checks') or {}, ensure_ascii=False)[:2200]}\n\n"
                        f"Plano anterior:\n{plano.model_dump_json(indent=2)}\n\n"
                        "Corrija o plano para passar em ruff, pyright e pytest."
                    )
                    plano = res_retry.output
                    debate["arquiteto_retry"] = plano.model_dump()
                    alteracoes_dicts = [alt.model_dump() for alt in plano.alteracoes]

            status_final = "pendente" if validacao.get("ok") else "descartada"
            salva = salvar_proposta_lab(
                proposta_id=proposta_id,
                titulo=proposta.titulo,
                categoria=categoria_final,
                resumo=plano.resumo_tecnico or f"{proposta.problema} -> {proposta.solucao}",
                ganho_estimado=proposta.ganho_estimado,
                debate=debate,
                branch=branch if status_final == "pendente" else "",
                diff_texto=str(validacao.get("diff") or ""),
                checks=dict(validacao.get("checks") or {}),
                status=status_final,
                db_path=db_path,
            )
            dur_ms = max(1, int((time.perf_counter() - t0) * 1000))
            _log.info(
                "athena_lab_ciclo_concluido",
                proposta_id=proposta_id,
                status=status_final,
                nota_critico=veredito.nota,
                latencia_ms=dur_ms,
            )
            res_final = {
                "ok": True,
                "status": status_final,
                "proposta": salva,
                "erro_validacao": "" if status_final == "pendente" else str(validacao.get("erro") or ""),
            }
            ESTADO_LAB["ultimo_resultado"] = res_final
            return res_final
        except Exception as exc:
            _log.error("athena_lab_ciclo_erro", proposta_id=proposta_id, erro=str(exc))
            res_err = {"ok": False, "erro": f"{type(exc).__name__}: {exc}"}
            ESTADO_LAB["ultimo_resultado"] = res_err
            return res_err
        finally:
            ESTADO_LAB["ativo"] = False
            ESTADO_LAB["etapa"] = "ocioso"
            ESTADO_LAB["ultima_execucao"] = datetime.now().isoformat(timespec="seconds")


# ==============================================================================
# LOGFIRE SENTINEL (VIGIA AUTO-HEALING CONECTADO AO ATHENA LAB)
# ==============================================================================

_RE_ERRO_TRANSITORIO = re.compile(
    r"(?:\b503\b|\b429\b|service\s+unavailable|\bunavailable\b|overloaded|"
    r"resource_exhausted|rate\s*limit|too\s+many\s+requests|quota|"
    r"timeouterror|readtimeout|connecttimeout|connecterror|timed\s+out|"
    r"modelunavailableerror|modeloverloadederror|modelratelimiterror|"
    r"modelquotaexceedederror|connection\s+refused|remotedisconnected)",
    re.IGNORECASE,
)

_RE_BUG_CODIGO_CORRIGIVEL = re.compile(
    r"(?:\btypeerror\b|\battributeerror\b|\bkeyerror\b|\bvalueerror\b|\bnameerror\b|"
    r"\bimporterror\b|\bmodulenotfounderror\b|\busererror\b|\boperationalerror\b|"
    r"\bzerodivisionerror\b|\bindexerror\b|\bunboundlocalerror\b|\bsyntaxerror\b|"
    r"\bhttp\s*500\b|\bstatus[_\s:=]+500\b|internal\s+server\s+error)",
    re.IGNORECASE,
)

_SENTINEL_EXECUCOES_TS: list[float] = []
_SENTINEL_VISTOS_RAM: set[str] = set()
_SENTINEL_TASKS_BG: set[asyncio.Task[Any]] = set()


def sentinel_ativo() -> bool:
    return (os.getenv("ATHENA_SENTINEL_ENABLED", "true") or "true").strip().lower() in (
        "1",
        "true",
        "sim",
        "yes",
        "on",
    )


def sentinel_intervalo_minutos() -> int:
    bruto = (os.getenv("ATHENA_SENTINEL_INTERVAL_MIN", "10") or "10").strip()
    try:
        return max(1, min(int(bruto), 1440))
    except ValueError:
        return 10


def sentinel_max_por_hora() -> int:
    bruto = (os.getenv("ATHENA_SENTINEL_MAX_POR_HORA", "2") or "2").strip()
    try:
        return max(1, min(int(bruto), 20))
    except ValueError:
        return 2


def limpar_estado_sentinel() -> None:
    """Limpa contadores e cache em RAM do Sentinel (util em testes deterministas)."""
    _SENTINEL_EXECUCOES_TS.clear()
    _SENTINEL_VISTOS_RAM.clear()


def eh_bug_de_codigo_corrigivel(erro: dict[str, Any]) -> bool:
    """Filtra ruido transitorio externo (503/429/timeouts) e aceita bugs reais de codigo da Athena."""
    if not isinstance(erro, dict):
        return False

    exc_type = str(erro.get("exception_type") or "").strip()
    exc_msg = str(erro.get("exception_message") or "").strip()
    msg = str(erro.get("message") or "").strip()
    span = str(erro.get("span_name") or "").strip()
    tb = str(erro.get("exception_stacktrace") or "").strip()
    status_http = str(erro.get("http_status_code") or erro.get("status_code") or "").strip()

    texto_combinado = " | ".join(p for p in (exc_type, exc_msg, msg, span, status_http, tb[-500:]) if p)
    if not texto_combinado:
        return False

    if _RE_ERRO_TRANSITORIO.search(texto_combinado):
        return False

    if status_http == "500":
        return True

    return bool(_RE_BUG_CODIGO_CORRIGIVEL.search(texto_combinado))


def gerar_fingerprint_erro(erro: dict[str, Any]) -> str:
    """Gera hash SHA-256 curto (16 hex) normalizado por tipo + mensagem + arquivo para deduplicacao."""
    exc_type = str(erro.get("exception_type") or "Error").strip().lower()
    msg_base = str(erro.get("exception_message") or erro.get("message") or erro.get("span_name") or "").strip().lower()
    # Normaliza tokens volateis (enderecos 0x..., UUIDs, timestamps, numeros soltos)
    msg_norm = re.sub(r"0x[0-9a-f]+", "0xADDR", msg_base)
    msg_norm = re.sub(r"[0-9a-f]{8,}", "ID", msg_norm)
    msg_norm = re.sub(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?\b", "TS", msg_norm)
    msg_norm = re.sub(r"\b\d+\b", "N", msg_norm)
    msg_norm = re.sub(r"\s+", " ", msg_norm).strip()[:220]

    tb = str(erro.get("exception_stacktrace") or "")
    arquivos = re.findall(r'([A-Za-z0-9_.-]+\.py)', tb)
    arq_principal = arquivos[-1].lower() if arquivos else ""

    chave = f"{exc_type}|{arq_principal}|{msg_norm}"
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()[:16]


def _garantir_tabela_sentinel(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS erros_sentinel_vistos (
            fingerprint TEXT PRIMARY KEY,
            exception_type TEXT NOT NULL DEFAULT '',
            resumo TEXT NOT NULL DEFAULT '',
            visto_em TEXT NOT NULL,
            acionou_lab INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _erro_sentinel_ja_visto(fingerprint: str, *, db_path: str | Path | None = None) -> bool:
    if fingerprint in _SENTINEL_VISTOS_RAM:
        return True
    with _conn_lab(db_path) as c:
        _garantir_tabela_sentinel(c)
        row = c.execute(
            "SELECT 1 FROM erros_sentinel_vistos WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
    if row is not None:
        _SENTINEL_VISTOS_RAM.add(fingerprint)
        return True
    return False


def _registrar_erro_sentinel_visto(
    fingerprint: str,
    exc_type: str,
    resumo: str,
    *,
    acionou_lab: bool = False,
    db_path: str | Path | None = None,
) -> None:
    _SENTINEL_VISTOS_RAM.add(fingerprint)
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn_lab(db_path) as c:
        _garantir_tabela_sentinel(c)
        c.execute(
            """
            INSERT OR REPLACE INTO erros_sentinel_vistos
                (fingerprint, exception_type, resumo, visto_em, acionou_lab)
            VALUES (?, ?, ?, ?, ?)
            """,
            (fingerprint, exc_type[:100], resumo[:400], agora, 1 if acionou_lab else 0),
        )


def _resumir_erro_logfire(erro: dict[str, Any]) -> str:
    exc_type = str(erro.get("exception_type") or "").strip()
    exc_msg = str(erro.get("exception_message") or erro.get("message") or "").strip()
    span = str(erro.get("span_name") or "").strip()
    tb = str(erro.get("exception_stacktrace") or "").strip()
    ultima_tb = ""
    if tb:
        linhas_tb = [ln.strip() for ln in tb.splitlines() if ln.strip()]
        ultima_tb = " | ".join(linhas_tb[-2:])[:180]
    partes = [p for p in (f"{exc_type}: {exc_msg}".strip(": "), f"span={span}" if span else "", ultima_tb) if p]
    return " · ".join(partes)[:320]


async def verificar_erros_logfire_sentinel(
    *,
    db_path: str | Path | None = None,
    repo_dir: Path | None = None,
    forcar: bool = False,
    background_lab: bool = False,
) -> dict[str, Any]:
    """Vigia periodico que consulta o Logfire (0 tokens) e aciona o Athena Lab para bugs reais ineditos."""
    ativo = sentinel_ativo()
    if not forcar and not ativo:
        return {
            "ok": True,
            "ativo": False,
            "acionou_lab": False,
            "erros_analisados": 0,
            "motivo": "Logfire Sentinel desativado (ATHENA_SENTINEL_ENABLED=false).",
        }

    if not (os.getenv("LOGFIRE_READ_TOKEN") or "").strip():
        return {
            "ok": True,
            "ativo": ativo,
            "acionou_lab": False,
            "erros_analisados": 0,
            "motivo": "LOGFIRE_READ_TOKEN nao configurado (verificacao ignorada silenciosamente).",
        }

    agora_ts = time.time()
    _SENTINEL_EXECUCOES_TS[:] = [ts for ts in _SENTINEL_EXECUCOES_TS if (agora_ts - ts) < 3600.0]
    max_hora = sentinel_max_por_hora()
    if len(_SENTINEL_EXECUCOES_TS) >= max_hora:
        _log.info("sentinel_limite_hora_atingido", executados_ultima_hora=len(_SENTINEL_EXECUCOES_TS), limite=max_hora)
        return {
            "ok": True,
            "ativo": ativo,
            "acionou_lab": False,
            "limite_hora_atingido": True,
            "erros_analisados": 0,
            "motivo": f"Limite de {max_hora} ciclo(s) auto-fix por hora atingido.",
        }

    if _LAB_LOCK.locked():
        return {
            "ok": True,
            "ativo": ativo,
            "acionou_lab": False,
            "erros_analisados": 0,
            "motivo": "Athena Lab ja esta executando outro ciclo no momento.",
        }

    janela_min = sentinel_intervalo_minutos() + 5
    erros = await asyncio.to_thread(consultar_erros_logfire_estruturados, minutos=janela_min, limite=15)
    if not erros:
        return {
            "ok": True,
            "ativo": ativo,
            "acionou_lab": False,
            "erros_analisados": 0,
            "motivo": f"Nenhum erro encontrado no Logfire nos ultimos {janela_min} min.",
        }

    bugs_filtrados = 0
    duplicados = 0
    for erro in erros:
        if not eh_bug_de_codigo_corrigivel(erro):
            continue
        bugs_filtrados += 1
        fp = gerar_fingerprint_erro(erro)
        if _erro_sentinel_ja_visto(fp, db_path=db_path):
            duplicados += 1
            continue

        resumo_erro = _resumir_erro_logfire(erro)
        exc_type = str(erro.get("exception_type") or "Error").strip()
        _registrar_erro_sentinel_visto(fp, exc_type, resumo_erro, acionou_lab=True, db_path=db_path)
        _SENTINEL_EXECUCOES_TS.append(time.time())

        foco_fix = f"Corrigir bug capturado no Logfire: {resumo_erro}"
        _log.info(
            "sentinel_bug_detectado",
            fingerprint=fp,
            exception_type=exc_type,
            resumo=resumo_erro,
            background_lab=background_lab,
        )

        if background_lab:
            task = asyncio.create_task(
                rodar_ciclo_rd(foco=foco_fix, categoria="auto-fix-logfire", db_path=db_path, repo_dir=repo_dir)
            )
            _SENTINEL_TASKS_BG.add(task)
            task.add_done_callback(_SENTINEL_TASKS_BG.discard)
            return {
                "ok": True,
                "ativo": ativo,
                "acionou_lab": True,
                "em_segundo_plano": True,
                "fingerprint": fp,
                "resumo_erro": resumo_erro,
                "erros_analisados": len(erros),
                "motivo": f"Bug detectado ({exc_type}) — Athena Lab iniciado em segundo plano.",
            }

        res_lab = await rodar_ciclo_rd(
            foco=foco_fix,
            categoria="auto-fix-logfire",
            db_path=db_path,
            repo_dir=repo_dir,
        )
        return {
            "ok": True,
            "ativo": ativo,
            "acionou_lab": True,
            "fingerprint": fp,
            "resumo_erro": resumo_erro,
            "erros_analisados": len(erros),
            "resultado_lab": res_lab,
        }

    return {
        "ok": True,
        "ativo": ativo,
        "acionou_lab": False,
        "erros_analisados": len(erros),
        "bugs_filtrados": bugs_filtrados,
        "duplicados_ignorados": duplicados,
        "motivo": "Nenhum bug de codigo inedito encontrado no Logfire.",
    }
