"""Gerenciador de servidores MCP configuraveis para a Athena (Pydantic AI v2 + FastMCP).

Le `mcp_servers.json`, expande variaveis de ambiente `${ENV_VAR}` em memoria
(nunca expondo segredos na API ou logs), cria `MCPToolset` com `StdioTransport`
ou `StreamableHttpTransport` e isola falhas para nunca impedir a Athena de iniciar.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from pydantic_ai.mcp import MCPToolset

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "mcp_servers.json"
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_TOOLSETS_CACHE: list[MCPToolset] | None = None
_STATUS_CACHE: list[dict] = []
_MCP_EXIT_STACK: AsyncExitStack | None = None
_MCP_LOCK = asyncio.Lock()


def _expandir_env_texto(valor: str) -> tuple[str, list[str]]:
    faltantes: list[str] = []

    def _subst(match: re.Match[str]) -> str:
        chave = match.group(1)
        padrao = match.group(2)
        if chave == "PYTHON":
            return sys.executable
        env_val = os.getenv(chave)
        if env_val is None or env_val.strip() == "":
            if padrao is not None:
                return padrao
            faltantes.append(chave)
            return ""
        return env_val.strip()

    return _ENV_PATTERN.sub(_subst, valor or ""), faltantes


def _expandir_dict_env(mapa: dict | None) -> tuple[dict[str, str], list[str]]:
    if not isinstance(mapa, dict):
        return {}, []
    out: dict[str, str] = {}
    faltantes: list[str] = []
    for k, v in mapa.items():
        txt, miss = _expandir_env_texto(str(v))
        out[str(k)] = txt
        faltantes.extend(miss)
    return out, faltantes


def _resolver_comando_e_args(comando_raw: str, args_raw: list) -> tuple[str, list[str], list[str]]:
    faltantes: list[str] = []
    cmd, miss_cmd = _expandir_env_texto(comando_raw)
    faltantes.extend(miss_cmd)

    cmd_limpo = cmd.strip()
    if cmd_limpo.lower() in ("python", "python3", "python.exe"):
        cmd_limpo = sys.executable

    args_resolvidos: list[str] = []
    for item in args_raw or []:
        arg_exp, miss_arg = _expandir_env_texto(str(item))
        faltantes.extend(miss_arg)
        # Se for script local relativo dentro do projeto, resolve para caminho absoluto
        if arg_exp.endswith(".py") and not Path(arg_exp).is_absolute():
            cand = BASE_DIR / arg_exp
            if cand.is_file():
                arg_exp = str(cand)
        args_resolvidos.append(arg_exp)

    return cmd_limpo, args_resolvidos, faltantes


def _ler_config_bruta() -> list[dict]:
    if not CONFIG_PATH.is_file():
        return []
    try:
        dados = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        servidores = dados.get("servidores") if isinstance(dados, dict) else None
        return servidores if isinstance(servidores, list) else []
    except Exception:
        return []


def _criar_callback_trilha(nome_servidor: str):
    async def _processar_chamada_mcp(ctx, call_tool, tool_name, tool_args):
        deps = getattr(ctx, "deps", None)
        if deps is not None and hasattr(deps, "registrar"):
            resumo_args = str(tool_args)[:180]
            deps.registrar("Agente Sistema", f"MCP ({nome_servidor})", "ferramenta", f"{tool_name}({resumo_args})")
        return await call_tool(tool_name, tool_args, metadata=None)

    return _processar_chamada_mcp


def carregar_servidores_mcp(recarregar: bool = False) -> list[MCPToolset]:
    """Carrega os servidores MCP ativos de mcp_servers.json com tratamento gracioso de erros."""
    global _TOOLSETS_CACHE, _STATUS_CACHE
    if _TOOLSETS_CACHE is not None and not recarregar:
        return list(_TOOLSETS_CACHE)

    toolsets: list[MCPToolset] = []
    status_lista: list[dict] = []

    for idx, srv in enumerate(_ler_config_bruta()):
        if not isinstance(srv, dict):
            continue
        nome = str(srv.get("nome") or f"mcp-{idx + 1}").strip()
        ativo = bool(srv.get("ativo", False))
        opcional_sem_env = bool(srv.get("opcional_sem_env", False))
        requer_env_val = srv.get("requer_env")
        requer_env: list = requer_env_val if isinstance(requer_env_val, list) else []
        transporte = str(srv.get("transporte") or "stdio").strip().lower()
        descricao = str(srv.get("descricao") or "").strip()
        comando_raw = str(srv.get("comando") or "").strip()
        args_val = srv.get("args")
        args_raw: list = args_val if isinstance(args_val, list) else []
        url_raw = str(srv.get("url") or "").strip()

        url_exibicao = url_raw
        if transporte != "stdio" and url_raw:
            url_tmp, _miss_url = _expandir_env_texto(url_raw)
            if url_tmp.startswith(("http://", "https://")):
                url_exibicao = url_tmp

        alvo_exibicao = (
            f"{comando_raw} {' '.join(str(a) for a in args_raw)}".strip()
            if transporte == "stdio"
            else url_exibicao
        )

        registro_status = {
            "nome": nome,
            "ativo": ativo,
            "transporte": transporte,
            "alvo": alvo_exibicao,
            "descricao": descricao,
            "status": "inativo",
            "erro": "",
        }

        if not ativo:
            status_lista.append(registro_status)
            continue

        # Se for servidor condicional a variaveis de ambiente, ignora silenciosamente sem erro
        # quando a variavel nao estiver definida.
        envs_ausentes = [str(k).strip() for k in requer_env if not (os.getenv(str(k).strip()) or "").strip()]
        if envs_ausentes and opcional_sem_env:
            registro_status["ativo"] = False
            registro_status["status"] = "inativo"
            status_lista.append(registro_status)
            continue

        try:
            if transporte == "stdio":
                cmd, args_res, miss = _resolver_comando_e_args(comando_raw, args_raw)
                env_extra, miss_env = _expandir_dict_env(srv.get("env"))
                miss.extend(miss_env)
                if miss:
                    raise ValueError(f"Variavel(is) de ambiente ausente(s): {', '.join(sorted(set(miss)))}")
                if not cmd:
                    raise ValueError("Campo 'comando' obrigatorio para transporte stdio.")

                # Valida se script .py existe quando passado como primeiro argumento
                if args_res and args_res[0].endswith(".py") and not Path(args_res[0]).is_file():
                    raise FileNotFoundError(f"Script MCP nao encontrado: {args_res[0]}")

                env_final = {**os.environ, **env_extra} if env_extra else None
                transport_obj = StdioTransport(
                    command=cmd,
                    args=args_res,
                    env=env_final,
                    cwd=str(BASE_DIR),
                )
            elif transporte in ("http", "streamable-http", "sse"):
                url_exp, miss = _expandir_env_texto(url_raw)
                headers_exp, miss_h = _expandir_dict_env(srv.get("headers"))
                miss.extend(miss_h)
                if miss:
                    raise ValueError(f"Variavel(is) de ambiente ausente(s): {', '.join(sorted(set(miss)))}")
                if not url_exp.startswith(("http://", "https://")):
                    raise ValueError(f"URL invalida para servidor MCP HTTP: '{url_exp}'")

                if transporte == "sse":
                    transport_obj = SSETransport(url=url_exp, headers=headers_exp or None)
                else:
                    transport_obj = StreamableHttpTransport(url=url_exp, headers=headers_exp or None)
            else:
                raise ValueError(f"Transporte MCP nao suportado: '{transporte}' (use 'stdio' ou 'http').")

            toolset = MCPToolset(
                transport_obj,
                id=f"mcp:{nome}",
                init_timeout=15.0,
                read_timeout=30.0,
                process_tool_call=_criar_callback_trilha(nome),
            )
            toolsets.append(toolset)
            registro_status["status"] = "ativo"
        except Exception as e:
            registro_status["status"] = "erro"
            registro_status["erro"] = str(e)
            print(f"[MCP aviso] Servidor '{nome}' ignorado: {e}", file=sys.stderr)

        status_lista.append(registro_status)

    _TOOLSETS_CACHE = toolsets
    _STATUS_CACHE = status_lista
    return list(_TOOLSETS_CACHE)


def obter_toolsets_mcp() -> list[MCPToolset]:
    """Retorna a lista de MCPToolset ativos prontos para anexar a um Agent do Pydantic AI."""
    return carregar_servidores_mcp(recarregar=False)


async def iniciar_conexoes_mcp() -> list[dict]:
    """Mantem as sessoes dos servidores MCP abertas durante o lifespan do FastAPI (reuso sem respawn por turno)."""
    global _MCP_EXIT_STACK
    async with _MCP_LOCK:
        if _MCP_EXIT_STACK is not None:
            return listar_servidores_mcp()

        toolsets = carregar_servidores_mcp(recarregar=False)
        if not toolsets:
            return listar_servidores_mcp()

        stack = AsyncExitStack()
        for ts in toolsets:
            nome_srv = (ts.id or "").removeprefix("mcp:") or "mcp"
            try:
                await stack.enter_async_context(ts)
            except Exception as e:
                print(f"[MCP aviso] Falha ao pre-conectar '{nome_srv}': {e}", file=sys.stderr)
                for item in _STATUS_CACHE:
                    if item.get("nome") == nome_srv:
                        item["status"] = "erro"
                        item["erro"] = str(e)
                        break

        _MCP_EXIT_STACK = stack
        return listar_servidores_mcp()


async def encerrar_conexoes_mcp() -> None:
    """Encerra graciosamente os subprocessos/sessoes MCP mantidos em memoria no shutdown da API."""
    global _MCP_EXIT_STACK
    async with _MCP_LOCK:
        if _MCP_EXIT_STACK is None:
            return
        stack = _MCP_EXIT_STACK
        _MCP_EXIT_STACK = None
        try:
            await stack.aclose()
        except Exception as e:
            print(f"[MCP aviso] Erro ao encerrar conexoes MCP: {e}", file=sys.stderr)


def listar_servidores_mcp() -> list[dict]:
    """Retorna os servidores de mcp_servers.json com status (ativo/inativo/erro), sem expor segredos."""
    if _TOOLSETS_CACHE is None:
        carregar_servidores_mcp(recarregar=False)
    return [dict(item) for item in _STATUS_CACHE]
