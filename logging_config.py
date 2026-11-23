"""Configuracao de logs estruturados com `structlog` correlacionados com OpenTelemetry.

- Injeta automaticamente `trace_id` e `span_id` quando ha um span OpenTelemetry ativo.
- Suporta `ATHENA_LOG_FORMAT=console` (padrao para dev local) ou `ATHENA_LOG_FORMAT=json` (auditoria/producao).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

_LOGGING_CONFIGURADO = False


def injetar_contexto_otel(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Processor customizado: injeta `trace_id` e `span_id` do span OpenTelemetry atual quando valido."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        span_ctx = span.get_span_context() if span is not None else None
        if span_ctx is not None and span_ctx.is_valid:
            event_dict.setdefault("trace_id", f"{span_ctx.trace_id:032x}")
            event_dict.setdefault("span_id", f"{span_ctx.span_id:016x}")
    except Exception:
        pass
    return event_dict


def _nivel_para_int(nivel: str | None) -> int:
    bruto = (nivel or os.getenv("ATHENA_LOG_LEVEL", "INFO")).strip().upper()
    mapa = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    return mapa.get(bruto, logging.INFO)


def configurar_logging(
    formato: str | None = None,
    *,
    nivel: str | None = None,
    stream: Any = None,
    forcar: bool = False,
) -> str:
    """Configura `structlog` de forma idempotente (ou forcada em testes).

    Retorna o formato efetivamente aplicado: `"json"` ou `"console"`.
    """
    global _LOGGING_CONFIGURADO
    fmt = (formato or os.getenv("ATHENA_LOG_FORMAT", "console")).strip().lower()
    if fmt not in ("console", "json"):
        fmt = "console"

    if _LOGGING_CONFIGURADO and not forcar and stream is None and formato is None:
        return fmt

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        injetar_contexto_otel,
        structlog.processors.StackInfoRenderer(),
    ]

    if fmt == "json":
        processors = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]
    else:
        processors = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(colors=stream is None),
        ]

    destino = stream if stream is not None else sys.stderr
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(_nivel_para_int(nivel)),
        context_class=dict,
        logger_factory=structlog.WriteLoggerFactory(file=destino),
        cache_logger_on_first_use=False,
    )
    _LOGGING_CONFIGURADO = True
    return fmt


def obter_logger(nome: str = "athena") -> Any:
    """Retorna um logger estruturado bound com o nome do componente."""
    if not _LOGGING_CONFIGURADO:
        configurar_logging()
    return structlog.get_logger().bind(componente=nome)
