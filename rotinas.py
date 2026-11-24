"""Rotinas agendadas da Athena usando APScheduler (AsyncIOScheduler) + SQLite.

Integrado ao `lifespan` do FastAPI em `servidor.py`.
Persiste configuracoes e ultimo resultado na tabela `rotinas` em `memoria.db`
e salva o Briefing Diario automaticamente no Obsidian em `04-Diario/YYYY-MM-DD.md`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agentes import (
    MODELO_PADRAO,
    ContextoMissao,
    agente_analista,
    normalizar_agente_alvo,
    obter_agente_direto,
    obter_modelo,
)
from auditoria import registrar_atividade
from ferramentas_estudos import listar_provas
from ferramentas_google import TOKEN_PATH, buscar_emails, listar_proximos_eventos
from ferramentas_investimentos import ver_carteira
from ferramentas_obsidian import escrever_nota_vault
from logging_config import obter_logger
from memoria import obter_db_path
from ulife_v2 import consultar_dados_ulife, sincronizar_ulife

_log = obter_logger("rotinas")
TIMEOUT_FONTE_BRIEFING_SEGUNDOS = 30.0
MAX_FALHAS_AUTO_PAUSA = 3
JOB_PREFIXO_DINAMICA = "dyn:"
JOB_RECIBO_SEMANAL_ID = "recibo_semanal_sexta"
MARCADOR_ROTINA_SILENCIOSA = "SEM_NOVIDADES"

DB_PATH = Path(__file__).resolve().parent / "memoria.db"
FUSO_NOME = "America/Sao_Paulo"
ROTINA_BRIEFING_ID = "briefing_diario"
ROTINA_ULIFE_ID = "sincronizar_ulife"
ROTINA_LAB_ID = "athena_lab_semanal"


def _lab_auto_schedule_ativo() -> bool:
    return (os.getenv("ATHENA_LAB_AUTO_SCHEDULE", "false") or "false").strip().lower() in (
        "1",
        "true",
        "sim",
        "yes",
        "on",
    )

DIAS_SEMANA_PT = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)

_SCHEDULER: AsyncIOScheduler | None = None
_LOCK_EXECUCAO = asyncio.Lock()


def _obter_fuso():
    try:
        return ZoneInfo(FUSO_NOME)
    except Exception:
        return timezone(timedelta(hours=-3))


def _agora_sp() -> datetime:
    return datetime.now(_obter_fuso())


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS rotinas (
                id TEXT PRIMARY KEY,
                nome TEXT NOT NULL,
                hora INTEGER NOT NULL,
                minuto INTEGER NOT NULL,
                ativa INTEGER NOT NULL DEFAULT 1,
                ultima_execucao TEXT DEFAULT '',
                ultimo_resultado TEXT DEFAULT ''
            )
            """
        )
        c.execute(
            """
            INSERT OR IGNORE INTO rotinas (id, nome, hora, minuto, ativa, ultima_execucao, ultimo_resultado)
            VALUES (?, ?, ?, ?, ?, '', '')
            """,
            (ROTINA_BRIEFING_ID, "Briefing Diário da Athena", 8, 0, 1),
        )
        c.execute(
            """
            INSERT OR IGNORE INTO rotinas (id, nome, hora, minuto, ativa, ultima_execucao, ultimo_resultado)
            VALUES (?, ?, ?, ?, ?, '', '')
            """,
            (ROTINA_ULIFE_ID, "Sincronização Silenciosa do Ulife", 7, 55, 1),
        )
        c.execute(
            """
            INSERT OR IGNORE INTO rotinas (id, nome, hora, minuto, ativa, ultima_execucao, ultimo_resultado)
            VALUES (?, ?, ?, ?, ?, '', '')
            """,
            (ROTINA_LAB_ID, "Athena Lab — Ciclo Semanal R&D (Opcional)", 9, 0, 1 if _lab_auto_schedule_ativo() else 0),
        )
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _formatar_linha_rotina(row: tuple) -> dict:
    rid, nome, hora, minuto, ativa, ultima_exec, ultimo_res = row
    proxima = ""
    if _SCHEDULER is not None and _SCHEDULER.running:
        job = _SCHEDULER.get_job(rid)
        if job is not None and job.next_run_time is not None:
            proxima = job.next_run_time.strftime("%Y-%m-%d %H:%M")

    return {
        "id": rid,
        "nome": nome,
        "hora": int(hora),
        "minuto": int(minuto),
        "horario": f"{int(hora):02d}:{int(minuto):02d}",
        "ativa": bool(ativa),
        "ultima_execucao": ultima_exec or "",
        "ultimo_resultado": ultimo_res or "",
        "proxima_execucao": proxima,
    }


