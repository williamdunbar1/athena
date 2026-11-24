"""Busca Hibrida Persistente (Densa + Esparsa BM25 + RRF + Reranker) com Qdrant e FastEmbed.

Indexa de forma incremental (controlando mtime no memoria.db):
- PDFs locais em `PASTA_FACULDADE/pdfs_faculdade` (padrao `~/Documents/agente-faculdade/pdfs_faculdade`)
- Notas Markdown (.md) do Obsidian Vault (OBSIDIAN_VAULT)

Schema hibrido na collection `conhecimento_athena`:
- Vetor denso (`dense`): `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (COSINE)
- Vetor esparso (`bm25`): `Qdrant/bm25` (Modifier.IDF)
- Fusao: `Prefetch(dense) + Prefetch(bm25) -> FusionQuery(Fusion.RRF)`
- Reranker local: `TextCrossEncoder` (`Xenova/ms-marco-MiniLM-L-6-v2`, fallback automatico por score RRF).
"""

from __future__ import annotations

import atexit
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eficiencia import compactar_saida_tool
from ferramentas_estudos import PASTA_PDFS, formatar_nome_materia
from ferramentas_obsidian import _iterar_notas_md, inicializar_vault

if TYPE_CHECKING:
    from qdrant_client import QdrantClient, models

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "memoria.db"
QDRANT_PATH = BASE_DIR / "qdrant_db"

COLECAO_ATHENA = "conhecimento_athena"
VETOR_DENSO = "dense"
VETOR_ESPARSO = "bm25"

MODELO_EMBEDDING = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODELO_ESPARSO = "Qdrant/bm25"
MODELO_RERANKER = (
    os.getenv("ATHENA_RERANKER_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2").strip()
    or "Xenova/ms-marco-MiniLM-L-6-v2"
)
DIMENSAO_DENSA_PADRAO = 384

CHUNK_TAMANHO = 800
CHUNK_SOBREPOSICAO = 150
LOTE_UPSERT = 64
CANDIDATOS_PREFETCH_PADRAO = 15

