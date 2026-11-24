import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from logging_config import obter_logger

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "memoria.db"
ALEMBIC_INI_PATH = BASE_DIR / "alembic.ini"
ALEMBIC_DIR_PATH = BASE_DIR / "alembic"
CONVERSA_PADRAO_ID = "principal"
TITULO_CONVERSA_PADRAO = "Nova conversa"

_log = obter_logger("memoria")


def obter_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve o caminho do SQLite (parametro explicito > `ATHENA_DB_PATH` > `DB_PATH`)."""
    if db_path is not None:
        return Path(db_path).resolve()
    env_db = (os.getenv("ATHENA_DB_PATH") or "").strip()
    if env_db:
        return Path(env_db).resolve()
    return DB_PATH.resolve()


@contextmanager
def _conn(db_path: str | Path | None = None):
    alvo = obter_db_path(db_path)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(alvo, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _garantir_coluna_meta_mensagens(c: sqlite3.Connection) -> None:
    cols = {r[1] for r in c.execute("PRAGMA table_info(mensagens)").fetchall()}
    if not cols:
        return
    if "meta_json" not in cols:
        c.execute("ALTER TABLE mensagens ADD COLUMN meta_json TEXT DEFAULT '{}'")
    if "conversa_id" not in cols:
        c.execute("ALTER TABLE mensagens ADD COLUMN conversa_id TEXT NOT NULL DEFAULT 'principal'")


def _garantir_tabela_conversas(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS conversas (
            id TEXT PRIMARY KEY,
            titulo TEXT NOT NULL,
            agente_alvo TEXT NOT NULL DEFAULT 'athena',
            criado_em TEXT NOT NULL,
            atualizado_em TEXT NOT NULL
        )
        """
    )


def gerar_titulo_conversa(mensagem: str, max_chars: int = 45) -> str:
    """Gera um titulo curto e limpo a partir da primeira mensagem do usuario."""
    txt = str(mensagem or "").strip()
    txt = re.sub(r"^\[Anexos?(?:\s*\(\d+\))?:[^\]]*\]\s*", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"[#*`_>\[\]]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" .,:;-!?")
    if not txt:
        return TITULO_CONVERSA_PADRAO
    if len(txt) <= max_chars:
        return txt
    cortado = txt[: max(10, max_chars - 1)].rstrip()
    if " " in cortado and len(cortado) > 16:
        cortado = cortado.rsplit(" ", 1)[0].rstrip(" .,:;-")
    return f"{cortado}…"


def executar_migrations(db_path: str | Path | None = None) -> str:
    """Executa `alembic upgrade head` no banco SQLite de forma segura e idempotente.

    Preserva 100% dos dados existentes em bancos antigos e cria o schema completo em bancos novos.
    Retorna o identificador da revisao ativa registrada em `alembic_version`.
    """
    from alembic import command
    from alembic.config import Config

    alvo = obter_db_path(db_path)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    url_sqlite = f"sqlite:///{alvo.as_posix()}"

    cfg = Config(str(ALEMBIC_INI_PATH)) if ALEMBIC_INI_PATH.is_file() else Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR_PATH))
    cfg.set_main_option("sqlalchemy.url", url_sqlite)
    cfg.attributes["programmatic"] = True
    cfg.attributes["db_url"] = url_sqlite

    command.upgrade(cfg, "head")

    versao = ""
    with _conn(alvo) as c:
        row = c.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        if row:
            versao = str(row[0])

    _log.info("alembic_migrations_aplicadas", db=str(alvo), revisao=versao or "head")
    return versao


def init_db(db_path: str | Path | None = None):
    """Inicializa o banco aplicando as migrations Alembic (com fallback seguro se necessario)."""
    if ALEMBIC_DIR_PATH.is_dir():
        executar_migrations(db_path)
        return

    with _conn(db_path) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS mensagens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                conteudo TEXT NOT NULL,
                trilha_json TEXT DEFAULT '[]',
                criado_em TEXT NOT NULL,
                meta_json TEXT DEFAULT '{}'
            )
            """
        )
        _garantir_coluna_meta_mensagens(c)
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS fatos (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL,
                atualizado_em TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_hora TEXT NOT NULL,
                modelo TEXT NOT NULL,
                duracao_ms INTEGER NOT NULL,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                agentes_usados TEXT DEFAULT '',
                sucesso INTEGER NOT NULL DEFAULT 1
            )
            """
        )


