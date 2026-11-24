"""Compactacao inteligente de contexto e extracao automatica de memoria em 2o plano.

Recursos:
- `preparar_historico_compactado`: mantem as N mensagens mais recentes intactas e compacta o bloco antigo
  em `resumos_conversa` no `memoria.db`, usando `gemini-3.5-flash-lite` (timeout curto de 12s) com fallback
  deterministico local instantaneo para nunca atrasar nem derrubar o chat.
- `extrair_memorias_em_background`: executa em 2o plano (`asyncio.create_task`) apos a resposta ser entregue,
  ignorando mensagens triviais, bloqueando segredos sensiveis e evitando duplicatas na tabela `fatos`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from eficiencia import normalizar_texto_eficiencia
from guardrails import mascarar_segredos_e_pii
from logging_config import obter_logger
from memoria import _conn as _conn_memoria
from memoria import guardar_fato, listar_fatos
from memoria_temporal import registrar_fato_temporal

MODELO_COMPACTACAO = "google:gemini-3.5-flash-lite"
TIMEOUT_COMPACTACAO_SEGUNDOS = 12.0
LIMITE_MSGS_PADRAO = 16
MANTER_RECENTES_PADRAO = 8
MAX_CHARS_RESUMO = 1800

_log = obter_logger("compactador")
_BG_MEMORY_TASKS: set[asyncio.Task[Any]] = set()

_SAUDACOES_TRIVIAIS: frozenset[str] = frozenset(
    {
        "oi",
        "ola",
        "eae",
        "eai",
        "fala",
        "hey",
        "hello",
        "hi",
        "bom dia",
        "boa tarde",
        "boa noite",
        "teste",
        "testando",
        "ok",
        "okay",
        "beleza",
        "blz",
        "valeu",
        "vlw",
        "obrigado",
        "obrigada",
        "obg",
        "sim",
        "nao",
        "tudo bem",
        "tudo certo",
        "show",
        "perfeito",
        "entendi",
        "certo",
    }
)

_PADROES_SEGREDO: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:senha|password|passwd|secret|segredo|pin|passphrase|private[_ ]?key)\b", re.IGNORECASE),
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|bearer|cookie|jwt|token)\b", re.IGNORECASE),
    re.compile(r"\b(?:AIza[0-9A-Za-z_-]{16,}|pylf_[0-9A-Za-z_-]{10,}|sk-[0-9A-Za-z_-]{16,}|ghp_[0-9A-Za-z]{16,})\b"),
    re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),  # CPF
    re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b"),  # Cartao
)


def _normalizar_texto(texto: str) -> str:
    sem_acento = normalizar_texto_eficiencia(texto)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", sem_acento)).strip()


def auto_memory_ativa() -> bool:
    """Controlada por `ATHENA_AUTO_MEMORY` no `.env` (padrao: `true`)."""
    val = (os.getenv("ATHENA_AUTO_MEMORY", "true") or "true").strip().lower()
    return val not in ("0", "false", "no", "off", "nao")


def _garantir_tabela_resumos(c) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS resumos_conversa (
            conversa_id TEXT PRIMARY KEY,
            resumo TEXT NOT NULL,
            mensagens_compactadas INTEGER NOT NULL,
            atualizado_em TEXT NOT NULL
        )
        """
    )


def obter_resumo_conversa(
    conversa_id: str,
    *,
    db_path: str | Path | None = None,
) -> tuple[str, int]:
    """Retorna `(resumo, mensagens_compactadas)` salvos para a conversa ou `('', 0)`."""
    cid = (conversa_id or "principal").strip() or "principal"
    with _conn_memoria(db_path) as c:
        _garantir_tabela_resumos(c)
        row = c.execute(
            "SELECT resumo, mensagens_compactadas FROM resumos_conversa WHERE conversa_id = ?",
            (cid,),
        ).fetchone()
    if not row:
        return "", 0
    return str(row[0] or ""), int(row[1] or 0)


def salvar_resumo_conversa(
    conversa_id: str,
    resumo: str,
    mensagens_compactadas: int,
    *,
    db_path: str | Path | None = None,
) -> None:
    """Persiste ou atualiza o resumo compactado na tabela `resumos_conversa`."""
    cid = (conversa_id or "principal").strip() or "principal"
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn_memoria(db_path) as c:
        _garantir_tabela_resumos(c)
        c.execute(
            """
            INSERT INTO resumos_conversa (conversa_id, resumo, mensagens_compactadas, atualizado_em)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(conversa_id) DO UPDATE SET
                resumo = excluded.resumo,
                mensagens_compactadas = excluded.mensagens_compactadas,
                atualizado_em = excluded.atualizado_em
            """,
            (cid, resumo.strip(), max(0, int(mensagens_compactadas)), agora),
        )


