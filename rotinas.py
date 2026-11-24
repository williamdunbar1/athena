"""Rotinas agendadas da Athena usando APScheduler (AsyncIOScheduler) + SQLite.

Integrado ao `lifespan` do FastAPI em `servidor.py`.
Persiste configuracoes e ultimo resultado na tabela `rotinas` em `memoria.db`
e salva o Briefing Diario automaticamente no Obsidian em `04-Diario/YYYY-MM-DD.md`.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agentes import MODELO_PADRAO, ContextoMissao, agente_analista, obter_modelo
from ferramentas_estudos import listar_provas
from ferramentas_google import TOKEN_PATH, buscar_emails, listar_proximos_eventos
from ferramentas_investimentos import ver_carteira
from ferramentas_obsidian import escrever_nota_vault
from logging_config import obter_logger
from ulife_v2 import consultar_dados_ulife, sincronizar_ulife

_log = obter_logger("rotinas")
TIMEOUT_FONTE_BRIEFING_SEGUNDOS = 30.0

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
