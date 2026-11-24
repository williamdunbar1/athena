"""Memoria Temporal em Grafo 100% local (SQLite + Alembic + NetworkX), inspirada em Graphiti e Mem0.

Recursos:
- `registrar_fato_temporal`: registra triplas `(sujeito, relacao, objeto)` com mascara OWASP de PII/segredos,
  deduplicacao de fato identico vigente e encerramento automatico (`valido_ate = agora`) quando um fato muda.
- `consultar_memoria_temporal`: constroi um `nx.DiGraph` leve com os fatos (vigentes ou incluindo historico)
  e recupera fatos relevantes por entidade/palavra-chave + vizinhos de 1 salto no grafo.
- `linha_do_tempo_entidade`: retorna a evolucao cronologica completa de uma entidade/relacao.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from eficiencia import compactar_saida_tool, normalizar_texto_eficiencia
from guardrails import mascarar_segredos_e_pii
from memoria import obter_db_path

_STOPWORDS_TEMPORAL: frozenset[str] = frozenset(
    {
        "de",
        "da",
        "do",
        "das",
        "dos",
        "em",
        "no",
        "na",
        "nos",
        "nas",
        "com",
        "por",
        "para",
        "que",
        "qual",
        "quais",
        "como",
        "meu",
        "minha",
        "meus",
        "minhas",
        "sobre",
        "esta",
        "estao",
        "foi",
        "era",
        "mudou",
        "mudaram",
        "mes",
        "ano",
        "semana",
        "hoje",
        "agora",
        "antes",
        "depois",
        "um",
        "uma",
        "os",
        "as",
        "ao",
        "aos",
    }
)
_RE_TOKEN = re.compile(r"[a-z0-9_]{2,}")

# Regra "Slow Facts vs. Fast Facts": padroes de dados volateis que mudam rapido e NAO devem ser memorizados.
_PADROES_DADO_VOLATIL: tuple[re.Pattern[str], ...] = (
    # Cotacoes, cambio e precos momentaneos de mercado
    re.compile(
        r"\b(?:cotac(?:ao|oes)|preco\s+(?:atual|de\s+hoje|agora|do\s+dia)|"
        r"dolar\s+(?:hoje|agora|esta|em|a\s+r?\$|fechou|cotado)|"
        r"euro\s+(?:hoje|agora|esta|a\s+r?\$)|"
        r"(?:bitcoin|btc|eth|ethereum|ibovespa|selic)\s+(?:hoje|agora|esta|em|a\s+r?\$|caiu|subiu)|"
        r"(?:petr4|vale3|itub4|bbas3|mxrf11|hglg11|bova11|ivvb11)\s*(?:esta|a|em|:|=)?\s*r?\$?\s*\d)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:dolar|euro|bitcoin|btc|eth|ibovespa|acao|acoes|fii)\b.{0,30}(?:r\$|\$|usd|brl)\s*\d",
        re.IGNORECASE,
    ),
    # Saldo bancario / conta pontual
    re.compile(
        r"\b(?:saldo\s+(?:atual|hoje|agora|em\s+conta|da\s+conta|bancario|disponivel|no\s+mercado\s+pago|no\s+nubank)|"
        r"(?:estou\s+com|tenho)\s+r\$\s*\d[\d.,]*\s+(?:na\s+conta|no\s+banco|no\s+mercado\s+pago|hoje|agora))\b",
        re.IGNORECASE,
    ),
    # Erros HTTP / API transitorios (503, 429, 502, 504, timeouts momentaneos)
    re.compile(
        r"\b(?:erro\s*(?:http\s*)?(?:429|500|502|503|504)|http\s*(?:429|500|502|503|504)|"
        r"status\s*(?:429|500|502|503|504)|modelunavailableerror|rate\s*limit|"
        r"service\s+unavailable|servidor\s+indisponivel\s+(?:hoje|agora))\b",
        re.IGNORECASE,
    ),
    # Clima / temperatura / tempo momentaneo
    re.compile(
        r"\b(?:clima\s+(?:hoje|agora|atual|em)|previsao\s+do\s+tempo|"
        r"temperatura\s+(?:atual|hoje|agora|esta|de\s+\d+)|esta\s+chovendo|fazendo\s+\d{1,2}\s*(?:graus|c))\b",
        re.IGNORECASE,
    ),
    # Notas parciais ou frequencia transitoria do dia
    re.compile(
        r"\b(?:nota\s+parcial|media\s+parcial|pontuacao\s+parcial|faltas?\s+(?:hoje|atuais|agora))\b",
        re.IGNORECASE,
    ),
)


def eh_dado_volatil(texto: str) -> bool:
    """Detecta Fast Facts (dados volateis: cotacoes, saldo pontual, erros 503/429, clima, notas parciais).

    Fatos volateis mudam rapidamente e NAO devem ser persistidos na memoria de longo prazo —
    a Athena deve busca-los na fonte ao vivo sempre que precisar.
    """
    bruto = (texto or "").strip()
    if not bruto:
        return False
    norm = normalizar_texto_eficiencia(bruto)
    return any(p.search(norm) or p.search(bruto) for p in _PADROES_DADO_VOLATIL)


def _garantir_tabela_temporal(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS fatos_temporais (
            id TEXT PRIMARY KEY,
            sujeito TEXT NOT NULL,
            relacao TEXT NOT NULL,
            objeto TEXT NOT NULL,
            categoria TEXT NOT NULL DEFAULT 'geral',
            origem TEXT NOT NULL DEFAULT 'chat',
            valido_de TEXT NOT NULL,
            valido_ate TEXT,
            criado_em TEXT NOT NULL
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fatos_temporais_sujeito_relacao_valido
        ON fatos_temporais (sujeito, relacao, valido_ate)
        """
    )