def salvar_mensagem(
    role: str,
    conteudo: str,
    trilha: list[dict] | None = None,
    meta: dict | None = None,
    *,
    conversa_id: str = CONVERSA_PADRAO_ID,
    agente_alvo: str = "athena",
    db_path: str | Path | None = None,
):
    agora = datetime.now().isoformat(timespec="seconds")
    cid = (conversa_id or CONVERSA_PADRAO_ID).strip() or CONVERSA_PADRAO_ID
    agente = (agente_alvo or "athena").strip().lower() or "athena"
    meta_str = json.dumps(meta or {}, ensure_ascii=False)
    with _conn(db_path) as c:
        _garantir_coluna_meta_mensagens(c)
        _garantir_tabela_conversas(c)
        c.execute(
            "INSERT INTO mensagens (role, conteudo, trilha_json, criado_em, meta_json, conversa_id) VALUES (?, ?, ?, ?, ?, ?)",
            (role, conteudo, json.dumps(trilha or [], ensure_ascii=False), agora, meta_str, cid),
        )
        row_conv = c.execute("SELECT titulo FROM conversas WHERE id = ?", (cid,)).fetchone()
        if row_conv is None:
            titulo_init = gerar_titulo_conversa(conteudo) if role == "user" else TITULO_CONVERSA_PADRAO
            c.execute(
                "INSERT INTO conversas (id, titulo, agente_alvo, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?)",
                (cid, titulo_init, agente, agora, agora),
            )
        else:
            titulo_atual = str(row_conv[0] or "").strip()
            if role == "user" and (not titulo_atual or titulo_atual.lower() == TITULO_CONVERSA_PADRAO.lower()):
                novo_titulo = gerar_titulo_conversa(conteudo)
                c.execute(
                    "UPDATE conversas SET titulo = ?, atualizado_em = ? WHERE id = ?",
                    (novo_titulo, agora, cid),
                )
            else:
                c.execute(
                    "UPDATE conversas SET atualizado_em = ? WHERE id = ?",
                    (agora, cid),
                )


def listar_mensagens(
    limite: int = 30,
    *,
    conversa_id: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    with _conn(db_path) as c:
        _garantir_coluna_meta_mensagens(c)
        if conversa_id:
            rows = c.execute(
                "SELECT id, role, conteudo, trilha_json, criado_em, COALESCE(meta_json, '{}'), "
                "COALESCE(conversa_id, 'principal') "
                "FROM mensagens WHERE COALESCE(conversa_id, 'principal') = ? ORDER BY id DESC LIMIT ?",
                (conversa_id.strip(), limite),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, role, conteudo, trilha_json, criado_em, COALESCE(meta_json, '{}'), "
                "COALESCE(conversa_id, 'principal') "
                "FROM mensagens ORDER BY id DESC LIMIT ?",
                (limite,),
            ).fetchall()
    rows.reverse()
    out: list[dict] = []
    for r in rows:
        try:
            meta_obj = json.loads(r[5] or "{}")
            if not isinstance(meta_obj, dict):
                meta_obj = {}
        except Exception:
            meta_obj = {}
        out.append({
            "id": r[0],
            "role": r[1],
            "conteudo": r[2],
            "trilha": json.loads(r[3] or "[]"),
            "criado_em": r[4],
            "meta": meta_obj,
            "conversa_id": r[6] or CONVERSA_PADRAO_ID,
        })
    return out


def limpar_mensagens(*, conversa_id: str | None = None, db_path: str | Path | None = None):
    with _conn(db_path) as c:
        _garantir_coluna_meta_mensagens(c)
        existe_res = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resumos_conversa'"
        ).fetchone()
        if conversa_id:
            cid = conversa_id.strip()
            c.execute("DELETE FROM mensagens WHERE COALESCE(conversa_id, 'principal') = ?", (cid,))
            if existe_res:
                c.execute("DELETE FROM resumos_conversa WHERE conversa_id = ?", (cid,))
        else:
            c.execute("DELETE FROM mensagens")
            if existe_res:
                c.execute("DELETE FROM resumos_conversa")


