"""The Board + Handoff Estruturado Enxuto + Agente Checker independente ("No proof, no done").

Mantem tarefas multi-etapa em um quadro persistido no `memoria.db` (`board_tarefas`):
- Colunas: `Todo`, `Working`, `Blocked`, `Ready for Review`, `Done`.
- `formatar_handoff_enxuto(card)`: entrega apenas os 7 campos essenciais
  (`from`, `to`, `objective`, `artifacts`, `decisions`, `open_questions`, `next_gate`),
  nunca o historico inteiro do chat.
- `avaliar_card_checker(card_id, usar_llm=False)`: validador independente que aplica a regra
  "No proof, no done", reprovando para `Working` com feedback estruturado (tentativa 1) e
  movendo para `Blocked` + alerta na auditoria apos 2 reprovacoes para evitar loops infinitos.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from auditoria import registrar_atividade
from guardrails import mascarar_segredos_e_pii
from memoria import obter_db_path

STATUS_BOARD_VALIDOS: tuple[str, ...] = (
    "Todo",
    "Working",
    "Blocked",
    "Ready for Review",
    "Done",
)
_MAPA_STATUS_BOARD: dict[str, str] = {
    "todo": "Todo",
    "a fazer": "Todo",
    "working": "Working",
    "em andamento": "Working",
    "fazendo": "Working",
    "blocked": "Blocked",
    "bloqueado": "Blocked",
    "ready for review": "Ready for Review",
    "review": "Ready for Review",
    "revisao": "Ready for Review",
    "done": "Done",
    "concluido": "Done",
}

CAMPOS_HANDOFF: tuple[str, ...] = (
    "from",
    "to",
    "objective",
    "artifacts",
    "decisions",
    "open_questions",
    "next_gate",
)
MAX_TENTATIVAS_CHECKER = 2
MODELO_CHECKER = "google:gemini-3.5-flash-lite"
_PROVAS_PLACEHOLDER: frozenset[str] = frozenset(
    {"ok", "feito", "pronto", "done", "sim", "concluido", "resolvido", "testado", "validado", "-", "n/a"}
)


def _garantir_tabela_board(c: sqlite3.Connection) -> None:
    c.execute(
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
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_board_tarefas_status
        ON board_tarefas (status, atualizado_em DESC)
        """
    )