_QDRANT_LOCK = threading.RLock()
_CLIENT: QdrantClient | None = None
_DENSE_MODEL = None
_SPARSE_MODEL = None
_RERANKER_MODEL = None
_RERANKER_INDISPONIVEL = False


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_arquivos_indexados (
                chave_arquivo TEXT PRIMARY KEY,
                fonte TEXT NOT NULL,
                mtime REAL NOT NULL,
                chunks INTEGER NOT NULL DEFAULT 0,
                atualizado_em TEXT NOT NULL
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


def _fechar_cliente() -> None:
    global _CLIENT
    with _QDRANT_LOCK:
        if _CLIENT is not None:
            with suppress(Exception):
                _CLIENT.close()
            _CLIENT = None


atexit.register(_fechar_cliente)


def _obter_modelo_denso():
    global _DENSE_MODEL
    if _DENSE_MODEL is None:
        from fastembed import TextEmbedding

        _DENSE_MODEL = TextEmbedding(model_name=MODELO_EMBEDDING)
    return _DENSE_MODEL


def _obter_modelo_esparso():
    global _SPARSE_MODEL
    if _SPARSE_MODEL is None:
        from fastembed import SparseTextEmbedding

        _SPARSE_MODEL = SparseTextEmbedding(model_name=MODELO_ESPARSO)
    return _SPARSE_MODEL


def _obter_modelo_reranker():
    """Carrega o TextCrossEncoder sob demanda; retorna None (fallback RRF) se nao puder carregar."""
    global _RERANKER_MODEL, _RERANKER_INDISPONIVEL
    if _RERANKER_INDISPONIVEL:
        return None
    if _RERANKER_MODEL is None:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            _RERANKER_MODEL = TextCrossEncoder(model_name=MODELO_RERANKER)
        except Exception:
            _RERANKER_INDISPONIVEL = True
            return None
    return _RERANKER_MODEL


def _dimensao_densa(client: QdrantClient) -> int:
    try:
        return int(client.get_embedding_size(MODELO_EMBEDDING))
    except Exception:
        return DIMENSAO_DENSA_PADRAO


def _colecao_tem_schema_hibrido(client: QdrantClient) -> bool:
    """Verifica se a collection existente ja possui vetores nomeados `dense` + `bm25`."""
    if not client.collection_exists(COLECAO_ATHENA):
        return False
    try:
        info = client.get_collection(COLECAO_ATHENA)
        params = info.config.params
        vetores = getattr(params, "vectors", None)
        esparsos = getattr(params, "sparse_vectors", None)
        tem_denso = isinstance(vetores, dict) and VETOR_DENSO in vetores
        tem_esparso = isinstance(esparsos, dict) and VETOR_ESPARSO in esparsos
        return bool(tem_denso and tem_esparso)
    except Exception:
        return False


def _criar_colecao_hibrida(client: QdrantClient, dimensao: int | None = None) -> None:
    from qdrant_client import models

    dim = int(dimensao) if dimensao else _dimensao_densa(client)
    client.create_collection(
        collection_name=COLECAO_ATHENA,
        vectors_config={
            VETOR_DENSO: models.VectorParams(
                size=dim,
                distance=models.Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            VETOR_ESPARSO: models.SparseVectorParams(
                modifier=models.Modifier.IDF,
            ),
        },
    )


def _garantir_colecao(client: QdrantClient, *, forcar_migracao: bool = False) -> bool:
    """Garante collection hibrida (`dense` + `bm25`).

    Retorna True se a collection foi recriada/migrada a partir do schema antigo (sinalizando reindexacao completa).
    """
    if not client.collection_exists(COLECAO_ATHENA):
        _criar_colecao_hibrida(client)
        return False

    if _colecao_tem_schema_hibrido(client):
        return False

    if forcar_migracao:
        client.delete_collection(collection_name=COLECAO_ATHENA)
        _criar_colecao_hibrida(client)
        with _conn() as c:
            c.execute("DELETE FROM rag_arquivos_indexados")
        return True

    return False


def _obter_cliente(*, forcar_migracao: bool = False) -> tuple[QdrantClient, bool]:
    global _CLIENT
    with _QDRANT_LOCK:
        if _CLIENT is None:
            from qdrant_client import QdrantClient

            QDRANT_PATH.mkdir(parents=True, exist_ok=True)
            _CLIENT = QdrantClient(path=str(QDRANT_PATH))
        migrada = _garantir_colecao(_CLIENT, forcar_migracao=forcar_migracao)
        return _CLIENT, migrada


def _para_sparse_vector(emb) -> models.SparseVector:
    from qdrant_client import models

    if isinstance(emb, models.SparseVector):
        return emb
    indices = emb.indices.tolist() if hasattr(emb.indices, "tolist") else list(emb.indices)
    values = emb.values.tolist() if hasattr(emb.values, "tolist") else list(emb.values)
    return models.SparseVector(
        indices=[int(i) for i in indices],
        values=[float(v) for v in values],
    )


def _para_dense_vector(emb) -> list[float]:
    if hasattr(emb, "tolist"):
        return [float(x) for x in emb.tolist()]
    return [float(x) for x in emb]


def _gerar_vetores_documentos(textos: list[str]) -> tuple[list[list[float]], list[models.SparseVector]]:
    if not textos:
        return [], []
    dense_model = _obter_modelo_denso()
    sparse_model = _obter_modelo_esparso()
    densos = [_para_dense_vector(v) for v in dense_model.embed(textos)]
    esparsos = [_para_sparse_vector(v) for v in sparse_model.embed(textos)]
    return densos, esparsos


def _gerar_vetores_consulta(consulta: str) -> tuple[list[float], models.SparseVector]:
    dense_model = _obter_modelo_denso()
    sparse_model = _obter_modelo_esparso()
    denso = _para_dense_vector(next(iter(dense_model.query_embed(consulta))))
    esparso = _para_sparse_vector(next(iter(sparse_model.query_embed(consulta))))
    return denso, esparso


def _reranquear_candidatos(
    consulta: str,
    pontos: list,
    top_k: int,
) -> tuple[list[tuple[Any, float, str]], bool]:
    """Aplica TextCrossEncoder nos candidatos RRF; se indisponivel, mantem a ordem/score RRF."""
    if not pontos:
        return [], False

    reranker = _obter_modelo_reranker()
    if reranker is None:
        return [(pt, float(getattr(pt, "score", 0.0) or 0.0), "rrf") for pt in pontos[:top_k]], False

    docs = [str((getattr(pt, "payload", None) or {}).get("texto") or "") for pt in pontos]
    try:
        scores = [float(s) for s in reranker.rerank(consulta, docs)]
        if len(scores) != len(pontos):
            raise ValueError("Quantidade de scores do reranker diverge dos candidatos.")
        combinados = sorted(
            zip(pontos, scores, strict=False),
            key=lambda par: par[1],
            reverse=True,
        )
        return [(pt, sc, "rerank") for pt, sc in combinados[:top_k]], True
    except Exception:
        return [(pt, float(getattr(pt, "score", 0.0) or 0.0), "rrf") for pt in pontos[:top_k]], False


def _dividir_em_chunks(
    texto: str,
    tamanho: int = CHUNK_TAMANHO,
    sobreposicao: int = CHUNK_SOBREPOSICAO,
) -> list[str]:
    limpo = re.sub(r"[ \t]+", " ", (texto or "").replace("\r\n", "\n").replace("\r", "\n"))
    limpo = re.sub(r"\n{3,}", "\n\n", limpo).strip()
    if not limpo:
        return []
    if len(limpo) <= tamanho:
        return [limpo]

    chunks: list[str] = []
    inicio = 0
    total = len(limpo)

    while inicio < total:
        fim = min(total, inicio + tamanho)
        if fim < total:
            corte = limpo.rfind("\n\n", inicio + tamanho // 2, fim)
            if corte == -1:
                corte = limpo.rfind(". ", inicio + tamanho // 2, fim)
                if corte != -1:
                    corte += 1
            if corte == -1:
                corte = limpo.rfind(" ", inicio + tamanho // 2, fim)
            if corte > inicio:
                fim = corte

        trecho = limpo[inicio:fim].strip()
        if trecho:
            chunks.append(trecho)
        if fim >= total:
            break
        inicio = max(inicio + 1, fim - sobreposicao)
        if inicio >= total:
            break

    return chunks


def _ponto_id(chave_arquivo: str, idx: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"athena-rag:{chave_arquivo}:{idx}"))


def _remover_pontos_arquivo(client: QdrantClient, chave_arquivo: str) -> None:
    from qdrant_client import models

    client.delete(
        collection_name=COLECAO_ATHENA,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="chave_arquivo",
                        match=models.MatchValue(value=chave_arquivo),
                    )
                ]
            )
        ),
    )


