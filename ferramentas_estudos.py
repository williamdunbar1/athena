import importlib
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from eficiencia import compactar_saida_tool
from guardrails import sanitizar_conteudo_externo

IDIOMAS_YOUTUBE_PADRAO: tuple[str, ...] = ("pt", "pt-BR", "en")
MAX_CHARS_TRANSCRICAO_YOUTUBE = 12000
_RE_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

PASTA_FACULDADE = Path(
    os.getenv("PASTA_FACULDADE", "").strip() or (Path.home() / "Documents" / "agente-faculdade")
)
PASTA_PDFS = PASTA_FACULDADE / "pdfs_faculdade"
DB_PATH = Path(__file__).resolve().parent / "memoria.db"

NOMES_LEGIVEIS_MATERIAS: dict[str, str] = {
    "academia_de_futuros_profissionais": "Academia de Futuros Profissionais",
    "calculo_algebra_linear": "Cálculo e Álgebra Linear",
    "fisica_pratica": "Física e Mecânica dos Sólidos (Prática)",
    "fisica_teorica": "Física e Mecânica dos Sólidos (Teórica)",
    "fisica_e_mecanica_dos_solidos": "Física e Mecânica dos Sólidos",
    "geral": "Geral",
}

_PREPOSICOES_PT: frozenset[str] = frozenset({
    "de", "da", "do", "dos", "das", "e", "em", "na", "no", "nas", "nos", "para", "com", "a", "o", "as", "os",
})


