import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from logging_config import obter_logger

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "memoria.db"
ALEMBIC_INI_PATH = BASE_DIR / "alembic.ini"
ALEMBIC_DIR_PATH = BASE_DIR / "alembic"

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
    if "meta_json" not in cols:
        c.execute("ALTER TABLE mensagens ADD COLUMN meta_json TEXT DEFAULT '{}'")


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
):
    agora = datetime.now().isoformat(timespec="seconds")
    meta_str = json.dumps(meta or {}, ensure_ascii=False)
    with _conn() as c:
        _garantir_coluna_meta_mensagens(c)
        c.execute(
            "INSERT INTO mensagens (role, conteudo, trilha_json, criado_em, meta_json) VALUES (?, ?, ?, ?, ?)",
            (role, conteudo, json.dumps(trilha or [], ensure_ascii=False), agora, meta_str),
        )


def listar_mensagens(limite: int = 30, *, db_path: str | Path | None = None) -> list[dict]:
    with _conn(db_path) as c:
        _garantir_coluna_meta_mensagens(c)
        rows = c.execute(
            "SELECT id, role, conteudo, trilha_json, criado_em, COALESCE(meta_json, '{}') FROM mensagens ORDER BY id DESC LIMIT ?",
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
        })
    return out


def limpar_mensagens(*, db_path: str | Path | None = None):
    with _conn(db_path) as c:
        c.execute("DELETE FROM mensagens")
        existe_res = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resumos_conversa'"
        ).fetchone()
        if existe_res:
            c.execute("DELETE FROM resumos_conversa")


def guardar_fato(chave: str, valor: str, *, db_path: str | Path | None = None):
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as c:
        c.execute(
            "INSERT INTO fatos (chave, valor, atualizado_em) VALUES (?, ?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, atualizado_em=excluded.atualizado_em",
            (chave.strip().lower(), valor.strip(), agora),
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