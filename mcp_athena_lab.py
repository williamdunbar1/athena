"""Servidor MCP local (somente leitura) do Athena Lab para integracao com o Google Antigravity no VS Code.

Expoe ferramentas de inspecao ao vivo via FastMCP (transporte stdio):
- `listar_propostas_lab`: propostas pendentes/briefings do Athena Lab com seus `briefing_prompt`.
- `ler_ultimos_erros_logfire`: ultimos erros/excecoes capturados no Pydantic Logfire (mascarados).
- `resumo_saude_athena`: contagem de testes, skills, grafo Obsidian, cache semantico e status do CLI.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

import athena_lab as _lab
import ferramentas_logfire as _lf
import grafo_obsidian as _grafo
import semantic_cache as _sc
import skills_manager as _skills
from guardrails import mascarar_segredos_e_pii

BASE_DIR = Path(__file__).resolve().parent

mcp = FastMCP("Athena Lab")


def _contar_testes_offline(tests_dir: Path | None = None) -> dict[str, Any]:
    """Conta funcoes `def test_*` via AST (instantaneo, 0 subprocessos, somente leitura)."""
    pasta = (tests_dir or (BASE_DIR / "tests")).resolve()
    if not pasta.is_dir():
        return {"total_funcoes_teste": 0, "arquivos": []}

    arquivos: list[str] = []
    total = 0
    for arq in sorted(pasta.glob("test_*.py")):
        arquivos.append(arq.name)
        try:
            arvore = ast.parse(arq.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(arvore):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                total += 1
    return {"total_funcoes_teste": total, "arquivos": arquivos}


@mcp.tool()
def listar_propostas_lab(status: str = "pendentes", limite: int = 20) -> dict[str, Any]:
    """Lista propostas do Athena Lab (somente leitura) com briefings cirurgicos prontos para o Antigravity.

    Args:
        status: 'pendentes' (pendente + pronta_antigravity), 'todas', ou um status especifico.
        limite: numero maximo de propostas retornadas (1 a 50).
    """
    lim = max(1, min(int(limite or 20), 50))
    filtro = (status or "pendentes").strip().lower()

    if filtro in ("todas", "all", "*"):
        brutas = _lab.listar_propostas_lab(status=None, limite=lim)
    elif filtro in ("pendentes", "acionaveis"):
        todas = _lab.listar_propostas_lab(status=None, limite=min(100, lim * 3))
        brutas = [p for p in todas if p.get("status") in _lab.STATUS_ACIONAVEIS_LAB][:lim]
    else:
        brutas = _lab.listar_propostas_lab(status=filtro, limite=lim)

    itens: list[dict[str, Any]] = []
    for p in brutas:
        itens.append(
            {
                "id": p.get("id", ""),
                "titulo": p.get("titulo", ""),
                "categoria": p.get("categoria", ""),
                "status": p.get("status", ""),
                "status_label": p.get("status_label", p.get("status", "")),
                "modo_execucao": p.get("modo_execucao", ""),
                "resumo": p.get("resumo", ""),
                "ganho_estimado": p.get("ganho_estimado", ""),
                "branch": p.get("branch", ""),
                "criado_em": p.get("criado_em", ""),
                "checks": p.get("checks") or {},
                "briefing_prompt": p.get("briefing_prompt", ""),
            }
        )

    return {
        "ok": True,
        "filtro": filtro,
        "total": len(itens),
        "cli_antigravity": _lab.obter_status_cli_antigravity(),
        "propostas": itens,
    }


@mcp.tool()
def ler_ultimos_erros_logfire(minutos: int = 60, limite: int = 15) -> dict[str, Any]:
    """Consulta os erros e excecoes recentes da Athena no Pydantic Logfire (somente leitura, segredos mascarados)."""
    mins = max(1, min(int(minutos or 60), 10080))
    lim = max(1, min(int(limite or 15), 25))
    configurado = bool((os.getenv("LOGFIRE_READ_TOKEN") or "").strip())

    resumo_texto = _lf.buscar_erros_logfire(minutos=mins, limite=lim)
    resumo_seguro, _ = mascarar_segredos_e_pii(resumo_texto)

    brutos = _lf.consultar_erros_logfire_estruturados(minutos=mins, limite=lim)
    erros_seguros: list[dict[str, Any]] = []
    for err in brutos:
        msg_raw = str(err.get("exception_message") or err.get("message") or "")
        tb_raw = str(err.get("exception_stacktrace") or "")[-1200:]
        msg_mask, _ = mascarar_segredos_e_pii(msg_raw)
        tb_mask, _ = mascarar_segredos_e_pii(tb_raw)
        erros_seguros.append(
            {
                "timestamp": str(err.get("start_timestamp") or ""),
                "service_name": str(err.get("service_name") or "athena"),
                "span_name": str(err.get("span_name") or ""),
                "exception_type": str(err.get("exception_type") or ""),
                "message": msg_mask[:400],
                "traceback": tb_mask,
                "corrigivel_codigo": _lab.eh_bug_de_codigo_corrigivel(err),
                "fingerprint": _lab.gerar_fingerprint_erro(err),
            }
        )

    return {
        "ok": True,
        "minutos": mins,
        "logfire_configurado": configurado,
        "total_erros": len(erros_seguros),
        "erros": erros_seguros,
        "resumo_texto": resumo_seguro,
    }


@mcp.tool()
def resumo_saude_athena() -> dict[str, Any]:
    """Retorna retrato somente leitura da saude da Athena: testes, skills, grafo Obsidian, cache semantico e Lab."""
    info_testes = _contar_testes_offline()

    skills_lista = _skills.listar_skills()
    info_skills = {
        "total": len(skills_lista),
        "nomes": [s["name"] for s in skills_lista],
    }

    try:
        stats_grafo = _grafo.estatisticas_grafo()
        info_grafo: dict[str, Any] = {"disponivel": True, **stats_grafo}
    except Exception as exc:
        info_grafo = {
            "disponivel": False,
            "total_notas": 0,
            "total_conexoes": 0,
            "notas_orfas": 0,
            "top_hubs": [],
            "erro": str(exc),
        }

    info_cache = _sc.estatisticas_cache_semantico()

    try:
        todas_lab = _lab.listar_propostas_lab(status=None, limite=50)
        acionaveis = sum(1 for p in todas_lab if p.get("status") in _lab.STATUS_ACIONAVEIS_LAB)
        total_lab = len(todas_lab)
    except Exception:
        acionaveis = 0
        total_lab = 0

    return {
        "ok": True,
        "testes": info_testes,
        "skills": info_skills,
        "grafo_obsidian": info_grafo,
        "cache_semantico": info_cache,
        "athena_lab": {
            "cli_antigravity": _lab.obter_status_cli_antigravity(),
            "propostas_acionaveis": acionaveis,
            "total_propostas": total_lab,
            "sentinel_ativo": _lab.sentinel_ativo(),
        },
    }


if __name__ == "__main__":
    mcp.run()