def garantir_conversas_legadas(*, db_path: str | Path | None = None) -> int:
    """Auto-registra na tabela `conversas` qualquer conversa antiga que ja exista em `mensagens`."""
    inseridas = 0
    with _conn(db_path) as c:
        _garantir_coluna_meta_mensagens(c)
        _garantir_tabela_conversas(c)
        tem_msg = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mensagens'"
        ).fetchone()
        if not tem_msg:
            return 0
        grupos = c.execute(
            """
            SELECT COALESCE(m.conversa_id, 'principal') AS cid,
                   MIN(m.criado_em) AS prim,
                   MAX(m.criado_em) AS ult
            FROM mensagens m
            LEFT JOIN conversas cv ON cv.id = COALESCE(m.conversa_id, 'principal')
            WHERE cv.id IS NULL
            GROUP BY COALESCE(m.conversa_id, 'principal')
            """
        ).fetchall()
        for cid, criado_em, atualizado_em in grupos:
            primeira_user = c.execute(
                """
                SELECT conteudo FROM mensagens
                WHERE COALESCE(conversa_id, 'principal') = ? AND role = 'user'
                ORDER BY id ASC LIMIT 1
                """,
                (cid,),
            ).fetchone()
            titulo = (
                gerar_titulo_conversa(str(primeira_user[0]))
                if primeira_user and str(primeira_user[0] or "").strip()
                else ("Conversa principal" if cid == CONVERSA_PADRAO_ID else TITULO_CONVERSA_PADRAO)
            )
            agora = datetime.now().isoformat(timespec="seconds")
            c.execute(
                """
                INSERT OR IGNORE INTO conversas (id, titulo, agente_alvo, criado_em, atualizado_em)
                VALUES (?, ?, 'athena', ?, ?)
                """,
                (cid, titulo, criado_em or agora, atualizado_em or criado_em or agora),
            )
            inseridas += 1
    return inseridas


def criar_conversa(
    conversa_id: str | None = None,
    titulo: str = TITULO_CONVERSA_PADRAO,
    agente_alvo: str = "athena",
    *,
    db_path: str | Path | None = None,
) -> dict:
    """Cria (ou retorna existente) uma conversa na tabela `conversas`."""
    cid = (conversa_id or "").strip() or f"conv_{uuid.uuid4().hex[:12]}"
    tit = (titulo or "").strip()[:120] or TITULO_CONVERSA_PADRAO
    agente = (agente_alvo or "athena").strip().lower() or "athena"
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as c:
        _garantir_tabela_conversas(c)
        c.execute(
            """
            INSERT OR IGNORE INTO conversas (id, titulo, agente_alvo, criado_em, atualizado_em)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cid, tit, agente, agora, agora),
        )
        row = c.execute(
            "SELECT id, titulo, agente_alvo, criado_em, atualizado_em FROM conversas WHERE id = ?",
            (cid,),
        ).fetchone()
    assert row is not None
    return {
        "id": row[0],
        "titulo": row[1],
        "agente_alvo": row[2],
        "criado_em": row[3],
        "atualizado_em": row[4],
        "total_mensagens": 0,
    }


def obter_conversa(conversa_id: str, *, db_path: str | Path | None = None) -> dict | None:
    """Retorna os metadados de uma conversa (auto-registrando legadas se necessario)."""
    cid = (conversa_id or "").strip()
    if not cid:
        return None
    garantir_conversas_legadas(db_path=db_path)
    with _conn(db_path) as c:
        _garantir_tabela_conversas(c)
        _garantir_coluna_meta_mensagens(c)
        row = c.execute(
            """
            SELECT cv.id, cv.titulo, cv.agente_alvo, cv.criado_em, cv.atualizado_em,
                   (SELECT COUNT(*) FROM mensagens m WHERE COALESCE(m.conversa_id, 'principal') = cv.id)
            FROM conversas cv
            WHERE cv.id = ?
            """,
            (cid,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "titulo": row[1],
        "agente_alvo": row[2],
        "criado_em": row[3],
        "atualizado_em": row[4],
        "total_mensagens": int(row[5] or 0),
    }


def listar_conversas(limite: int = 50, *, db_path: str | Path | None = None) -> list[dict]:
    """Lista conversas recentes ordenadas por `atualizado_em DESC`, incluindo conversas legadas."""
    garantir_conversas_legadas(db_path=db_path)
    lim = max(1, min(int(limite or 50), 200))
    with _conn(db_path) as c:
        _garantir_tabela_conversas(c)
        _garantir_coluna_meta_mensagens(c)
        rows = c.execute(
            """
            SELECT cv.id, cv.titulo, cv.agente_alvo, cv.criado_em, cv.atualizado_em,
                   (SELECT COUNT(*) FROM mensagens m WHERE COALESCE(m.conversa_id, 'principal') = cv.id)
            FROM conversas cv
            ORDER BY cv.atualizado_em DESC, cv.rowid DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "titulo": r[1],
            "agente_alvo": r[2],
            "criado_em": r[3],
            "atualizado_em": r[4],
            "total_mensagens": int(r[5] or 0),
        }
        for r in rows
    ]