def _upsert_em_lotes(client: QdrantClient, pontos: list[models.PointStruct]) -> None:
    for i in range(0, len(pontos), LOTE_UPSERT):
        client.upsert(
            collection_name=COLECAO_ATHENA,
            points=pontos[i : i + LOTE_UPSERT],
        )


def obter_estatisticas_rag() -> dict:
    """Le estatisticas do indice hibrido direto do memoria.db (instantaneo, sem carregar ONNX)."""
    with _conn() as c:
        row_total = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(chunks), 0), COALESCE(MAX(atualizado_em), '') FROM rag_arquivos_indexados"
        ).fetchone()
        por_fonte = {
            r[0]: {"arquivos": int(r[1]), "trechos": int(r[2])}
            for r in c.execute(
                "SELECT fonte, COUNT(*), COALESCE(SUM(chunks), 0) FROM rag_arquivos_indexados GROUP BY fonte"
            ).fetchall()
        }
    return {
        "colecao": COLECAO_ATHENA,
        "modo": "hibrido_bm25_rrf",
        "modelo": MODELO_EMBEDDING,
        "modelo_esparso": MODELO_ESPARSO,
        "modelo_reranker": MODELO_RERANKER,
        "arquivos_indexados": int(row_total[0] or 0),
        "trechos_indexados": int(row_total[1] or 0),
        "ultima_sincronizacao": row_total[2] or "",
        "por_fonte": por_fonte,
    }