def limpar_resumos_conversa(
    conversa_id: str | None = None,
    *,
    db_path: str | Path | None = None,
) -> None:
    """Remove resumos compactados (da conversa especificada ou de todas ao resetar o chat)."""
    with _conn_memoria(db_path) as c:
        _garantir_tabela_resumos(c)
        if conversa_id:
            c.execute("DELETE FROM resumos_conversa WHERE conversa_id = ?", (conversa_id,))
        else:
            c.execute("DELETE FROM resumos_conversa")


def resumo_extrativo_deterministico(
    mensagens_antigas: list[dict],
    resumo_anterior: str = "",
    *,
    max_chars: int = MAX_CHARS_RESUMO,
) -> str:
    """Fallback deterministico 100% local: extrai primeiras linhas essenciais sem chamar nenhuma API."""
    linhas: list[str] = []
    if resumo_anterior.strip():
        linhas.append(resumo_anterior.strip())

    for m in mensagens_antigas:
        papel = "Usuario" if m.get("role") == "user" else "Athena"
        conteudo = str(m.get("conteudo") or "").strip()
        if not conteudo:
            continue
        primeira_linha = ""
        for ln in conteudo.splitlines():
            limpa = " ".join(ln.strip().split())
            if limpa:
                primeira_linha = limpa
                break
        if not primeira_linha:
            continue
        if len(primeira_linha) > 150:
            primeira_linha = primeira_linha[:147].rstrip() + "..."
        linhas.append(f"- {papel}: {primeira_linha}")

    if not linhas:
        return ""

    texto = "\n".join(linhas)
    if len(texto) <= max_chars:
        return texto

    # Mantem sempre os itens mais recentes dentro do teto de caracteres
    selecionadas: list[str] = []
    total = 0
    for ln in reversed(linhas):
        extra = len(ln) + (1 if selecionadas else 0)
        if total + extra > max_chars:
            break
        selecionadas.append(ln)
        total += extra
    if not selecionadas:
        return texto[-max_chars:].lstrip()
    selecionadas.reverse()
    return "\n".join(selecionadas)


async def _resumir_com_llm(
    mensagens_antigas: list[dict],
    resumo_anterior: str = "",
) -> str:
    """Compacta mensagens antigas em resumo estruturado usando `gemini-3.5-flash-lite` com timeout de 12s."""
    from pydantic_ai import Agent

    agente_resumo = Agent(
        MODELO_COMPACTACAO,
        instructions=(
            "Voce e um compactador de contexto ultra conciso em portugues do Brasil. "
            "Receba o resumo anterior (se houver) e as mensagens antigas da conversa e devolva APENAS "
            "um resumo estruturado curto (maximo 12 linhas) cobrindo: "
            "Decisoes, Fatos/Preferencias, Arquivos/Caminhos citados e Pendencias em aberto. "
            "Nunca invente informacoes nem inclua senhas/tokens."
        ),
    )

    bloco_msgs = "\n".join(
        f"{'Usuario' if m.get('role') == 'user' else 'Athena'}: {str(m.get('conteudo') or '').strip()[:500]}"
        for m in mensagens_antigas
        if str(m.get("conteudo") or "").strip()
    )
    prompt = (
        f"[RESUMO ANTERIOR]\n{resumo_anterior.strip() or '(nenhum)'}\n\n"
        f"[MENSAGENS ANTIGAS PARA COMPACTAR]\n{bloco_msgs}"
    )

    async with asyncio.timeout(TIMEOUT_COMPACTACAO_SEGUNDOS):
        res = await agente_resumo.run(prompt, retries=0)
    saida = str(getattr(res, "output", res) or "").strip()
    if not saida:
        raise ValueError("Resumo LLM retornou vazio.")
    return saida[:MAX_CHARS_RESUMO].strip()