def formatar_nome_materia(slug: str) -> str:
    """Converte slugs `snake_case` de pastas de materias em nomes legiveis em portugues."""
    bruto = (slug or "").strip()
    if not bruto:
        return "Geral"
    chave = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", bruto).encode("ascii", "ignore").decode("ascii").lower()).strip("_")
    if chave in NOMES_LEGIVEIS_MATERIAS:
        return NOMES_LEGIVEIS_MATERIAS[chave]
    if "_" not in bruto and any(ch.isupper() for ch in bruto):
        return bruto
    palavras = [p for p in re.split(r"[_\-\s]+", bruto) if p]
    if not palavras:
        return bruto
    saida: list[str] = []
    for idx, p in enumerate(palavras):
        low = p.lower()
        if idx > 0 and low in _PREPOSICOES_PT:
            saida.append(low)
        else:
            saida.append(low.capitalize())
    return " ".join(saida)


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS pdf_paginas (
                materia TEXT NOT NULL,
                arquivo TEXT NOT NULL,
                pagina INTEGER NOT NULL,
                texto TEXT NOT NULL,
                mtime REAL NOT NULL,
                PRIMARY KEY (materia, arquivo, pagina)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS provas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                materia TEXT NOT NULL,
                titulo TEXT NOT NULL,
                data_iso TEXT NOT NULL,
                detalhes TEXT DEFAULT '',
                UNIQUE(materia, titulo, data_iso)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS progresso_aulas (
                materia TEXT NOT NULL,
                arquivo TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendente',
                atualizado_em TEXT NOT NULL,
                PRIMARY KEY (materia, arquivo)
            )
            """
        )
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def sincronizar_indice_pdfs() -> int:
    if not PASTA_PDFS.exists():
        return 0
    novos = 0
    atuais: set[tuple[str, str]] = set()
    with _conn() as c:
        for pdf in sorted(PASTA_PDFS.rglob("*.pdf")):
            materia = pdf.parent.name if pdf.parent != PASTA_PDFS else "geral"
            atuais.add((materia, pdf.name))
            mtime = pdf.stat().st_mtime
            row = c.execute(
                "SELECT mtime FROM pdf_paginas WHERE materia=? AND arquivo=? LIMIT 1",
                (materia, pdf.name),
            ).fetchone()
            if row and abs(row[0] - mtime) < 1.0:
                continue
            c.execute("DELETE FROM pdf_paginas WHERE materia=? AND arquivo=?", (materia, pdf.name))
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(pdf))
                inseridas = 0
                for idx, pag in enumerate(reader.pages, start=1):
                    txt = (pag.extract_text() or "").strip()
                    if txt:
                        c.execute(
                            "INSERT INTO pdf_paginas (materia, arquivo, pagina, texto, mtime) VALUES (?, ?, ?, ?, ?)",
                            (materia, pdf.name, idx, txt, mtime),
                        )
                        inseridas += 1
                if inseridas == 0:
                    # Registra sentinela pagina=0 para nao re-parsear PDFs escaneados sem texto em toda chamada
                    c.execute(
                        "INSERT INTO pdf_paginas (materia, arquivo, pagina, texto, mtime) VALUES (?, ?, 0, '', ?)",
                        (materia, pdf.name, mtime),
                    )
                novos += 1
            except Exception:
                c.execute(
                    "INSERT INTO pdf_paginas (materia, arquivo, pagina, texto, mtime) VALUES (?, ?, 0, '', ?)",
                    (materia, pdf.name, mtime),
                )

        # Remove do indice SQLite arquivos/pastas que nao existem mais em pdfs_faculdade
        existentes_db = c.execute("SELECT DISTINCT materia, arquivo FROM pdf_paginas").fetchall()
        for mat_db, arq_db in existentes_db:
            if (mat_db, arq_db) not in atuais:
                c.execute("DELETE FROM pdf_paginas WHERE materia=? AND arquivo=?", (mat_db, arq_db))
    return novos


def listar_materias_e_aulas() -> str:
    sincronizar_indice_pdfs()
    if not PASTA_PDFS.exists():
        return f"Pasta de PDFs nao encontrada em: {PASTA_PDFS}"
    por_mat: dict[str, list[str]] = {}
    for pdf in sorted(PASTA_PDFS.rglob("*.pdf")):
        mat = pdf.parent.name if pdf.parent != PASTA_PDFS else "geral"
        por_mat.setdefault(mat, []).append(pdf.name)
    if not por_mat:
        return "Nenhum PDF encontrado em pdfs_faculdade/."

    with _conn() as c:
        status_map = {
            (r[0], r[1]): r[2]
            for r in c.execute("SELECT materia, arquivo, status FROM progresso_aulas").fetchall()
        }

    linhas = []
    for mat, arqs in por_mat.items():
        nome_legivel = formatar_nome_materia(mat)
        linhas.append(f"\nMateria: {nome_legivel} [{mat}] ({len(arqs)} PDFs)")
        for a in arqs:
            st = status_map.get((mat, a), "pendente")
            linhas.append(f"  - [{st}] {a}")
    return compactar_saida_tool("\n".join(linhas))


def pesquisar_pdfs(consulta: str, materia: str = "", limite: int = 8) -> str:
    consulta_limpa = (consulta or "").strip()
    if not consulta_limpa:
        return "Informe um termo para pesquisar nos PDFs da faculdade."
    sincronizar_indice_pdfs()
    termos = [t.strip().lower() for t in consulta_limpa.split() if len(t.strip()) >= 3]
    if not termos:
        termos = [consulta_limpa.lower()]

    with _conn() as c:
        if materia:
            rows = c.execute(
                "SELECT materia, arquivo, pagina, texto FROM pdf_paginas WHERE pagina > 0 AND LOWER(materia) LIKE ?",
                (f"%{materia.lower()}%",),
            ).fetchall()
        else:
            rows = c.execute("SELECT materia, arquivo, pagina, texto FROM pdf_paginas WHERE pagina > 0").fetchall()

    ranqueados = []
    for mat, arq, pag, txt in rows:
        low = (arq + " " + txt).lower()
        score = sum(low.count(t) for t in termos)
        if score > 0:
            ranqueados.append((score, mat, arq, pag, txt))

    ranqueados.sort(key=lambda x: x[0], reverse=True)
    if not ranqueados:
        return f"Nenhum trecho encontrado para '{consulta}'."

    saida = []
    for _score, mat, arq, pag, txt in ranqueados[:limite]:
        saida.append(f"[{mat} | {arq}, pag. {pag}]\n{txt[:750]}")
    return compactar_saida_tool("\n\n---\n\n".join(saida))


def ler_pdf_paginas(trecho_nome: str, pagina_inicial: int = 1, qtd_paginas: int = 5) -> str:
    sincronizar_indice_pdfs()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT materia, arquivo, pagina, texto
            FROM pdf_paginas
            WHERE LOWER(arquivo) LIKE ? AND pagina >= ? AND pagina < ?
            ORDER BY arquivo, pagina
            """,
            (f"%{trecho_nome.lower()}%", pagina_inicial, pagina_inicial + qtd_paginas),
        ).fetchall()
    if not rows:
        return f"Nenhuma pagina encontrada para arquivo contendo '{trecho_nome}'."
    return compactar_saida_tool("\n\n".join(f"[{m} | {a}, pag. {p}]\n{t[:1400]}" for m, a, p, t in rows))