def listar_rotinas() -> list[dict]:
    """Retorna todas as rotinas cadastradas em memoria.db com status e proximo horario."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, nome, hora, minuto, ativa, ultima_execucao, ultimo_resultado "
            "FROM rotinas ORDER BY id ASC"
        ).fetchall()
    return [_formatar_linha_rotina(r) for r in rows]


def _obter_rotina(rotina_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, nome, hora, minuto, ativa, ultima_execucao, ultimo_resultado "
            "FROM rotinas WHERE id = ?",
            (rotina_id,),
        ).fetchone()
    return _formatar_linha_rotina(row) if row else None


def _sincronizar_job_scheduler(rotina: dict) -> None:
    if _SCHEDULER is None:
        return
    rid = rotina["id"]
    existente = _SCHEDULER.get_job(rid)
    if not rotina["ativa"]:
        if existente is not None:
            _SCHEDULER.remove_job(rid)
        return

    if rid == ROTINA_LAB_ID and not _lab_auto_schedule_ativo():
        if existente is not None:
            _SCHEDULER.remove_job(rid)
        return

    trigger_kwargs: dict = {
        "hour": rotina["hora"],
        "minute": rotina["minuto"],
        "timezone": _obter_fuso(),
    }
    if rid == ROTINA_LAB_ID:
        trigger_kwargs["day_of_week"] = "sun"

    trigger = CronTrigger(**trigger_kwargs)
    _SCHEDULER.add_job(
        _disparar_rotina_agendada,
        trigger=trigger,
        id=rid,
        args=[rid],
        replace_existing=True,
        misfire_grace_time=1800,
        coalesce=True,
    )


def iniciar_agendador() -> None:
    """Inicializa o AsyncIOScheduler(timezone='America/Sao_Paulo') e registra rotinas ativas + Logfire Sentinel."""
    global _SCHEDULER
    if _SCHEDULER is not None and _SCHEDULER.running:
        return
    _SCHEDULER = AsyncIOScheduler(timezone=_obter_fuso())
    for rotina in listar_rotinas():
        _sincronizar_job_scheduler(rotina)
    carregar_rotinas_dinamicas_no_scheduler()

    # Friday Receipt: agenda relatorio semanal toda sexta-feira as 18:00 (America/Sao_Paulo)
    _SCHEDULER.add_job(
        _disparar_recibo_semanal_agendado,
        trigger=CronTrigger(day_of_week="fri", hour=18, minute=0, timezone=_obter_fuso()),
        id=JOB_RECIBO_SEMANAL_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    from athena_lab import sentinel_ativo, sentinel_intervalo_minutos

    if sentinel_ativo():
        intervalo_min = sentinel_intervalo_minutos()
        _SCHEDULER.add_job(
            _disparar_logfire_sentinel,
            trigger=IntervalTrigger(minutes=intervalo_min, timezone=_obter_fuso()),
            id="logfire_sentinel",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        _log.info("logfire_sentinel_agendado", intervalo_minutos=intervalo_min)

    _SCHEDULER.start()


def encerrar_agendador() -> None:
    """Encerra o AsyncIOScheduler com seguranca no shutdown do FastAPI."""
    global _SCHEDULER
    if _SCHEDULER is not None:
        try:
            if _SCHEDULER.running:
                _SCHEDULER.shutdown(wait=False)
        except Exception:
            pass
        _SCHEDULER = None


def atualizar_rotina(rotina_id: str, hora: int, minuto: int, ativa: bool | int) -> dict:
    """Atualiza horario e estado (ativa/pausada) de uma rotina e reagenda no APScheduler."""
    try:
        h = int(hora)
        m = int(minuto)
    except (TypeError, ValueError):
        return {"ok": False, "erro": "Hora e minuto precisam ser numeros inteiros."}

    if not (0 <= h <= 23 and 0 <= m <= 59):
        return {"ok": False, "erro": "Horario invalido. Use hora 0..23 e minuto 0..59."}

    ativo_int = 1 if bool(ativa) else 0
    with _conn() as c:
        existe = c.execute("SELECT 1 FROM rotinas WHERE id = ?", (rotina_id,)).fetchone()
        if not existe:
            return {"ok": False, "erro": f"Rotina nao encontrada: {rotina_id}"}
        c.execute(
            "UPDATE rotinas SET hora = ?, minuto = ?, ativa = ? WHERE id = ?",
            (h, m, ativo_int, rotina_id),
        )

    rotina = _obter_rotina(rotina_id)
    if rotina is not None:
        _sincronizar_job_scheduler(rotina)
        rotina = _obter_rotina(rotina_id)
    return {"ok": True, "rotina": rotina}


async def _executar_briefing_diario(
    pasta_raiz: Path | None = None,
    modelo: str | None = None,
) -> tuple[str, str]:
    """Coleta agenda, Gmail, Ulife, provas e Carteira em paralelo (`asyncio.gather`), gera Briefing Diario e salva no Obsidian."""
    agora = _agora_sp()
    hoje_iso = agora.strftime("%Y-%m-%d")
    hoje_br = agora.strftime("%d/%m/%Y")
    dia_semana = DIAS_SEMANA_PT[agora.weekday()]

    def _coletar_agenda() -> str:
        if not TOKEN_PATH.is_file():
            return "Google Agenda nao conectado (token.json ausente — ignorado)."
        return listar_proximos_eventos(3)

    def _coletar_gmail() -> str:
        if not TOKEN_PATH.is_file():
            return "Gmail nao conectado (token.json ausente — ignorado)."
        return buscar_emails("is:unread", 8)

    async def _coletar_com_timeout(nome_fonte: str, fn, *args) -> str:
        t_f = time.perf_counter()
        try:
            async with asyncio.timeout(TIMEOUT_FONTE_BRIEFING_SEGUNDOS):
                res_f = await asyncio.to_thread(fn, *args)
            _log.info(
                "briefing_fonte_ok",
                fonte=nome_fonte,
                latencia_ms=max(1, int((time.perf_counter() - t_f) * 1000)),
            )
            return str(res_f)
        except Exception as exc:
            _log.warning(
                "briefing_fonte_erro",
                fonte=nome_fonte,
                latencia_ms=max(1, int((time.perf_counter() - t_f) * 1000)),
                erro=str(exc),
            )
            raise

    t_coleta = time.perf_counter()
    (
        res_agenda,
        res_emails,
        res_ulife,
        res_provas,
        res_carteira,
    ) = await asyncio.gather(
        _coletar_com_timeout("google_agenda", _coletar_agenda),
        _coletar_com_timeout("gmail", _coletar_gmail),
        _coletar_com_timeout("ulife", consultar_dados_ulife, "tudo"),
        _coletar_com_timeout("provas", listar_provas),
        _coletar_com_timeout("carteira", ver_carteira),
        return_exceptions=True,
    )
    _log.info(
        "briefing_coleta_paralela_concluida",
        latencia_ms=max(1, int((time.perf_counter() - t_coleta) * 1000)),
    )

    agenda_txt = (
        f"Nao foi possivel consultar Google Agenda: {res_agenda}"
        if isinstance(res_agenda, BaseException)
        else str(res_agenda)
    )
    emails_txt = (
        f"Nao foi possivel consultar Gmail: {res_emails}"
        if isinstance(res_emails, BaseException)
        else str(res_emails)
    )
    ulife_txt = (
        f"Erro ao ler dados do Ulife: {res_ulife}"
        if isinstance(res_ulife, BaseException)
        else str(res_ulife)
    )
    provas_txt = (
        f"Erro ao ler agenda local de provas: {res_provas}"
        if isinstance(res_provas, BaseException)
        else str(res_provas)
    )
    carteira_txt = (
        f"Nao foi possivel atualizar cotacoes da carteira simulada: {res_carteira}"
        if isinstance(res_carteira, BaseException)
        else str(res_carteira)
    )

    prompt = (
        f"Gere o **Briefing Diário da Athena** para William referente a hoje, {dia_semana}, {hoje_br} ({hoje_iso}).\n"
        "Seja direto, acionável, claro e organizado em Markdown (português do Brasil), SEM usar ferramentas do Vault "
        "(o salvamento no Obsidian será feito automaticamente em seguida).\n\n"
        "Estruture EXATAMENTE com estas 4 seções:\n"
        "## Prioridades de Hoje\n"
        "## Prazos da Faculdade\n"
        "## E-mails Importantes\n"
        "## Resumo da Carteira\n\n"
        "Se alguma fonte estiver indisponível ou vazia, mencione em 1 linha curta sem inventar dados.\n\n"
        f"### DADOS COLETADOS\n\n"
        f"#### 1. Google Agenda (próximos 3 dias)\n{agenda_txt[:3500]}\n\n"
        f"#### 2. Gmail (até 8 não lidos recentes)\n{emails_txt[:4000]}\n\n"
        f"#### 3. Portal Ulife (dados salvos no banco)\n{ulife_txt[:4500]}\n\n"
        f"#### 4. Agenda Local de Provas\n{provas_txt[:1500]}\n\n"
        f"#### 5. Carteira Simulada\n{carteira_txt[:2500]}\n"
    )

    pasta_padrao = pasta_raiz or (Path(__file__).resolve().parent / "sandbox_files")
    modelo_uso = modelo or MODELO_PADRAO
    ctx = ContextoMissao(pasta_raiz=pasta_padrao, modelo=modelo_uso)
    res = await agente_analista.run(prompt, deps=ctx, model=obter_modelo(modelo_uso))
    briefing_md = (res.output or "").strip()

    caminho_nota = f"04-Diario/{hoje_iso}.md"
    nota_obsidian = (
        "---\n"
        "tags:\n"
        "  - diario\n"
        "  - briefing\n"
        "  - athena\n"
        f"data: {hoje_iso}\n"
        f"gerado_em: \"{agora.strftime('%Y-%m-%d %H:%M')}\"\n"
        "---\n\n"
        f"# Briefing Diário — {hoje_br} ({dia_semana})\n\n"
        "Gerado automaticamente pela [[Perfil-William|Athena]] (rotina `briefing_diario`).\n\n"
        f"{briefing_md}\n"
    )
    status_vault = escrever_nota_vault(caminho_nota, nota_obsidian, anexar=False)
    return briefing_md, f"{caminho_nota} ({status_vault})"


async def _executar_sincronizacao_ulife() -> tuple[str, str]:
    """Executa sincronizacao silenciosa do Ulife v2 (cookies httpx ou Playwright fora da tela)."""
    res = await sincronizar_ulife()
    if not res.get("ok"):
        raise RuntimeError(res.get("erro") or "Falha ao sincronizar Ulife.")

    r = res.get("resumo") or {}
    modo = "Cookies diretos (httpx, sem navegador)" if r.get("modo_sync") == "httpx_cookies" else "Renovação silenciosa (Playwright fora da tela)"
    resumo_md = (
        f"### Sincronização Ulife concluída ({r.get('sincronizado_em', '-')})\n\n"
        f"- **Modo:** {modo}\n"
        f"- **Curso:** {r.get('curso') or '-'} ({r.get('instituicao') or '-'})\n"
        f"- **Matérias:** {r.get('total_disciplinas', 0)} | **Provas/Atividades:** {r.get('total_atividades', 0)} | **Avisos:** {r.get('total_avisos', 0)}\n"
        f"- **Materiais:** {r.get('total_materiais', 0)} | **PDFs novos baixados:** {r.get('pdfs_novos_baixados', 0)} | **PDFs indexados:** {r.get('pdfs_indexados', 0)}\n"
        f"- **Obsidian:** `{res.get('obsidian', '01-Faculdade/Painel-Ulife.md')}`"
    )
    return resumo_md, str(res.get("obsidian") or "01-Faculdade/Painel-Ulife.md")


async def executar_rotina_agora(
    rotina_id: str,
    pasta_raiz: Path | None = None,
    modelo: str | None = None,
) -> dict:
    """Executa uma rotina imediatamente, salva o resultado no banco e no Obsidian."""
    rotina = _obter_rotina(rotina_id)
    if rotina is None:
        return {"ok": False, "erro": f"Rotina nao encontrada: {rotina_id}"}

    async with _LOCK_EXECUCAO:
        agora_str = _agora_sp().strftime("%Y-%m-%d %H:%M")
        try:
            if rotina_id == ROTINA_BRIEFING_ID:
                briefing_md, info_obsidian = await _executar_briefing_diario(
                    pasta_raiz=pasta_raiz,
                    modelo=modelo,
                )
            elif rotina_id == ROTINA_ULIFE_ID:
                briefing_md, info_obsidian = await _executar_sincronizacao_ulife()
            elif rotina_id == ROTINA_LAB_ID:
                from athena_lab import rodar_ciclo_rd

                res_lab = await rodar_ciclo_rd()
                prop = res_lab.get("proposta") if isinstance(res_lab, dict) else None
                tit = prop.get("titulo", "-") if isinstance(prop, dict) else "-"
                st_lab = res_lab.get("status") or ("erro" if not res_lab.get("ok") else "concluido")
                briefing_md = f"### Athena Lab ({st_lab})\n- **Proposta:** {tit}\n- **Detalhe:** {res_lab.get('motivo') or res_lab.get('erro') or 'Pronta para revisao humana na aba Athena Lab.'}"
                info_obsidian = ""
            else:
                return {"ok": False, "erro": f"Executor nao implementado para rotina '{rotina_id}'."}

            with _conn() as c:
                c.execute(
                    "UPDATE rotinas SET ultima_execucao = ?, ultimo_resultado = ? WHERE id = ?",
                    (agora_str, briefing_md, rotina_id),
                )

            atualizada = _obter_rotina(rotina_id)
            return {
                "ok": True,
                "rotina": atualizada,
                "nota_obsidian": info_obsidian,
            }
        except Exception as e:
            msg_erro = f"Erro na execucao ({agora_str}): {e}"
            with _conn() as c:
                c.execute(
                    "UPDATE rotinas SET ultima_execucao = ?, ultimo_resultado = ? WHERE id = ?",
                    (agora_str, msg_erro, rotina_id),
                )
            return {"ok": False, "erro": str(e), "rotina": _obter_rotina(rotina_id)}


async def _disparar_rotina_agendada(rotina_id: str) -> None:
    try:
        await executar_rotina_agora(rotina_id)
    except Exception as exc:
        _log.warning("rotina_agendada_falhou", rotina_id=rotina_id, erro=str(exc))


async def _disparar_logfire_sentinel() -> None:
    try:
        from athena_lab import verificar_erros_logfire_sentinel

        await verificar_erros_logfire_sentinel()
    except Exception as exc:
        _log.warning("sentinel_agendado_falhou", erro=str(exc))


# ==============================================================================
# ROTINAS DINAMICAS CRIADAS PELO CHAT (CRON 5 CAMPOS + AUTO-PAUSA APOS 3 FALHAS)
# ==============================================================================


def _garantir_tabela_rotinas_dinamicas(c: sqlite3.Connection) -> None:
    c.execute(
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


@contextmanager
def _conn_dinamicas(db_path: str | Path | None = None):
    alvo = obter_db_path(db_path)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(alvo, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        _garantir_tabela_rotinas_dinamicas(c)
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _validar_cron_expr(cron_expr: str) -> str:
    expr = " ".join((cron_expr or "").split())
    partes = expr.split(" ")
    if len(partes) != 5:
        raise ValueError(
            f"Expressao cron invalida ('{cron_expr}'). Use exatamente 5 campos (ex.: '0 9 * * 1-5')."
        )
    try:
        CronTrigger.from_crontab(expr, timezone=_obter_fuso())
    except Exception as exc:
        raise ValueError(f"Expressao cron invalida ('{expr}'): {exc}") from exc
    return expr


def _row_para_rotina_dinamica(row: tuple[Any, ...]) -> dict[str, Any]:
    rid = str(row[0])
    ativa = bool(row[5])
    falhas = int(row[6] or 0)
    proxima = ""
    if _SCHEDULER is not None and _SCHEDULER.running:
        job = _SCHEDULER.get_job(f"{JOB_PREFIXO_DINAMICA}{rid}")
        if job is not None and job.next_run_time is not None:
            proxima = job.next_run_time.strftime("%Y-%m-%d %H:%M")
    return {
        "id": rid,
        "nome": str(row[1]),
        "instrucao": str(row[2]),
        "cron_expr": str(row[3]),
        "agente_alvo": str(row[4] or "athena"),
        "ativa": ativa,
        "falhas_consecutivas": falhas,
        "pausada_por_falha": (not ativa) and falhas >= MAX_FALHAS_AUTO_PAUSA,
        "ultima_execucao": str(row[7] or ""),
        "ultimo_resultado": str(row[8] or ""),
        "criado_em": str(row[9]),
        "proxima_execucao": proxima,
    }


def _sincronizar_job_dinamico(rotina: dict[str, Any]) -> None:
    if _SCHEDULER is None:
        return
    job_id = f"{JOB_PREFIXO_DINAMICA}{rotina['id']}"
    existente = _SCHEDULER.get_job(job_id)
    if not rotina.get("ativa"):
        if existente is not None:
            with suppress(Exception):
                _SCHEDULER.remove_job(job_id)
        return
    trigger = CronTrigger.from_crontab(rotina["cron_expr"], timezone=_obter_fuso())
    _SCHEDULER.add_job(
        _disparar_rotina_dinamica_agendada,
        trigger=trigger,
        id=job_id,
        args=[rotina["id"]],
        replace_existing=True,
        misfire_grace_time=1800,
        coalesce=True,
    )


def criar_rotina_dinamica(
    nome: str,
    instrucao: str,
    cron_expr: str,
    agente_alvo: str = "athena",
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Cria uma rotina dinamica agendada por expressao cron de 5 campos e registra no scheduler."""
    nome_limpo = " ".join((nome or "").split())[:120]
    inst_limpa = (instrucao or "").strip()[:2000]
    if not nome_limpo or not inst_limpa:
        raise ValueError("Informe nome e instrucao nao vazios para criar a rotina.")

    cron_valido = _validar_cron_expr(cron_expr)
    agente_norm = normalizar_agente_alvo(agente_alvo)
    rid = f"rd_{uuid.uuid4().hex[:10]}"
    agora = datetime.now().isoformat(timespec="seconds")

    with _conn_dinamicas(db_path) as c:
        c.execute(
            """
            INSERT INTO rotinas_dinamicas
                (id, nome, instrucao, cron_expr, agente_alvo, ativa, falhas_consecutivas, ultima_execucao, ultimo_resultado, criado_em)
            VALUES (?, ?, ?, ?, ?, 1, 0, '', '', ?)
            """,
            (rid, nome_limpo, inst_limpa, cron_valido, agente_norm, agora),
        )
        row = c.execute(
            """
            SELECT id, nome, instrucao, cron_expr, agente_alvo, ativa, falhas_consecutivas, ultima_execucao, ultimo_resultado, criado_em
            FROM rotinas_dinamicas WHERE id = ?
            """,
            (rid,),
        ).fetchone()

    rotina = _row_para_rotina_dinamica(row)
    _sincronizar_job_dinamico(rotina)
    with suppress(Exception):
        registrar_atividade(
            agente=agente_norm,
            acao="criar_rotina_dinamica",
            alvo=nome_limpo,
            status="sucesso",
            detalhes=f"cron={cron_valido} | id={rid}",
            db_path=db_path,
        )
    return rotina