async def preparar_historico_compactado(
    conversa_id: str,
    mensagens: list[dict],
    limite_msgs: int = LIMITE_MSGS_PADRAO,
    manter_recentes: int = MANTER_RECENTES_PADRAO,
    *,
    db_path: str | Path | None = None,
) -> tuple[str, list[dict]]:
    """Retorna `(resumo_compactado, mensagens_recentes)` para injetar no contexto da Athena.

    - Ate `limite_msgs`: retorna `("", mensagens)` sem custo extra.
    - Acima de `limite_msgs`: compacta o bloco mais antigo via `gemini-3.5-flash-lite` (timeout 12s)
      com fallback deterministico local se a API falhar, salva em `resumos_conversa` e preserva
      intactas as `manter_recentes` ultimas mensagens.
    """
    lista = list(mensagens or [])
    lim = max(2, int(limite_msgs))
    manter = max(1, min(int(manter_recentes), lim - 1))

    if len(lista) <= lim:
        return "", lista

    antigas = lista[:-manter]
    recentes = lista[-manter:]
    qtd_antigas = len(antigas)

    resumo_salvo, qtd_salva = obter_resumo_conversa(conversa_id, db_path=db_path)
    if resumo_salvo and qtd_salva >= qtd_antigas:
        return resumo_salvo, recentes

    delta_antigas = antigas[qtd_salva:] if (resumo_salvo and 0 < qtd_salva < qtd_antigas) else antigas
    t0 = time.perf_counter()
    origem = "llm"
    try:
        resumo_final = await _resumir_com_llm(delta_antigas, resumo_anterior=resumo_salvo)
    except Exception as exc:
        origem = "fallback_deterministico"
        _log.warning(
            "compactacao_fallback_deterministico",
            conversa_id=conversa_id,
            mensagens_antigas=qtd_antigas,
            tipo_erro=type(exc).__name__,
            erro=str(exc),
        )
        resumo_final = resumo_extrativo_deterministico(
            delta_antigas,
            resumo_anterior=resumo_salvo,
        )

    salvar_resumo_conversa(conversa_id, resumo_final, qtd_antigas, db_path=db_path)
    _log.info(
        "historico_compactado",
        conversa_id=conversa_id,
        origem=origem,
        mensagens_compactadas=qtd_antigas,
        mensagens_recentes=len(recentes),
        latencia_ms=max(1, int((time.perf_counter() - t0) * 1000)),
    )
    return resumo_final, recentes


def _eh_mensagem_trivial(texto: str) -> bool:
    limpo = (texto or "").strip()
    if len(limpo) < 25:
        return True
    norm = _normalizar_texto(limpo)
    return (not norm) or (norm in _SAUDACOES_TRIVIAIS)


def _contem_segredo_sensivel(texto: str) -> bool:
    bruto = texto or ""
    return any(padrao.search(bruto) for padrao in _PADROES_SEGREDO)


def _ja_existe_fato_semelhante(
    chave: str,
    valor: str,
    existentes: list[dict],
) -> bool:
    k_norm = _normalizar_texto(chave).replace(" ", "_")
    v_norm = _normalizar_texto(valor)
    if not k_norm or not v_norm:
        return True

    for item in existentes:
        ek = _normalizar_texto(str(item.get("chave") or "")).replace(" ", "_")
        ev = _normalizar_texto(str(item.get("valor") or ""))
        if not ev:
            continue
        if ek == k_norm and (ev == v_norm or v_norm in ev):
            return True
        if ev == v_norm:
            return True
    return False


def _parse_json_fatos(texto_bruto: str) -> list[dict[str, str]]:
    bruto = (texto_bruto or "").strip()
    if not bruto:
        return []
    m = re.search(r"\[[\s\S]*\]", bruto)
    trecho = m.group(0) if m else bruto
    try:
        dados = json.loads(trecho)
    except Exception:
        return []
    if not isinstance(dados, list):
        return []
    out: list[dict[str, str]] = []
    for item in dados:
        if isinstance(item, dict):
            chave = str(item.get("chave") or "").strip()
            valor = str(item.get("valor") or "").strip()
            if chave and valor:
                out.append({"chave": chave[:60], "valor": valor[:220]})
    return out[:4]