def salvar_prova(materia: str, titulo: str, data_iso: str, detalhes: str = "") -> str:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO provas (materia, titulo, data_iso, detalhes) VALUES (?, ?, ?, ?)",
            (materia.strip(), titulo.strip(), data_iso.strip(), detalhes.strip()),
        )
    return f"Prova salva: [{data_iso}] {materia} - {titulo}"


def listar_provas() -> str:
    hoje = date.today()
    with _conn() as c:
        rows = c.execute("SELECT materia, titulo, data_iso, detalhes FROM provas ORDER BY data_iso ASC").fetchall()
    if not rows:
        return "Nenhuma prova cadastrada ainda."
    linhas = []
    for mat, tit, dt, det in rows:
        try:
            dias = (date.fromisoformat(dt) - hoje).days
            tag = f"em {dias} dia(s)" if dias >= 0 else f"passou ha {-dias} dia(s)"
        except ValueError:
            tag = "sem data exata"
        extra = f" | {det}" if det else ""
        linhas.append(f"- [{dt} ({tag})] {mat}: {tit}{extra}")
    return compactar_saida_tool("\n".join(linhas))


def marcar_progresso(materia: str, arquivo: str, status: str) -> str:
    st = status.strip().lower()
    if st not in ("estudada", "revisar", "pendente"):
        return "Status invalido. Use: estudada, revisar ou pendente."
    agora = datetime.now().isoformat(timespec="minutes")
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO progresso_aulas (materia, arquivo, status, atualizado_em) VALUES (?, ?, ?, ?)",
            (materia.strip(), arquivo.strip(), st, agora),
        )
    return f"Progresso atualizado: {materia}/{arquivo} -> {st}"