def listar_rotinas_dinamicas(*, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Lista todas as rotinas dinamicas criadas pelo chat ou painel."""
    with _conn_dinamicas(db_path) as c:
        rows = c.execute(
            """
            SELECT id, nome, instrucao, cron_expr, agente_alvo, ativa, falhas_consecutivas, ultima_execucao, ultimo_resultado, criado_em
            FROM rotinas_dinamicas
            ORDER BY criado_em DESC, rowid DESC
            """
        ).fetchall()
    return [_row_para_rotina_dinamica(r) for r in rows]


def _obter_rotina_dinamica(rotina_id: str, *, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with _conn_dinamicas(db_path) as c:
        row = c.execute(
            """
            SELECT id, nome, instrucao, cron_expr, agente_alvo, ativa, falhas_consecutivas, ultima_execucao, ultimo_resultado, criado_em
            FROM rotinas_dinamicas WHERE id = ?
            """,
            (rotina_id,),
        ).fetchone()
    return _row_para_rotina_dinamica(row) if row else None


def alterar_status_rotina(
    rotina_id: str,
    ativa: bool,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Ativa ou pausa uma rotina dinamica (ao reativar, zera falhas_consecutivas)."""
    ativo_int = 1 if bool(ativa) else 0
    with _conn_dinamicas(db_path) as c:
        existe = c.execute("SELECT 1 FROM rotinas_dinamicas WHERE id = ?", (rotina_id,)).fetchone()
        if not existe:
            return {"ok": False, "erro": f"Rotina dinamica '{rotina_id}' nao encontrada."}
        if ativo_int == 1:
            c.execute(
                "UPDATE rotinas_dinamicas SET ativa = 1, falhas_consecutivas = 0 WHERE id = ?",
                (rotina_id,),
            )
        else:
            c.execute("UPDATE rotinas_dinamicas SET ativa = 0 WHERE id = ?", (rotina_id,))

    rotina = _obter_rotina_dinamica(rotina_id, db_path=db_path)
    if rotina is not None:
        _sincronizar_job_dinamico(rotina)
        rotina = _obter_rotina_dinamica(rotina_id, db_path=db_path)
    return {"ok": True, "rotina": rotina}


def excluir_rotina_dinamica(rotina_id: str, *, db_path: str | Path | None = None) -> dict[str, Any]:
    """Remove do scheduler e exclui permanentemente uma rotina dinamica."""
    if _SCHEDULER is not None:
        with suppress(Exception):
            _SCHEDULER.remove_job(f"{JOB_PREFIXO_DINAMICA}{rotina_id}")

    with _conn_dinamicas(db_path) as c:
        cur = c.execute("DELETE FROM rotinas_dinamicas WHERE id = ?", (rotina_id,))
        removida = int(cur.rowcount or 0) > 0

    if not removida:
        return {"ok": False, "erro": f"Rotina dinamica '{rotina_id}' nao encontrada."}
    return {"ok": True, "id": rotina_id}


def carregar_rotinas_dinamicas_no_scheduler(*, db_path: str | Path | None = None) -> int:
    """Registra todas as rotinas dinamicas ativas no APScheduler."""
    if _SCHEDULER is None:
        return 0
    carregadas = 0
    for rot in listar_rotinas_dinamicas(db_path=db_path):
        if rot.get("ativa"):
            with suppress(Exception):
                _sincronizar_job_dinamico(rot)
                carregadas += 1
    return carregadas


def eh_retorno_silencioso(texto: str) -> bool:
    """Detecta se a rotina nao encontrou novidades e deve concluir em modo silencioso (sem ruido)."""
    limpo = " ".join((texto or "").strip().split()).lower()
    if not limpo:
        return True
    marcadores = (
        "sem_novidades",
        "[sem_novidades]",
        "silencioso",
        "[silencioso]",
        "nada mudou",
        "sem novidades",
        "nenhuma novidade",
    )
    return limpo in marcadores or limpo.startswith(("[sem_novidades]", "sem_novidades:", "[silencioso]"))


async def _executar_agente_rotina_dinamica(
    rotina: dict[str, Any],
    *,
    pasta_raiz: Path | None = None,
    modelo: str | None = None,
) -> str:
    """Executa rotina em sessao 100% limpa (zero historico de chat acumulado) com suporte a retorno silencioso."""
    pasta_padrao = pasta_raiz or (Path(__file__).resolve().parent / "sandbox_files")
    modelo_uso = modelo or MODELO_PADRAO
    info_ag = obter_agente_direto(str(rotina.get("agente_alvo") or "athena"))
    instrucao_limpa = (
        f"[ROTINA AUTONOMA EM SESSAO LIMPA — {rotina.get('nome', 'Rotina')}]\n"
        f"{rotina['instrucao']}\n\n"
        f"REGRA DE SILENCIO: se nada mudou ou nao houver novidades acionaveis, responda EXATAMENTE `{MARCADOR_ROTINA_SILENCIOSA}`."
    )
    ctx = ContextoMissao(
        pasta_raiz=pasta_padrao,
        modelo=modelo_uso,
        mensagem_atual=str(rotina.get("instrucao") or ""),
        memorias_contexto="",
        resumo_conversa="",
    )
    res = await info_ag["agente"].run(
        instrucao_limpa,
        deps=ctx,
        model=obter_modelo(modelo_uso),
        message_history=[],
    )
    return str(getattr(res, "output", res) or "").strip()


async def executar_rotina_dinamica(
    rotina_id: str,
    *,
    pasta_raiz: Path | None = None,
    modelo: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Executa uma rotina dinamica em sessao limpa, com modo silencioso e auto-pausa apos 3 falhas consecutivas."""
    rotina = _obter_rotina_dinamica(rotina_id, db_path=db_path)
    if rotina is None:
        return {"ok": False, "erro": f"Rotina dinamica '{rotina_id}' nao encontrada."}

    agora_str = _agora_sp().strftime("%Y-%m-%d %H:%M")
    agente_nome = str(rotina.get("agente_alvo") or "athena")

    try:
        saida = await _executar_agente_rotina_dinamica(rotina, pasta_raiz=pasta_raiz, modelo=modelo)
        silencioso = eh_retorno_silencioso(saida)
        resultado_salvo = f"[Silencioso · {agora_str}] Sem novidades." if silencioso else saida[:4000]
        with _conn_dinamicas(db_path) as c:
            c.execute(
                """
                UPDATE rotinas_dinamicas
                SET falhas_consecutivas = 0, ultima_execucao = ?, ultimo_resultado = ?
                WHERE id = ?
                """,
                (agora_str, resultado_salvo, rotina_id),
            )
        registrar_atividade(
            agente=agente_nome,
            acao="rotina_dinamica",
            alvo=rotina["nome"],
            status="silencioso" if silencioso else "sucesso",
            detalhes="Sem novidades (execucao silenciosa)." if silencioso else saida[:300],
            db_path=db_path,
        )
        return {
            "ok": True,
            "silencioso": silencioso,
            "rotina": _obter_rotina_dinamica(rotina_id, db_path=db_path),
        }
    except Exception as exc:
        novas_falhas = int(rotina.get("falhas_consecutivas") or 0) + 1
        auto_pausada = novas_falhas >= MAX_FALHAS_AUTO_PAUSA
        novo_ativa = 0 if auto_pausada else (1 if rotina.get("ativa") else 0)
        msg_erro = f"Erro ({agora_str}) [falha {novas_falhas}/{MAX_FALHAS_AUTO_PAUSA}]: {exc}"
        if auto_pausada:
            msg_erro += " — Rotina pausada automaticamente por seguranca."

        with _conn_dinamicas(db_path) as c:
            c.execute(
                """
                UPDATE rotinas_dinamicas
                SET falhas_consecutivas = ?, ativa = ?, ultima_execucao = ?, ultimo_resultado = ?
                WHERE id = ?
                """,
                (novas_falhas, novo_ativa, agora_str, msg_erro[:4000], rotina_id),
            )

        if auto_pausada and _SCHEDULER is not None:
            with suppress(Exception):
                _SCHEDULER.remove_job(f"{JOB_PREFIXO_DINAMICA}{rotina_id}")

        registrar_atividade(
            agente=agente_nome,
            acao="rotina_auto_pausada" if auto_pausada else "rotina_dinamica",
            alvo=rotina["nome"],
            status="alerta_auto_pausa" if auto_pausada else "erro",
            detalhes=msg_erro,
            db_path=db_path,
        )
        return {
            "ok": False,
            "erro": str(exc),
            "auto_pausada": auto_pausada,
            "falhas_consecutivas": novas_falhas,
            "rotina": _obter_rotina_dinamica(rotina_id, db_path=db_path),
        }


async def _disparar_rotina_dinamica_agendada(rotina_id: str) -> None:
    try:
        await executar_rotina_dinamica(rotina_id)
    except Exception as exc:
        _log.warning("rotina_dinamica_agendada_falhou", rotina_id=rotina_id, erro=str(exc))


# ==============================================================================
# FRIDAY RECEIPT (RECIBO SEMANAL DE OPERACOES, ECONOMIA E SUGESTOES — 0 LLM)
# ==============================================================================


def _garantir_tabela_recibos(c: sqlite3.Connection) -> None:
    c.execute(
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
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_recibos_semanais_criado_em
        ON recibos_semanais (criado_em DESC)
        """
    )


def _tabela_existe(c: sqlite3.Connection, nome: str) -> bool:
    row = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (nome,),
    ).fetchone()
    return row is not None