def atualizar_titulo_conversa(
    conversa_id: str,
    titulo: str,
    *,
    db_path: str | Path | None = None,
) -> dict | None:
    """Atualiza o titulo de uma conversa existente."""
    cid = (conversa_id or "").strip()
    tit = re.sub(r"\s+", " ", (titulo or "").strip())[:120]
    if not cid or not tit:
        return None
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as c:
        _garantir_tabela_conversas(c)
        cur = c.execute(
            "UPDATE conversas SET titulo = ?, atualizado_em = ? WHERE id = ?",
            (tit, agora, cid),
        )
        if cur.rowcount == 0:
            return None
    return obter_conversa(cid, db_path=db_path)


def deletar_conversa(conversa_id: str, *, db_path: str | Path | None = None) -> bool:
    """Apaga a conversa, suas mensagens e eventual resumo compactado em `resumos_conversa`."""
    cid = (conversa_id or "").strip()
    if not cid:
        return False
    with _conn(db_path) as c:
        _garantir_tabela_conversas(c)
        _garantir_coluna_meta_mensagens(c)
        cur_m = c.execute("DELETE FROM mensagens WHERE COALESCE(conversa_id, 'principal') = ?", (cid,))
        existe_res = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resumos_conversa'"
        ).fetchone()
        if existe_res:
            c.execute("DELETE FROM resumos_conversa WHERE conversa_id = ?", (cid,))
        cur_c = c.execute("DELETE FROM conversas WHERE id = ?", (cid,))
        return (cur_c.rowcount > 0) or (cur_m.rowcount > 0)


def guardar_fato(chave: str, valor: str, *, db_path: str | Path | None = None):
    from guardrails import mascarar_segredos_e_pii

    valor_seguro, _ = mascarar_segredos_e_pii(valor.strip())
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as c:
        c.execute(
            "INSERT INTO fatos (chave, valor, atualizado_em) VALUES (?, ?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, atualizado_em=excluded.atualizado_em",
            (chave.strip().lower(), valor_seguro, agora),
        )


def listar_fatos(*, db_path: str | Path | None = None) -> list[dict]:
    with _conn(db_path) as c:
        rows = c.execute("SELECT chave, valor, atualizado_em FROM fatos ORDER BY atualizado_em DESC").fetchall()
    return [{"chave": r[0], "valor": r[1], "atualizado_em": r[2]} for r in rows]


def apagar_fato(chave: str, *, db_path: str | Path | None = None):
    with _conn(db_path) as c:
        c.execute("DELETE FROM fatos WHERE chave = ?", (chave,))


def montar_contexto_prompt(
    mensagem_atual: str,
    ultimas: int = 5,
    max_chars_msg: int = 400,
    *,
    resumo_compactado: str = "",
    historico_override: list[dict] | None = None,
) -> str:
    fatos = sorted(listar_fatos(), key=lambda f: str(f.get("chave", "")).lower())
    hist = list(historico_override) if historico_override is not None else listar_mensagens(limite=ultimas)

    partes = []
    if fatos:
        bloco_fatos = "\n".join(f"- {f['chave']}: {f['valor']}" for f in fatos)
        partes.append(f"[MEMORIA DE LONGO PRAZO DO USUARIO]\n{bloco_fatos}")
    if (resumo_compactado or "").strip():
        partes.append(f"[RESUMO COMPACTADO DA CONVERSA ANTERIOR]\n{resumo_compactado.strip()}")
    if hist:
        linhas_hist = []
        for m in hist:
            texto = (m["conteudo"] or "").strip()
            if len(texto) > max_chars_msg:
                texto = texto[:max_chars_msg].rstrip() + "..."
            papel = "Usuario" if m["role"] == "user" else "Athena"
            linhas_hist.append(f"{papel}: {texto}")
        bloco_hist = "\n".join(linhas_hist)
        partes.append(f"[ULTIMAS MENSAGENS DA CONVERSA]\n{bloco_hist}")
    partes.append(f"[PEDIDO ATUAL DO USUARIO]\n{mensagem_atual}")
    return "\n\n".join(partes)


