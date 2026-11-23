import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from pypdf import PdfReader

from eficiencia import compactar_saida_tool

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
                reader = PdfReader(str(pdf))
                for idx, pag in enumerate(reader.pages, start=1):
                    txt = (pag.extract_text() or "").strip()
                    if txt:
                        c.execute(
                            "INSERT INTO pdf_paginas (materia, arquivo, pagina, texto, mtime) VALUES (?, ?, ?, ?, ?)",
                            (materia, pdf.name, idx, txt, mtime),
                        )
                novos += 1
            except Exception:
                pass

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
    sincronizar_indice_pdfs()
    termos = [t.strip().lower() for t in consulta.split() if len(t.strip()) >= 3]
    if not termos:
        termos = [consulta.strip().lower()]

    with _conn() as c:
        if materia:
            rows = c.execute(
                "SELECT materia, arquivo, pagina, texto FROM pdf_paginas WHERE LOWER(materia) LIKE ?",
                (f"%{materia.lower()}%",),
            ).fetchall()
        else:
            rows = c.execute("SELECT materia, arquivo, pagina, texto FROM pdf_paginas").fetchall()

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