@contextmanager
def _conn_board(db_path: str | Path | None = None):
    alvo = obter_db_path(db_path)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(alvo, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        _garantir_tabela_board(c)
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def normalizar_status_board(status: str) -> str:
    chave = " ".join((status or "").strip().lower().split())
    if chave in _MAPA_STATUS_BOARD:
        return _MAPA_STATUS_BOARD[chave]
    raise ValueError(
        f"Status de board invalido: '{status}'. Use um de: {', '.join(STATUS_BOARD_VALIDOS)}."
    )


def _limpar_str(val: Any, limite: int = 320) -> str:
    txt = " ".join(str(val or "").split())[:limite]
    seg, _ = mascarar_segredos_e_pii(txt)
    return seg


def _limpar_lista(val: Any, *, max_itens: int = 12, max_chars: int = 240) -> list[str]:
    if not val:
        return []
    bruto = [val] if isinstance(val, str) else list(val) if isinstance(val, (list, tuple)) else [str(val)]
    out: list[str] = []
    for item in bruto:
        txt = _limpar_str(item, limite=max_chars)
        if txt:
            out.append(txt)
        if len(out) >= max_itens:
            break
    return out


def normalizar_handoff(
    handoff: dict[str, Any] | str | None,
    *,
    fallback_from: str = "Athena",
    fallback_to: str = "",
    fallback_objective: str = "",
    fallback_gate: str = "",
) -> dict[str, Any]:
    """Extrai estritamente os 7 campos de handoff enxuto (sem historico de chat)."""
    dados: dict[str, Any] = {}
    if isinstance(handoff, str) and handoff.strip():
        with suppress(Exception):
            parsed = json.loads(handoff)
            if isinstance(parsed, dict):
                dados = parsed
    elif isinstance(handoff, dict):
        dados = handoff

    return {
        "from": _limpar_str(dados.get("from") or fallback_from, limite=80) or "Athena",
        "to": _limpar_str(dados.get("to") or fallback_to, limite=80),
        "objective": _limpar_str(dados.get("objective") or fallback_objective, limite=280),
        "artifacts": _limpar_lista(dados.get("artifacts")),
        "decisions": _limpar_lista(dados.get("decisions")),
        "open_questions": _limpar_lista(dados.get("open_questions")),
        "next_gate": _limpar_str(dados.get("next_gate") or fallback_gate, limite=220),
    }


def normalizar_provas(provas: list[Any] | dict[str, Any] | str | None) -> list[str]:
    """Normaliza lista de provas concretas (comandos, diffs, caminhos, resultados) mascarando segredos."""
    if not provas:
        return []
    if isinstance(provas, str):
        bruto = provas.strip()
        if not bruto:
            return []
        if bruto.startswith("["):
            try:
                parsed = json.loads(bruto)
                if isinstance(parsed, list):
                    return _limpar_lista(parsed, max_itens=20, max_chars=400)
            except Exception:
                pass
        return _limpar_lista([ln for ln in bruto.splitlines() if ln.strip()], max_itens=20, max_chars=400)
    if isinstance(provas, dict):
        return _limpar_lista([f"{k}: {v}" for k, v in provas.items()], max_itens=20, max_chars=400)
    return _limpar_lista(provas, max_itens=20, max_chars=400)


def _row_para_card(row: tuple[Any, ...]) -> dict[str, Any]:
    titulo = str(row[1])
    dono = str(row[2])
    criterio = str(row[4] or "")
    handoff = normalizar_handoff(
        row[5],
        fallback_from="Athena",
        fallback_to=dono,
        fallback_objective=titulo,
        fallback_gate=criterio or "Validar provas no Checker",
    )
    provas = normalizar_provas(row[6])
    return {
        "id": str(row[0]),
        "titulo": titulo,
        "dono": dono,
        "status": str(row[3]),
        "criterio_done": criterio,
        "handoff": handoff,
        "handoff_json": json.dumps(handoff, ensure_ascii=False),
        "provas": provas,
        "provas_json": json.dumps(provas, ensure_ascii=False),
        "tentativas_checker": int(row[7] or 0),
        "criado_em": str(row[8]),
        "atualizado_em": str(row[9]),
    }


def formatar_handoff_enxuto(card: dict[str, Any]) -> str:
    """Retorna APENAS os 7 campos estruturados de handoff em JSON compacto, nunca o historico do chat."""
    bruto = card.get("handoff") or card.get("handoff_json")
    enxuto = normalizar_handoff(
        bruto,
        fallback_from="Athena",
        fallback_to=str(card.get("dono") or ""),
        fallback_objective=str(card.get("titulo") or ""),
        fallback_gate=str(card.get("criterio_done") or "Validar provas no Checker"),
    )
    # Garante estritamente as 7 chaves canonicas
    estrito = {campo: enxuto[campo] for campo in CAMPOS_HANDOFF}
    return json.dumps(estrito, ensure_ascii=False, indent=2)


def criar_card_board(
    titulo: str,
    dono: str,
    criterio_done: str = "",
    handoff: dict[str, Any] | str | None = None,
    *,
    status: str = "Todo",
    provas: list[Any] | str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Cria um card no Board (`board_tarefas`) com dono, criterio de conclusao e handoff enxuto."""
    tit_limpo = _limpar_str(titulo, limite=180)
    dono_limpo = _limpar_str(dono or "athena", limite=60) or "athena"
    if not tit_limpo:
        raise ValueError("Informe um titulo nao vazio para o card do board.")

    st_valido = normalizar_status_board(status)
    crit_limpo = _limpar_str(criterio_done, limite=400)
    handoff_norm = normalizar_handoff(
        handoff,
        fallback_from="Athena",
        fallback_to=dono_limpo,
        fallback_objective=tit_limpo,
        fallback_gate=crit_limpo or "Apresentar provas objetivas antes de concluir",
    )
    provas_norm = normalizar_provas(provas)

    cid = f"brd_{uuid.uuid4().hex[:10]}"
    agora = datetime.now().isoformat(timespec="seconds")

    with _conn_board(db_path) as c:
        c.execute(
            """
            INSERT INTO board_tarefas
                (id, titulo, dono, status, criterio_done, handoff_json, provas_json, tentativas_checker, criado_em, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                cid,
                tit_limpo,
                dono_limpo,
                st_valido,
                crit_limpo,
                json.dumps(handoff_norm, ensure_ascii=False),
                json.dumps(provas_norm, ensure_ascii=False),
                agora,
                agora,
            ),
        )
        row = c.execute(
            """
            SELECT id, titulo, dono, status, criterio_done, handoff_json, provas_json, tentativas_checker, criado_em, atualizado_em
            FROM board_tarefas WHERE id = ?
            """,
            (cid,),
        ).fetchone()

    return _row_para_card(row)


def obter_card_board(card_id: str, *, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with _conn_board(db_path) as c:
        row = c.execute(
            """
            SELECT id, titulo, dono, status, criterio_done, handoff_json, provas_json, tentativas_checker, criado_em, atualizado_em
            FROM board_tarefas WHERE id = ?
            """,
            (card_id,),
        ).fetchone()
    return _row_para_card(row) if row else None


def atualizar_card_board(
    card_id: str,
    status: str | None = None,
    handoff: dict[str, Any] | str | None = None,
    provas: list[Any] | dict[str, Any] | str | None = None,
    *,
    titulo: str | None = None,
    dono: str | None = None,
    criterio_done: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Atualiza status, handoff estruturado ou provas de um card existente no Board."""
    atual = obter_card_board(card_id, db_path=db_path)
    if atual is None:
        raise ValueError(f"Card do board '{card_id}' nao encontrado.")

    novo_titulo = _limpar_str(titulo, limite=180) if titulo is not None else atual["titulo"]
    novo_dono = _limpar_str(dono, limite=60) if dono is not None else atual["dono"]
    novo_status = normalizar_status_board(status) if status is not None else atual["status"]
    novo_criterio = _limpar_str(criterio_done, limite=400) if criterio_done is not None else atual["criterio_done"]

    if handoff is not None:
        base_hf = dict(atual["handoff"])
        novo_hf_raw = handoff
        if isinstance(novo_hf_raw, str):
            with suppress(Exception):
                parsed = json.loads(novo_hf_raw)
                if isinstance(parsed, dict):
                    base_hf.update(parsed)
        elif isinstance(novo_hf_raw, dict):
            base_hf.update(novo_hf_raw)
        novo_handoff = normalizar_handoff(
            base_hf,
            fallback_from="Athena",
            fallback_to=novo_dono,
            fallback_objective=novo_titulo,
            fallback_gate=novo_criterio,
        )
    else:
        novo_handoff = atual["handoff"]

    novas_provas = normalizar_provas(provas) if provas is not None else atual["provas"]
    agora = datetime.now().isoformat(timespec="seconds")

    with _conn_board(db_path) as c:
        c.execute(
            """
            UPDATE board_tarefas
            SET titulo = ?, dono = ?, status = ?, criterio_done = ?, handoff_json = ?, provas_json = ?, atualizado_em = ?
            WHERE id = ?
            """,
            (
                novo_titulo,
                novo_dono,
                novo_status,
                novo_criterio,
                json.dumps(novo_handoff, ensure_ascii=False),
                json.dumps(novas_provas, ensure_ascii=False),
                agora,
                card_id,
            ),
        )

    atualizado = obter_card_board(card_id, db_path=db_path)
    assert atualizado is not None
    return atualizado


def listar_board(
    status: str | None = None,
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Lista cards do Board ordenados por `atualizado_em DESC` (opcionalmente filtrando por status)."""
    filtro_st = normalizar_status_board(status) if (status or "").strip() else None
    with _conn_board(db_path) as c:
        if filtro_st:
            rows = c.execute(
                """
                SELECT id, titulo, dono, status, criterio_done, handoff_json, provas_json, tentativas_checker, criado_em, atualizado_em
                FROM board_tarefas
                WHERE status = ?
                ORDER BY atualizado_em DESC, rowid DESC
                """,
                (filtro_st,),
            ).fetchall()
        else:
            rows = c.execute(
                """
                SELECT id, titulo, dono, status, criterio_done, handoff_json, provas_json, tentativas_checker, criado_em, atualizado_em
                FROM board_tarefas
                ORDER BY atualizado_em DESC, rowid DESC
                """
            ).fetchall()
    return [_row_para_card(r) for r in rows]


def _prova_eh_substantiva(prova: str) -> bool:
    limpa = " ".join((prova or "").strip().lower().split())
    if len(limpa) < 8:
        return False
    return limpa not in _PROVAS_PLACEHOLDER


def _avaliar_deterministico_checker(card: dict[str, Any]) -> tuple[bool, dict[str, str]]:
    """Regra 'No proof, no done' deterministica rapida (sem chamadas de rede)."""
    provas = [p for p in (card.get("provas") or []) if _prova_eh_substantiva(str(p))]
    criterio = str(card.get("criterio_done") or "").strip()

    if not provas:
        return False, {
            "criterio_falho": criterio or "Evidencia objetiva de conclusao (No proof, no done)",
            "o_que_corrigir": (
                "Anexe provas concretas em `provas_json` (ex.: saida de comando/teste, caminho de arquivo gerado, "
                "URL validada ou resumo verificavel) compativeis com o criterio de Done."
            ),
            "o_que_nao_mudar": "Mantenha o objetivo principal do card e os artefatos ja validados.",
        }

    # Checagem leve de palavras-chave tecnicas explicitas no criterio_done
    texto_provas = " ".join(provas).lower()
    crit_low = criterio.lower()
    requisitos_explicitos = [
        kw for kw in ("pytest", "ruff", "pyright", "migration", "endpoint", "arquivo", "relatorio") if kw in crit_low
    ]
    if requisitos_explicitos:
        atendidos = [kw for kw in requisitos_explicitos if kw in texto_provas]
        # Aceita se pelo menos um requisito explicito aparece nas provas OU se ha evidencia numerica/caminho claro
        tem_evidencia_forte = bool(atendidos) or bool(
            re.search(r"(?:passed|0 errors|ok|\.[a-z0-9]{1,5}\b|https?://|/\w+)", texto_provas)
        )
        if not tem_evidencia_forte:
            faltando = ", ".join(requisitos_explicitos)
            return False, {
                "criterio_falho": f"Criterio exige evidencia relacionada a: {faltando}",
                "o_que_corrigir": f"Inclua prova concreta comprovando `{faltando}` (ex.: resultado do comando ou caminho gerado).",
                "o_que_nao_mudar": "Preserve as decisoes e artefatos ja registrados no handoff.",
            }

    return True, {
        "criterio_falho": "",
        "o_que_corrigir": "",
        "o_que_nao_mudar": "Todas as entregas comprovadas estao de acordo com o criterio de Done.",
    }


def _avaliar_com_llm_contexto_limpo(card: dict[str, Any]) -> tuple[bool, dict[str, str]]:
    """Avaliacao opcional via LLM em contexto 100% isolado do autor (recebe apenas handoff + provas)."""
    from pydantic_ai import Agent

    agente_checker = Agent(
        MODELO_CHECKER,
        instructions=(
            "Voce e o Agente Checker independente da Athena. Sua regra e 'No proof, no done'. "
            "Receba apenas titulo, criterio_done, handoff enxuto e provas. "
            "Responda EXCLUSIVAMENTE em JSON valido com as chaves: "
            '{"aprovado": true/false, "criterio_falho": "...", "o_que_corrigir": "...", "o_que_nao_mudar": "..."}.'
        ),
    )
    prompt = (
        f"Titulo: {card['titulo']}\n"
        f"Criterio de Done: {card['criterio_done'] or '(nao especificado)'}\n"
        f"Handoff Enxuto:\n{formatar_handoff_enxuto(card)}\n"
        f"Provas apresentadas:\n{json.dumps(card['provas'], ensure_ascii=False)}"
    )

    async def _run() -> str:
        async with asyncio.timeout(12.0):
            res = await agente_checker.run(prompt, retries=0)
        return str(getattr(res, "output", res) or "").strip()

    try:
        bruto = asyncio.run(_run())
        m = re.search(r"\{[\s\S]*\}", bruto)
        dados = json.loads(m.group(0) if m else bruto)
        if isinstance(dados, dict) and isinstance(dados.get("aprovado"), bool):
            return bool(dados["aprovado"]), {
                "criterio_falho": _limpar_str(dados.get("criterio_falho"), limite=240),
                "o_que_corrigir": _limpar_str(dados.get("o_que_corrigir"), limite=300),
                "o_que_nao_mudar": _limpar_str(dados.get("o_que_nao_mudar"), limite=240),
            }
    except Exception:
        pass
    return _avaliar_deterministico_checker(card)


def avaliar_card_checker(
    card_id: str,
    usar_llm: bool = False,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Agente Checker independente ('No proof, no done'): aprova para Done ou reprova/bloqueia com feedback estruturado."""
    card = obter_card_board(card_id, db_path=db_path)
    if card is None:
        return {"ok": False, "erro": f"Card '{card_id}' nao encontrado."}

    aprovado, feedback = _avaliar_deterministico_checker(card)
    if aprovado and usar_llm:
        aprovado, feedback = _avaliar_com_llm_contexto_limpo(card)

    agora = datetime.now().isoformat(timespec="seconds")
    handoff_atual = dict(card["handoff"])

    if aprovado:
        handoff_atual["next_gate"] = "Aprovado pelo Checker (Done)"
        with _conn_board(db_path) as c:
            c.execute(
                """
                UPDATE board_tarefas
                SET status = 'Done', handoff_json = ?, atualizado_em = ?
                WHERE id = ?
                """,
                (json.dumps(handoff_atual, ensure_ascii=False), agora, card_id),
            )
        with suppress(Exception):
            registrar_atividade(
                agente="Agente Checker",
                acao="aprovar_card_board",
                alvo=card["titulo"],
                status="sucesso",
                detalhes=f"Card {card_id} aprovado para Done com {len(card['provas'])} prova(s).",
                db_path=db_path,
            )
        atualizado = obter_card_board(card_id, db_path=db_path)
        return {
            "ok": True,
            "aprovado": True,
            "status_novo": "Done",
            "tentativas_checker": int(atualizado["tentativas_checker"]) if atualizado else int(card["tentativas_checker"]),
            "feedback": feedback,
            "card": atualizado,
        }

    novas_tentativas = int(card.get("tentativas_checker") or 0) + 1
    bloqueado = novas_tentativas >= MAX_TENTATIVAS_CHECKER
    status_novo = "Blocked" if bloqueado else "Working"

    open_q = list(handoff_atual.get("open_questions") or [])
    resumo_fb = f"[Checker #{novas_tentativas}] Falha: {feedback['criterio_falho']} | Corrigir: {feedback['o_que_corrigir']}"
    open_q.append(_limpar_str(resumo_fb, limite=240))
    handoff_atual["open_questions"] = open_q[-6:]
    handoff_atual["next_gate"] = (
        "Intervencao humana necessaria (2 reprovacoes no Checker)"
        if bloqueado
        else f"Corrigir e reenviar para Review: {feedback['o_que_corrigir'][:160]}"
    )

    with _conn_board(db_path) as c:
        c.execute(
            """
            UPDATE board_tarefas
            SET status = ?, tentativas_checker = ?, handoff_json = ?, atualizado_em = ?
            WHERE id = ?
            """,
            (status_novo, novas_tentativas, json.dumps(handoff_atual, ensure_ascii=False), agora, card_id),
        )

    with suppress(Exception):
        registrar_atividade(
            agente="Agente Checker",
            acao="bloquear_card_board" if bloqueado else "reprovar_card_board",
            alvo=card["titulo"],
            status="alerta_bloqueio" if bloqueado else "reprovado",
            detalhes=(
                f"Card {card_id} -> {status_novo} (tentativa {novas_tentativas}/{MAX_TENTATIVAS_CHECKER}). "
                f"Criterio falho: {feedback['criterio_falho']}"
            ),
            db_path=db_path,
        )

    atualizado = obter_card_board(card_id, db_path=db_path)
    return {
        "ok": True,
        "aprovado": False,
        "bloqueado": bloqueado,
        "status_novo": status_novo,
        "tentativas_checker": novas_tentativas,
        "feedback": feedback,
        "card": atualizado,
    }