def _garantir_tabela_telemetria(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT NOT NULL,
            modelo TEXT NOT NULL,
            duracao_ms INTEGER NOT NULL,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            agentes_usados TEXT DEFAULT '',
            sucesso INTEGER NOT NULL DEFAULT 1
        )
        """
    )


def registrar_telemetria(
    modelo: str,
    duracao_ms: int,
    tokens_in: int = 0,
    tokens_out: int = 0,
    agentes_usados: str = "",
    sucesso: bool = True,
) -> int:
    """Registra uma execucao do chat na tabela `telemetria` do memoria.db."""
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        _garantir_tabela_telemetria(c)
        cur = c.execute(
            """
            INSERT INTO telemetria
                (data_hora, modelo, duracao_ms, tokens_in, tokens_out, agentes_usados, sucesso)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agora,
                (modelo or "").strip() or "desconhecido",
                max(0, int(duracao_ms)),
                max(0, int(tokens_in or 0)),
                max(0, int(tokens_out or 0)),
                (agentes_usados or "").strip()[:300],
                1 if sucesso else 0,
            ),
        )
        return int(cur.lastrowid or 0)


def obter_resumo_telemetria(limite: int = 15) -> dict:
    """Retorna metricas consolidadas de telemetria e as ultimas N execucoes."""
    hoje_prefix = datetime.now().strftime("%Y-%m-%d")
    lim = max(1, min(int(limite), 100))

    with _conn() as c:
        _garantir_tabela_telemetria(c)
        tot_row = c.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(AVG(duracao_ms), 0),
                COALESCE(SUM(CASE WHEN sucesso = 1 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(tokens_in), 0),
                COALESCE(SUM(tokens_out), 0)
            FROM telemetria
            """
        ).fetchone()
        hoje_row = c.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(tokens_in), 0),
                COALESCE(SUM(tokens_out), 0)
            FROM telemetria
            WHERE data_hora LIKE ?
            """,
            (f"{hoje_prefix}%",),
        ).fetchone()
        rows = c.execute(
            """
            SELECT id, data_hora, modelo, duracao_ms, tokens_in, tokens_out, agentes_usados, sucesso
            FROM telemetria
            ORDER BY id DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()

    total_exec = int(tot_row[0] or 0)
    tempo_medio_ms = int(round(float(tot_row[1] or 0.0)))
    sucessos = int(tot_row[2] or 0)
    tokens_total_in = int(tot_row[3] or 0)
    tokens_total_out = int(tot_row[4] or 0)
    taxa_sucesso = round((sucessos / total_exec) * 100.0, 1) if total_exec > 0 else 100.0

    execucoes_hoje = int(hoje_row[0] or 0)
    tokens_hoje_in = int(hoje_row[1] or 0)
    tokens_hoje_out = int(hoje_row[2] or 0)

    ultimas = [
        {
            "id": int(r[0]),
            "data_hora": r[1],
            "modelo": r[2],
            "duracao_ms": int(r[3]),
            "tokens_in": int(r[4]),
            "tokens_out": int(r[5]),
            "tokens_total": int(r[4]) + int(r[5]),
            "agentes_usados": r[6] or "",
            "sucesso": bool(r[7]),
        }
        for r in rows
    ]

    return {
        "total_execucoes": total_exec,
        "execucoes_hoje": execucoes_hoje,
        "tempo_medio_ms": tempo_medio_ms,
        "taxa_sucesso": taxa_sucesso,
        "tokens_in": tokens_total_in,
        "tokens_out": tokens_total_out,
        "tokens_hoje": tokens_hoje_in + tokens_hoje_out,
        "tokens_hoje_in": tokens_hoje_in,
        "tokens_hoje_out": tokens_hoje_out,
        "tokens_hoje_total": tokens_hoje_in + tokens_hoje_out,
        "tokens_total": tokens_total_in + tokens_total_out,
        "ultimas_execucoes": ultimas,
    }