def _montar_pontos_hibridos(chave: str, metadados_chunks: list[dict]) -> list[models.PointStruct]:
    if not metadados_chunks:
        return []
    from qdrant_client import models

    textos = [item["texto"] for item in metadados_chunks]
    densos, esparsos = _gerar_vetores_documentos(textos)
    pontos: list[models.PointStruct] = []
    for idx_chunk, (meta, vec_denso, vec_esparso) in enumerate(zip(metadados_chunks, densos, esparsos, strict=False)):
        pontos.append(
            models.PointStruct(
                id=_ponto_id(chave, idx_chunk),
                vector={
                    VETOR_DENSO: vec_denso,
                    VETOR_ESPARSO: vec_esparso,
                },
                payload={
                    **meta,
                    "chave_arquivo": chave,
                },
            )
        )
    return pontos


def sincronizar_indice_vetorial() -> dict:
    """Indexa incrementalmente PDFs e notas Obsidian com vetores densos + esparsos (BM25).

    Se detectar a collection antiga (sem vetores nomeados `dense` + `bm25`), recria a collection
    automaticamente com o novo schema hibrido e reindexa todos os arquivos.
    """
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Descobre arquivos atuais no disco (PDFs + Obsidian Vault)
    atuais: dict[str, tuple[str, Path, float, str, str]] = {}

    if PASTA_PDFS.exists():
        for pdf in sorted(PASTA_PDFS.rglob("*.pdf")):
            if not pdf.is_file():
                continue
            try:
                mtime = pdf.stat().st_mtime
            except OSError:
                continue
            materia = pdf.parent.name if pdf.parent != PASTA_PDFS else "geral"
            chave = f"pdf:{materia}/{pdf.name}"
            atuais[chave] = ("pdf", pdf, mtime, materia, pdf.name)

    try:
        vault = inicializar_vault()
        for res, rel in _iterar_notas_md(vault, vault):
            try:
                mtime = res.stat().st_mtime
            except OSError:
                continue
            rel_str = rel.as_posix()
            subpasta = rel.parts[0] if len(rel.parts) > 1 else "raiz"
            chave = f"obsidian:{rel_str}"
            atuais[chave] = ("obsidian", res, mtime, subpasta, rel_str)
    except OSError:
        pass

    arquivos_atualizados = 0
    arquivos_removidos = 0
    trechos_gravados = 0
    erros: list[str] = []

    with _QDRANT_LOCK:
        client, migrada = _obter_cliente(forcar_migracao=True)

        with _conn() as c:
            existentes: dict[str, tuple[str, float, int]] = {
                r[0]: (r[1], float(r[2]), int(r[3]))
                for r in c.execute("SELECT chave_arquivo, fonte, mtime, chunks FROM rag_arquivos_indexados").fetchall()
            }

        # 1) Remove do Qdrant arquivos que foram apagados do disco
        chaves_apagadas = [k for k in existentes if k not in atuais]
        if chaves_apagadas:
            with _conn() as c:
                for chave in chaves_apagadas:
                    with suppress(Exception):
                        _remover_pontos_arquivo(client, chave)
                    c.execute("DELETE FROM rag_arquivos_indexados WHERE chave_arquivo = ?", (chave,))
                    arquivos_removidos += 1

        # 2) Indexa apenas arquivos novos ou cujo mtime mudou (ou todos se migrada para schema hibrido)
        for chave, (fonte, caminho, mtime, materia, nome_arq) in atuais.items():
            antigo = existentes.get(chave)
            if antigo and abs(antigo[1] - mtime) < 1.0:
                continue

            metadados_chunks: list[dict] = []
            try:
                if fonte == "pdf":
                    from pypdf import PdfReader

                    reader = PdfReader(str(caminho))
                    for num_pag, pag in enumerate(reader.pages, start=1):
                        txt = (pag.extract_text() or "").strip()
                        if not txt:
                            continue
                        for trecho in _dividir_em_chunks(txt):
                            metadados_chunks.append({
                                "fonte": "pdf",
                                "materia": materia,
                                "arquivo": nome_arq,
                                "pagina": num_pag,
                                "texto": trecho,
                            })
                else:
                    conteudo = caminho.read_text(encoding="utf-8-sig", errors="replace").strip()
                    for trecho in _dividir_em_chunks(conteudo):
                        metadados_chunks.append({
                            "fonte": "obsidian",
                            "materia": materia,
                            "arquivo": nome_arq,
                            "pagina": None,
                            "texto": trecho,
                        })

                pontos = _montar_pontos_hibridos(chave, metadados_chunks)

                if antigo:
                    _remover_pontos_arquivo(client, chave)
                if pontos:
                    _upsert_em_lotes(client, pontos)

                with _conn() as c:
                    c.execute(
                        """
                        INSERT OR REPLACE INTO rag_arquivos_indexados
                            (chave_arquivo, fonte, mtime, chunks, atualizado_em)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (chave, fonte, mtime, len(pontos), agora),
                    )
                arquivos_atualizados += 1
                trechos_gravados += len(pontos)
            except Exception as e:
                erros.append(f"{nome_arq}: {e}")

    stats = obter_estatisticas_rag()
    return {
        "ok": True,
        "schema_migrado_hibrido": migrada,
        "modo": "hibrido_bm25_rrf",
        "arquivos_novos_ou_alterados": arquivos_atualizados,
        "arquivos_removidos": arquivos_removidos,
        "trechos_gravados": trechos_gravados,
        "total_arquivos": stats["arquivos_indexados"],
        "total_trechos": stats["trechos_indexados"],
        "por_fonte": stats["por_fonte"],
        "ultima_sincronizacao": stats["ultima_sincronizacao"] or agora,
        "erros": erros[:10],
    }


def _consultar_hibrido_no_cliente(
    client: QdrantClient,
    consulta: str,
    *,
    filtro: models.Filter | None = None,
    top_k: int = 5,
    candidatos_prefetch: int = CANDIDATOS_PREFETCH_PADRAO,
) -> tuple[list[tuple[Any, float, str]], bool, str]:
    """Executa Prefetch (dense + bm25) -> FusionQuery(Fusion.RRF) + Reranker local (com fallback RRF)."""
    limite_prefetch = max(int(candidatos_prefetch), top_k * 3)

    if _colecao_tem_schema_hibrido(client):
        from qdrant_client import models

        vec_denso, vec_esparso = _gerar_vetores_consulta(consulta)
        resp = client.query_points(
            collection_name=COLECAO_ATHENA,
            prefetch=[
                models.Prefetch(
                    query=vec_denso,
                    using=VETOR_DENSO,
                    filter=filtro,
                    limit=limite_prefetch,
                ),
                models.Prefetch(
                    query=vec_esparso,
                    using=VETOR_ESPARSO,
                    filter=filtro,
                    limit=limite_prefetch,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limite_prefetch,
            with_payload=True,
        )
        candidatos = list(getattr(resp, "points", None) or [])
        ranqueados, usou_rerank = _reranquear_candidatos(consulta, candidatos, top_k=top_k)
        modo = "hibrido+rerank" if usou_rerank else "hibrido+rrf"
        return ranqueados, usou_rerank, modo

    # Compatibilidade transitoria se a collection antiga (vetor unico sem nome) ainda nao tiver sido migrada
    vec_denso, _ = _gerar_vetores_consulta(consulta)
    resp = client.query_points(
        collection_name=COLECAO_ATHENA,
        query=vec_denso,
        query_filter=filtro,
        limit=limite_prefetch,
        with_payload=True,
    )
    candidatos = list(getattr(resp, "points", None) or [])
    ranqueados, usou_rerank = _reranquear_candidatos(consulta, candidatos, top_k=top_k)
    modo = "denso+rerank" if usou_rerank else "denso"
    return ranqueados, usou_rerank, modo


def buscar_semantico(
    consulta: str,
    fonte: str = "todos",
    limite: int = 5,
    candidatos_prefetch: int = CANDIDATOS_PREFETCH_PADRAO,
) -> str:
    """Busca hibrida (Densa + Esparsa BM25 com RRF + Reranker local) nos PDFs da faculdade e/ou notas do Obsidian."""
    termo = (consulta or "").strip()
    if not termo:
        return "Erro: informe uma consulta para a busca hibrida no Qdrant."

    fonte_norm = (fonte or "todos").strip().lower()
    if fonte_norm in ("pdfs", "faculdade", "aula", "aulas"):
        fonte_norm = "pdf"
    elif fonte_norm in ("vault", "notas", "md"):
        fonte_norm = "obsidian"
    elif fonte_norm not in ("pdf", "obsidian", "todos"):
        fonte_norm = "todos"

    stats = obter_estatisticas_rag()
    if stats["trechos_indexados"] == 0:
        sincronizar_indice_vetorial()
        stats = obter_estatisticas_rag()
        if stats["trechos_indexados"] == 0:
            return "Indice Qdrant esta vazio (nenhum PDF ou nota Markdown encontrado para indexar)."

    top_k = max(1, min(int(limite), 15))
    filtro = None
    if fonte_norm in ("pdf", "obsidian"):
        from qdrant_client import models

        filtro = models.Filter(
            must=[
                models.FieldCondition(
                    key="fonte",
                    match=models.MatchValue(value=fonte_norm),
                )
            ]
        )

    with _QDRANT_LOCK:
        client, _ = _obter_cliente(forcar_migracao=False)
        # Se a collection no disco ainda for a legada (sem BM25), migra automaticamente para hibrida
        if not _colecao_tem_schema_hibrido(client):
            sincronizar_indice_vetorial()
            client, _ = _obter_cliente(forcar_migracao=False)

        ranqueados, _usou_rerank, modo = _consultar_hibrido_no_cliente(
            client,
            termo,
            filtro=filtro,
            top_k=top_k,
            candidatos_prefetch=candidatos_prefetch,
        )

    if not ranqueados:
        return f"Nenhum trecho encontrado no Qdrant para '{termo}' (fonte={fonte_norm})."

    saida = [
        f"=== BUSCA HIBRIDA QDRANT ({modo}): '{termo}' (fonte={fonte_norm} | {len(ranqueados)} resultado(s)) ==="
    ]
    for pt, score, tipo_score in ranqueados:
        p = getattr(pt, "payload", None) or {}
        if p.get("fonte") == "pdf":
            mat = formatar_nome_materia(p.get("materia") or "geral")
            arq = p.get("arquivo") or "arquivo.pdf"
            pag = p.get("pagina") or "?"
            citacao = f"[{arq}, pág. {pag}] (PDF · {mat})"
        else:
            mat = p.get("materia") or "raiz"
            arq = p.get("arquivo") or "nota.md"
            citacao = f"[Obsidian: {arq}] ({mat})"
        texto = (p.get("texto") or "").strip()
        saida.append(f"\n{citacao} | {tipo_score}={score:.3f}\n{texto[:900]}")

    return compactar_saida_tool("\n\n---\n".join(saida))


# Aliases ergonomicos
reindexar = sincronizar_indice_vetorial
buscar = buscar_semantico
