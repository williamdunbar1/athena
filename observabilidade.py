"""Observabilidade da Athena com OpenTelemetry + Pydantic Logfire.

Configuracao segura e resiliente:
- `send_to_logfire='if-token-present'`: nunca quebra offline ou sem LOGFIRE_TOKEN.
- Scrubbing extra para impedir vazamento de PIN, cookies, tokens e credenciais.
- Instrumentacao unificada para Pydantic AI (orquestrador + subagentes), FastAPI, FastMCP e HTTPX.
"""

from __future__ import annotations

import logging
import os
import warnings

_LOG = logging.getLogger("athena.observabilidade")
_OBSERVABILIDADE_CONFIGURADA = False

PADROES_SENSIVEIS_EXTRA: tuple[str, ...] = (
    "athena_pin",
    "ulife_cookies",
    "ulife_login",
    "cookie",
    "authorization",
    "credentials",
    "token",
)


def configurar_observabilidade(app=None) -> dict:
    """Inicializa Logfire + OpenTelemetry de forma idempotente e resiliente.

    Deve ser chamada logo apos `app = FastAPI(...)` em `servidor.py`.
    Nunca levanta excecao fatal caso o Logfire nao esteja configurado ou esteja offline.
    """
    global _OBSERVABILIDADE_CONFIGURADA
    resultado: dict = {
        "ok": False,
        "ja_configurada": _OBSERVABILIDADE_CONFIGURADA,
        "instrumentacoes": [],
        "avisos": [],
    }

    try:
        import logfire
    except Exception as exc:
        resultado["avisos"].append(f"logfire indisponivel: {exc}")
        return resultado

    if not _OBSERVABILIDADE_CONFIGURADA:
        try:
            logfire.configure(
                send_to_logfire="if-token-present",
                service_name="athena",
                environment=os.getenv("ATHENA_ENV", "local").strip() or "local",
                console=False,
                scrubbing=logfire.ScrubbingOptions(extra_patterns=list(PADROES_SENSIVEIS_EXTRA)),
            )
            _OBSERVABILIDADE_CONFIGURADA = True
        except Exception as exc:
            _LOG.warning("Falha ao configurar Logfire (seguindo sem telemetria remota): %s", exc)
            resultado["avisos"].append(f"configure: {exc}")
            return resultado

    # 1) Pydantic AI (orquestrador, subagentes, tools, retries, tokens e custo)
    try:
        logfire.instrument_pydantic_ai(include_binary_content=False)
        resultado["instrumentacoes"].append("pydantic_ai")
    except Exception as exc:
        resultado["avisos"].append(f"pydantic_ai: {exc}")

    # 2) FastAPI (endpoints HTTP e streaming SSE, sem capturar headers de autenticacao)
    if app is not None:
        try:
            logfire.instrument_fastapi(
                app,
                capture_headers=False,
                excluded_urls=["/api/status-atual"],
            )
            resultado["instrumentacoes"].append("fastapi")
        except Exception as exc:
            resultado["avisos"].append(f"fastapi: {exc}")

    # 3) FastMCP / MCP (envolto em try/except; suprime aviso no MCP 2/FastMCP 4 onde OTel ja e nativo)
    try:
        if hasattr(logfire, "instrument_mcp"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                logfire.instrument_mcp()
            resultado["instrumentacoes"].append("mcp")
    except Exception as exc:
        resultado["avisos"].append(f"mcp: {exc}")

    # 4) HTTPX (chamadas externas sem capturar headers de autenticacao)
    try:
        logfire.instrument_httpx(capture_headers=False)
        resultado["instrumentacoes"].append("httpx")
    except Exception as exc:
        resultado["avisos"].append(f"httpx: {exc}")

    resultado["ok"] = True
    return resultado