def extrair_video_id_youtube(url_ou_id: str) -> str | None:
    """Extrai o `video_id` (11 chars) de URLs do YouTube (`watch?v=`, `youtu.be/`, `shorts/`, `embed/`, `live/`) ou ID puro."""
    bruto = (url_ou_id or "").strip()
    if not bruto:
        return None

    if _RE_YOUTUBE_ID.match(bruto):
        return bruto

    cand_url = bruto if "://" in bruto else f"https://{bruto}"
    parsed = urlparse(cand_url)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("music.")
    partes = [p for p in parsed.path.split("/") if p]

    candidato: str | None = None
    if host == "youtu.be" and partes:
        candidato = partes[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            candidato = qs["v"][0]
        elif len(partes) >= 2 and partes[0] in ("shorts", "embed", "live", "v"):
            candidato = partes[1]

    if candidato and _RE_YOUTUBE_ID.match(candidato):
        return candidato
    return None


def _extrair_texto_itens_transcricao(itens) -> str:
    partes: list[str] = []
    for item in itens or []:
        txt = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        limpo = " ".join(str(txt or "").split())
        if limpo:
            partes.append(limpo)
    return " ".join(partes).strip()


def extrair_transcricao_youtube(
    url_ou_id: str,
    idiomas: tuple[str, ...] = IDIOMAS_YOUTUBE_PADRAO,
    max_chars: int = MAX_CHARS_TRANSCRICAO_YOUTUBE,
) -> str:
    """Extrai a transcricao/legenda de uma videoaula do YouTube (lazy import `youtube-transcript-api`).

    Prioriza `['pt', 'pt-BR', 'en']` (incluindo legendas geradas automaticamente), sanitiza com
    `guardrails.sanitizar_conteudo_externo` e compacta com teto padrao de 12.000 chars.
    """
    video_id = extrair_video_id_youtube(url_ou_id)
    if not video_id:
        return (
            "URL ou ID do YouTube invalido. Informe um link como "
            "`https://www.youtube.com/watch?v=...`, `https://youtu.be/...` ou `youtube.com/shorts/...`."
        )

    try:
        mod_yt = importlib.import_module("youtube_transcript_api")
        api_cls = getattr(mod_yt, "YouTubeTranscriptApi", None)
        if api_cls is None:
            raise ImportError("YouTubeTranscriptApi indisponivel.")
    except Exception:
        return (
            "Plugin opcional 'youtube-transcript-api' nao esta instalado neste ambiente. "
            "Instale com `uv sync --extra estudos` (ou `uv sync --extra web`)."
        )

    langs = list(idiomas or IDIOMAS_YOUTUBE_PADRAO)
    itens = None
    ultimo_erro: Exception | None = None

    # Suporte nativo a youtube-transcript-api v1.x (instancia .fetch/.list) e v0.6.x (metodos de classe)
    try:
        if hasattr(api_cls, "fetch"):
            api = api_cls()
            try:
                itens = api.fetch(video_id, languages=langs)
            except Exception as exc_fetch:
                ultimo_erro = exc_fetch
                if hasattr(api, "list"):
                    lista_tr = api.list(video_id)
                    escolhida: Any = None
                    for metodo in ("find_transcript", "find_generated_transcript"):
                        fn_find = getattr(lista_tr, metodo, None)
                        if callable(fn_find):
                            try:
                                escolhida = fn_find(langs)
                                break
                            except Exception:
                                continue
                    if escolhida is None:
                        for tr in lista_tr:
                            escolhida = tr
                            break
                    if escolhida is not None:
                        itens = escolhida.fetch()
        elif hasattr(api_cls, "get_transcript"):
            try:
                itens = api_cls.get_transcript(video_id, languages=langs)
            except Exception as exc_get:
                ultimo_erro = exc_get
                if hasattr(api_cls, "list_transcripts"):
                    lista_tr = api_cls.list_transcripts(video_id)
                    escolhida: Any = None
                    for metodo in ("find_transcript", "find_generated_transcript"):
                        fn_find = getattr(lista_tr, metodo, None)
                        if callable(fn_find):
                            try:
                                escolhida = fn_find(langs)
                                break
                            except Exception:
                                continue
                    if escolhida is None:
                        for tr in lista_tr:
                            escolhida = tr
                            break
                    if escolhida is not None:
                        itens = escolhida.fetch()
    except Exception as exc:
        ultimo_erro = exc

    texto_extraido = _extrair_texto_itens_transcricao(itens)
    if not texto_extraido:
        det = f" ({type(ultimo_erro).__name__}: {ultimo_erro})" if ultimo_erro else ""
        return f"Nao foi possivel obter legendas/transcricao para o video `{video_id}`{det}."

    url_canonica = f"https://www.youtube.com/watch?v={video_id}"
    bruto = (
        f"[Transcricao de Videoaula do YouTube | ID: {video_id} | URL: {url_canonica}]\n\n"
        f"{texto_extraido}"
    )
    sanitizado, _susp, _pads = sanitizar_conteudo_externo(bruto, origem=f"youtube:{video_id}")
    return compactar_saida_tool(sanitizado, max_chars=max(500, int(max_chars or MAX_CHARS_TRANSCRICAO_YOUTUBE)))