@contextmanager
def _conn_temporal(db_path: str | Path | None = None):
    alvo = obter_db_path(db_path)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(alvo, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        _garantir_tabela_temporal(c)
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _row_para_fato(row: tuple[Any, ...]) -> dict[str, Any]:
    valido_ate = row[7]
    return {
        "id": str(row[0]),
        "sujeito": str(row[1]),
        "relacao": str(row[2]),
        "objeto": str(row[3]),
        "categoria": str(row[4] or "geral"),
        "origem": str(row[5] or "chat"),
        "valido_de": str(row[6]),
        "valido_ate": str(valido_ate) if valido_ate is not None else None,
        "criado_em": str(row[8]),
        "vigente": valido_ate is None,
    }


def _norm_chave(texto: str) -> str:
    return re.sub(r"\s+", " ", normalizar_texto_eficiencia(texto)).strip()


def _extrair_tokens(texto: str) -> set[str]:
    norm = normalizar_texto_eficiencia(texto)
    return {t for t in _RE_TOKEN.findall(norm) if t not in _STOPWORDS_TEMPORAL}


def registrar_fato_temporal(
    sujeito: str,
    relacao: str,
    objeto: str,
    categoria: str = "geral",
    origem: str = "chat",
    db_path: str | Path | None = None,
    *,
    validar_volatil: bool = False,
) -> dict[str, Any]:
    """Registra um fato temporal `(sujeito, relacao, objeto)` preservando historico completo de mudancas.

    - Aplica `guardrails.mascarar_segredos_e_pii` no `objeto` (e `sujeito`) antes de persistir.
    - Quando `validar_volatil=True`, ignora Fast Facts volateis (`eh_dado_volatil`).
    - Se ja existir fato vigente identico para `(sujeito, relacao)`, nao duplica e retorna o existente.
    - Se ja existir fato vigente com `objeto` diferente, encerra o anterior (`valido_ate = agora`)
      e insere o novo como vigente (`valido_ate = NULL`).
    """
    suj_bruto = " ".join((sujeito or "").split())[:120]
    rel_bruta = " ".join((relacao or "").split())[:100]
    obj_bruto = " ".join((objeto or "").split())[:500]
    if not suj_bruto or not rel_bruta or not obj_bruto:
        raise ValueError("Informe sujeito, relacao e objeto nao vazios para registrar fato temporal.")

    if validar_volatil and eh_dado_volatil(f"{rel_bruta} {obj_bruto}"):
        return {
            "id": "",
            "sujeito": suj_bruto,
            "relacao": rel_bruta,
            "objeto": obj_bruto,
            "categoria": categoria,
            "origem": origem,
            "vigente": False,
            "acao": "ignorado_volatil",
            "ignorado": True,
        }

    suj_seg, _ = mascarar_segredos_e_pii(suj_bruto)
    obj_seg, _ = mascarar_segredos_e_pii(obj_bruto)
    cat_limpa = (" ".join((categoria or "geral").split()) or "geral")[:60]
    orig_limpa = (" ".join((origem or "chat").split()) or "chat")[:60]

    suj_norm = _norm_chave(suj_seg)
    rel_norm = _norm_chave(rel_bruta)
    obj_norm = _norm_chave(obj_seg)

    agora = datetime.now().isoformat(timespec="microseconds")

    with _conn_temporal(db_path) as c:
        vigentes = c.execute(
            """
            SELECT id, sujeito, relacao, objeto, categoria, origem, valido_de, valido_ate, criado_em
            FROM fatos_temporais
            WHERE valido_ate IS NULL
            ORDER BY valido_de DESC, rowid DESC
            """
        ).fetchall()

        correspondentes = [
            r for r in vigentes if _norm_chave(str(r[1])) == suj_norm and _norm_chave(str(r[2])) == rel_norm
        ]

        if correspondentes:
            mais_recente = correspondentes[0]
            if _norm_chave(str(mais_recente[3])) == obj_norm:
                fato = _row_para_fato(mais_recente)
                fato["acao"] = "mantido"
                return fato

            substituidos = [str(r[0]) for r in correspondentes]
            for fid in substituidos:
                c.execute(
                    "UPDATE fatos_temporais SET valido_ate = ? WHERE id = ?",
                    (agora, fid),
                )
            acao = "atualizado"
            substituiu_id: str | None = substituidos[0]
        else:
            acao = "criado"
            substituiu_id = None

        novo_id = f"ft_{uuid.uuid4().hex[:12]}"
        c.execute(
            """
            INSERT INTO fatos_temporais
                (id, sujeito, relacao, objeto, categoria, origem, valido_de, valido_ate, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (novo_id, suj_seg, rel_bruta, obj_seg, cat_limpa, orig_limpa, agora, agora),
        )
        row_novo = c.execute(
            """
            SELECT id, sujeito, relacao, objeto, categoria, origem, valido_de, valido_ate, criado_em
            FROM fatos_temporais WHERE id = ?
            """,
            (novo_id,),
        ).fetchone()

    fato_final = _row_para_fato(row_novo)
    fato_final["acao"] = acao
    if substituiu_id is not None:
        fato_final["substituiu_id"] = substituiu_id
    return fato_final


def consultar_memoria_temporal(
    consulta: str,
    incluir_historico: bool = False,
    limite: int = 8,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Consulta fatos temporais usando um grafo `nx.DiGraph` com expansao de vizinhos de 1 salto."""
    lim = max(1, min(int(limite or 8), 50))
    with _conn_temporal(db_path) as c:
        if incluir_historico:
            rows = c.execute(
                """
                SELECT id, sujeito, relacao, objeto, categoria, origem, valido_de, valido_ate, criado_em
                FROM fatos_temporais
                ORDER BY valido_de DESC, rowid DESC
                """
            ).fetchall()
        else:
            rows = c.execute(
                """
                SELECT id, sujeito, relacao, objeto, categoria, origem, valido_de, valido_ate, criado_em
                FROM fatos_temporais
                WHERE valido_ate IS NULL
                ORDER BY valido_de DESC, rowid DESC
                """
            ).fetchall()

    fatos = [_row_para_fato(r) for r in rows]
    if not fatos:
        return []

    consulta_limpa = (consulta or "").strip()
    tokens = _extrair_tokens(consulta_limpa)
    consulta_norm = _norm_chave(consulta_limpa)

    if not tokens and not consulta_norm:
        return fatos[:lim]

    import networkx as nx

    grafo: nx.DiGraph = nx.DiGraph()
    fatos_por_no: dict[str, list[dict[str, Any]]] = {}

    for fato in fatos:
        no_s = _norm_chave(fato["sujeito"])
        no_o = _norm_chave(fato["objeto"])
        grafo.add_node(no_s, label=fato["sujeito"])
        grafo.add_node(no_o, label=fato["objeto"])
        aresta_fatos = grafo.get_edge_data(no_s, no_o, default={}).get("fatos", [])
        aresta_fatos = [*aresta_fatos, fato]
        grafo.add_edge(no_s, no_o, fatos=aresta_fatos)
        fatos_por_no.setdefault(no_s, []).append(fato)
        fatos_por_no.setdefault(no_o, []).append(fato)

    scores: dict[str, float] = {}
    mapa_fatos: dict[str, dict[str, Any]] = {f["id"]: f for f in fatos}
    nos_semente: set[str] = set()

    for fato in fatos:
        suj_n = _norm_chave(fato["sujeito"])
        rel_n = _norm_chave(fato["relacao"])
        obj_n = _norm_chave(fato["objeto"])
        cat_n = _norm_chave(fato["categoria"])
        texto_fato = f"{suj_n} {rel_n} {obj_n} {cat_n}"
        tokens_fato = _extrair_tokens(texto_fato)

        pontos = 0.0
        if consulta_norm and consulta_norm in texto_fato:
            pontos += 4.0
        for tok in tokens:
            if tok in tokens_fato or tok in texto_fato:
                pontos += 2.5

        if pontos > 0:
            if fato["vigente"]:
                pontos += 0.5
            scores[fato["id"]] = max(scores.get(fato["id"], 0.0), pontos)
            nos_semente.add(suj_n)
            nos_semente.add(obj_n)

    # Expansao de 1 salto no grafo (sucessores + predecessores dos nos-semente)
    vizinhos_1hop: set[str] = set()
    for no in nos_semente:
        if no in grafo:
            vizinhos_1hop.update(grafo.successors(no))
            vizinhos_1hop.update(grafo.predecessors(no))
            # Tambem inclui nos cuja label contenha tokens-chave da consulta
    for no in grafo.nodes:
        if consulta_norm and (consulta_norm in no or no in consulta_norm):
            nos_semente.add(no)
            vizinhos_1hop.update(grafo.successors(no))
            vizinhos_1hop.update(grafo.predecessors(no))

    for no_viz in nos_semente | vizinhos_1hop:
        for fato_viz in fatos_por_no.get(no_viz, []):
            fid = fato_viz["id"]
            if fid not in scores:
                scores[fid] = 1.25 + (0.25 if fato_viz["vigente"] else 0.0)

    if not scores:
        # Se a consulta era generica sobre historico/mudancas sem termo exato, devolve os mais recentes
        if any(p in consulta_norm for p in ("mud", "historic", "evoluc", "meta", "preferenc", "memor")):
            return fatos[:lim]
        return []

    ordenados = sorted(
        scores.items(),
        key=lambda par: (
            par[1],
            1 if mapa_fatos[par[0]]["vigente"] else 0,
            mapa_fatos[par[0]]["valido_de"],
        ),
        reverse=True,
    )
    return [mapa_fatos[fid] for fid, _sc in ordenados[:lim]]


def linha_do_tempo_entidade(
    sujeito: str,
    relacao: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Retorna a evolucao cronologica (mais antigo -> mais recente) dos fatos de uma entidade."""
    suj_norm = _norm_chave(sujeito)
    rel_norm = _norm_chave(relacao) if relacao and relacao.strip() else ""
    if not suj_norm:
        return []

    with _conn_temporal(db_path) as c:
        rows = c.execute(
            """
            SELECT id, sujeito, relacao, objeto, categoria, origem, valido_de, valido_ate, criado_em
            FROM fatos_temporais
            ORDER BY valido_de ASC, criado_em ASC, rowid ASC
            """
        ).fetchall()

    resultado: list[dict[str, Any]] = []
    for r in rows:
        if _norm_chave(str(r[1])) != suj_norm:
            continue
        if rel_norm and _norm_chave(str(r[2])) != rel_norm:
            continue
        resultado.append(_row_para_fato(r))
    return resultado


def resumo_memoria_temporal(
    limite_vigentes: int = 50,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Resumo para UI/API: lista fatos vigentes e contagens de historico."""
    lim = max(1, min(int(limite_vigentes or 50), 200))
    with _conn_temporal(db_path) as c:
        rows_vig = c.execute(
            """
            SELECT id, sujeito, relacao, objeto, categoria, origem, valido_de, valido_ate, criado_em
            FROM fatos_temporais
            WHERE valido_ate IS NULL
            ORDER BY valido_de DESC, rowid DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
        total_vig = int(
            c.execute("SELECT COUNT(*) FROM fatos_temporais WHERE valido_ate IS NULL").fetchone()[0] or 0
        )
        total_hist = int(
            c.execute("SELECT COUNT(*) FROM fatos_temporais WHERE valido_ate IS NOT NULL").fetchone()[0] or 0
        )

    return {
        "ok": True,
        "vigentes": [_row_para_fato(r) for r in rows_vig],
        "total_vigentes": total_vig,
        "total_historicos": total_hist,
        "total_geral": total_vig + total_hist,
    }


def formatar_fatos_temporais_markdown(
    fatos: list[dict[str, Any]],
    *,
    incluir_historico: bool = False,
) -> str:
    """Formata fatos temporais de forma enxuta para consumo pela LLM."""
    if not fatos:
        escopo = "vigentes ou historicos" if incluir_historico else "vigentes"
        return f"Nenhum fato temporal ({escopo}) encontrado para essa consulta."

    linhas = [
        "=== MEMORIA TEMPORAL EM GRAFO ===",
    ]
    for f in fatos:
        status = "VIGENTE" if f.get("vigente") else f"ENCERRADO em {str(f.get('valido_ate') or '')[:16]}"
        inicio = str(f.get("valido_de") or "")[:16]
        linhas.append(
            f"- [{status}] ({f['sujeito']}) --[{f['relacao']}]--> {f['objeto']} "
            f"(categoria: {f['categoria']} | desde: {inicio})"
        )
    return compactar_saida_tool("\n".join(linhas), max_chars=2400)