async def _extrair_fatos_com_llm(
    mensagem_usuario: str,
    resposta_athena: str,
) -> list[dict[str, str]]:
    """Extrai fatos duradouros (preferencias, metas, prazos) usando modelo leve com timeout curto."""
    from pydantic_ai import Agent

    agente_mem = Agent(
        MODELO_COMPACTACAO,
        instructions=(
            "Analise o turno da conversa e extraia APENAS fatos duradouros novos sobre o usuario "
            "(preferencias, metas, prazos importantes, curso, ferramentas preferidas ou rotina). "
            "REGRAS OBRIGATORIAS:\n"
            "- Se NAO houver fato duradouro claro, devolva exatamente: []\n"
            "- NUNCA extraia senhas, PINs, tokens, cookies, chaves de API, CPF ou dados bancarios sigilosos.\n"
            "- Responda EXCLUSIVAMENTE com um array JSON valido no formato "
            '[{"chave": "snake_case_curta", "valor": "descricao objetiva"}].'
        ),
    )
    prompt = (
        f"[MENSAGEM DO USUARIO]\n{mensagem_usuario.strip()[:1200]}\n\n"
        f"[RESPOSTA DA ATHENA]\n{(resposta_athena or '').strip()[:800]}"
    )
    async with asyncio.timeout(TIMEOUT_COMPACTACAO_SEGUNDOS):
        res = await agente_mem.run(prompt, retries=0)
    return _parse_json_fatos(str(getattr(res, "output", res) or ""))


async def extrair_memorias_em_background(
    mensagem_usuario: str,
    resposta_athena: str,
    *,
    db_path: str | Path | None = None,
    forcar: bool = False,
) -> list[tuple[str, str]]:
    """Extrai e salva fatos duradouros em 2o plano sem bloquear a entrega da resposta ao usuario."""
    if not forcar and not auto_memory_ativa():
        return []

    msg = (mensagem_usuario or "").strip()
    if _eh_mensagem_trivial(msg):
        return []
    if _contem_segredo_sensivel(msg):
        _log.info("auto_memory_ignorada_sensivel")
        return []

    t0 = time.perf_counter()
    try:
        candidatos = await _extrair_fatos_com_llm(msg, resposta_athena)
    except Exception as exc:
        _log.warning(
            "auto_memory_erro_extracao",
            tipo_erro=type(exc).__name__,
            erro=str(exc),
        )
        return []

    if not candidatos:
        return []

    existentes = listar_fatos(db_path=db_path)
    salvos: list[tuple[str, str]] = []
    for item in candidatos:
        chave_limpa = re.sub(r"[^a-z0-9_]+", "_", _normalizar_texto(item["chave"])).strip("_")[:50]
        valor_limpo = " ".join(item["valor"].split())[:220]
        if not chave_limpa or not valor_limpo:
            continue
        if _contem_segredo_sensivel(chave_limpa) or _contem_segredo_sensivel(valor_limpo):
            continue
        valor_seguro, tipos_mascarados = mascarar_segredos_e_pii(valor_limpo)
        if tipos_mascarados and "[MASCARADO:" in valor_seguro:
            restante = re.sub(r"\[MASCARADO:[a-z_]+\]", "", valor_seguro).strip()
            if len(restante) < 8:
                continue
        if _ja_existe_fato_semelhante(chave_limpa, valor_seguro, existentes):
            continue

        guardar_fato(chave_limpa, valor_seguro, db_path=db_path)
        with suppress(Exception):
            registrar_fato_temporal(
                sujeito="William",
                relacao=chave_limpa,
                objeto=valor_seguro,
                categoria="preferencia",
                origem="auto_memory",
                db_path=db_path,
            )
        existentes.append({"chave": chave_limpa, "valor": valor_seguro})
        salvos.append((chave_limpa, valor_seguro))

    if salvos:
        _log.info(
            "auto_memory_salva",
            quantidade=len(salvos),
            chaves=[k for k, _ in salvos],
            latencia_ms=max(1, int((time.perf_counter() - t0) * 1000)),
        )
    return salvos


def agendar_extracao_memorias(mensagem_usuario: str, resposta_athena: str) -> asyncio.Task[Any] | None:
    """Dispara `extrair_memorias_em_background` via `asyncio.create_task` com zero latencia percebida."""
    if not auto_memory_ativa() or _eh_mensagem_trivial(mensagem_usuario):
        return None
    try:
        task = asyncio.create_task(extrair_memorias_em_background(mensagem_usuario, resposta_athena))
    except RuntimeError:
        return None
    _BG_MEMORY_TASKS.add(task)
    task.add_done_callback(_BG_MEMORY_TASKS.discard)
    return task