def gerar_recibo_semanal(db_path: str | Path | None = None) -> dict[str, Any]:
    """Gera o Friday Receipt semanal agregando metricas reais locais (0 chamadas de LLM) e salva em `recibos_semanais`."""
    from semantic_cache import estatisticas_cache_semantico

    agora = _agora_sp()
    inicio_dt = agora - timedelta(days=7)
    periodo = f"{inicio_dt.strftime('%Y-%m-%d')} a {agora.strftime('%Y-%m-%d')}"
    inicio_iso = inicio_dt.strftime("%Y-%m-%dT00:00:00")
    criado_em = agora.strftime("%Y-%m-%d %H:%M:%S")
    rid = f"rec_{uuid.uuid4().hex[:10]}"

    alvo = obter_db_path(db_path)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(alvo, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        _garantir_tabela_recibos(c)

        # 1) Rotinas dinamicas
        rot_total = 0
        rot_ativas = 0
        rot_pausadas_falha: list[str] = []
        sugestoes_pausar: list[str] = []
        sugestoes_script: list[str] = []
        if _tabela_existe(c, "rotinas_dinamicas"):
            rows_rot = c.execute(
                "SELECT id, nome, cron_expr, ativa, falhas_consecutivas, ultima_execucao, ultimo_resultado FROM rotinas_dinamicas"
            ).fetchall()
            rot_total = len(rows_rot)
            for _r_id, r_nome, r_cron, r_ativa, r_falhas, r_ult, r_res in rows_rot:
                ativa_b = bool(r_ativa)
                falhas_n = int(r_falhas or 0)
                if ativa_b:
                    rot_ativas += 1
                if (not ativa_b) and falhas_n >= MAX_FALHAS_AUTO_PAUSA:
                    rot_pausadas_falha.append(str(r_nome))
                if falhas_n >= 2:
                    sugestoes_pausar.append(f"**{r_nome}** (`{r_cron}` — {falhas_n} falha(s) consecutiva(s))")
                elif ativa_b and not str(r_ult or "").strip():
                    sugestoes_pausar.append(f"**{r_nome}** (`{r_cron}` — ainda sem execucao registrada)")
                elif ativa_b and falhas_n == 0 and str(r_res or "").startswith("[Silencioso"):
                    sugestoes_script.append(f"**{r_nome}** (`{r_cron}` — retornou silenciosa; candidata a script deterministico 0 tokens)")

        # 2) Atividade / Auditoria (ultimos 7 dias)
        atv_total = 0
        atv_sucesso = 0
        atv_silenciosas = 0
        atv_erros = 0
        agentes_top: list[tuple[str, int]] = []
        if _tabela_existe(c, "atividade_agentes"):
            rows_atv = c.execute(
                "SELECT agente, status FROM atividade_agentes WHERE criado_em >= ?",
                (inicio_iso,),
            ).fetchall()
            atv_total = len(rows_atv)
            cont_ag: dict[str, int] = {}
            for ag, st in rows_atv:
                ag_s = str(ag or "sistema")
                st_s = str(st or "").lower()
                cont_ag[ag_s] = cont_ag.get(ag_s, 0) + 1
                if st_s == "silencioso":
                    atv_silenciosas += 1
                elif st_s in ("erro", "alerta_auto_pausa", "alerta_bloqueio", "reprovado"):
                    atv_erros += 1
                else:
                    atv_sucesso += 1
            agentes_top = sorted(cont_ag.items(), key=lambda x: x[1], reverse=True)[:5]

        # 3) Board (Done / Blocked / Review / Working / Todo)
        board_counts: dict[str, int] = {"Todo": 0, "Working": 0, "Blocked": 0, "Ready for Review": 0, "Done": 0}
        board_done: list[str] = []
        board_blocked: list[str] = []
        if _tabela_existe(c, "board_tarefas"):
            rows_brd = c.execute(
                "SELECT titulo, status FROM board_tarefas ORDER BY atualizado_em DESC"
            ).fetchall()
            for tit, st in rows_brd:
                st_s = str(st or "Todo")
                board_counts[st_s] = board_counts.get(st_s, 0) + 1
                if st_s == "Done" and len(board_done) < 8:
                    board_done.append(str(tit))
                elif st_s == "Blocked" and len(board_blocked) < 8:
                    board_blocked.append(str(tit))

        # 4) Cache Semantico (economia de tokens)
        cache_stats = estatisticas_cache_semantico()
        cache_hits = int(cache_stats.get("hits") or 0)
        cache_misses = int(cache_stats.get("misses") or 0)
        cache_entradas = int(cache_stats.get("entradas") or cache_stats.get("tamanho") or 0)
        total_cache_consultas = cache_hits + cache_misses
        taxa_cache = round((cache_hits / total_cache_consultas) * 100, 1) if total_cache_consultas > 0 else 0.0

        metricas: dict[str, Any] = {
            "periodo": periodo,
            "rotinas": {
                "total": rot_total,
                "ativas": rot_ativas,
                "pausadas_por_falha": len(rot_pausadas_falha),
            },
            "atividade": {
                "total_7d": atv_total,
                "sucesso": atv_sucesso,
                "silenciosas": atv_silenciosas,
                "erros_alertas": atv_erros,
                "top_agentes": [{"agente": a, "acoes": n} for a, n in agentes_top],
            },
            "board": {
                "contagem": board_counts,
                "done": board_done,
                "blocked": board_blocked,
            },
            "cache_semantico": {
                "hits": cache_hits,
                "misses": cache_misses,
                "taxa_acerto_pct": taxa_cache,
                "entradas": cache_entradas,
            },
            "sugestoes": {
                "pausar": sugestoes_pausar,
                "virar_script": sugestoes_script,
            },
        }

        linhas: list[str] = [
            f"# Friday Receipt — {periodo}",
            "",
            "## 1. Resumo Operacional (7 dias)",
            f"- **Atividades auditadas:** {atv_total} ({atv_sucesso} ok · {atv_silenciosas} silenciosas · {atv_erros} erros/alertas)",
            f"- **Rotinas dinâmicas:** {rot_ativas}/{rot_total} ativas ({len(rot_pausadas_falha)} pausada(s) por falha)",
            f"- **The Board:** {board_counts['Done']} Done · {board_counts['Blocked']} Blocked · {board_counts['Ready for Review']} Review · {board_counts['Working']} Working · {board_counts['Todo']} Todo",
            f"- **Cache Semântico (0 tokens):** {cache_hits} acerto(s) / {total_cache_consultas} consulta(s) ({taxa_cache}% de economia · {cache_entradas} em RAM)",
            "",
        ]

        if board_done or board_blocked:
            linhas.append("## 2. Entregas & Bloqueios no Board")
            for tit in board_done:
                linhas.append(f"- [Done] {tit}")
            for tit in board_blocked:
                linhas.append(f"- [Blocked — requer ação humana] {tit}")
            linhas.append("")

        if agentes_top:
            linhas.append("## 3. Agentes Mais Ativos")
            for ag_n, qtd in agentes_top:
                linhas.append(f"- **{ag_n}**: {qtd} ação(ões)")
            linhas.append("")

        linhas.append("## 4. Sugestões de Otimização")
        if sugestoes_pausar:
            linhas.append("**Candidatas a pausar / revisar:**")
            for s in sugestoes_pausar:
                linhas.append(f"- {s}")
        if sugestoes_script:
            linhas.append("**Candidatas a virar script determinístico (0 LLM):**")
            for s in sugestoes_script:
                linhas.append(f"- {s}")
        if not sugestoes_pausar and not sugestoes_script:
            linhas.append("- Nenhuma rotina com ruído ou falhas recorrentes detectada nesta semana.")

        resumo_md = "\n".join(linhas).strip()
        metricas_json = json.dumps(metricas, ensure_ascii=False)

        c.execute(
            """
            INSERT INTO recibos_semanais (id, periodo, resumo_md, metricas_json, criado_em)
            VALUES (?, ?, ?, ?, ?)
            """,
            (rid, periodo, resumo_md, metricas_json, criado_em),
        )
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

    with suppress(Exception):
        registrar_atividade(
            agente="Athena (Rotinas)",
            acao="gerar_recibo_semanal",
            alvo=periodo,
            status="sucesso",
            detalhes=f"Friday Receipt {rid} gerado (0 LLM).",
            db_path=db_path,
        )

    return {
        "id": rid,
        "periodo": periodo,
        "resumo_md": resumo_md,
        "metricas": metricas,
        "metricas_json": metricas_json,
        "criado_em": criado_em,
    }


def listar_recibos_semanais(limite: int = 12, *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Retorna os recibos semanais salvos em `recibos_semanais` do mais recente para o mais antigo."""
    lim = max(1, min(int(limite), 50))
    alvo = obter_db_path(db_path)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(alvo, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        _garantir_tabela_recibos(c)
        rows = c.execute(
            """
            SELECT id, periodo, resumo_md, metricas_json, criado_em
            FROM recibos_semanais
            ORDER BY criado_em DESC, rowid DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    finally:
        c.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        metricas_obj: dict[str, Any] = {}
        with suppress(Exception):
            parsed = json.loads(str(r[3] or "{}"))
            if isinstance(parsed, dict):
                metricas_obj = parsed
        out.append(
            {
                "id": str(r[0]),
                "periodo": str(r[1]),
                "resumo_md": str(r[2]),
                "metricas": metricas_obj,
                "metricas_json": str(r[3] or "{}"),
                "criado_em": str(r[4]),
            }
        )
    return out


async def _disparar_recibo_semanal_agendado() -> None:
    try:
        await asyncio.to_thread(gerar_recibo_semanal)
    except Exception as exc:
        _log.warning("recibo_semanal_agendado_falhou", erro=str(exc))
