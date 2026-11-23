"""GraphRAG leve sobre o Obsidian Vault (`Athena-Vault`) usando `networkx`.

Recursos:
- `construir_grafo_vault(vault_dir=None) -> nx.DiGraph`: escaneia notas `.md`, extrai `[[wikilinks]]`
  (`[[Nota]]`, `[[Nota|Alias]]`, `[[Pasta/Nota]]`) e `#tags` (frontmatter + corpo).
- `buscar_contexto_grafo(consulta, profundidade=2, max_notas=6, vault_dir=None) -> str`:
  identifica notas-semente e faz travessia em largura (BFS multi-hop) no grafo nao-direcionado equivalente
  para descobrir conexoes indiretas, caminhos e resumos.
- `estatisticas_grafo(vault_dir=None) -> dict`: contagem de notas/conexoes, top 5 hubs por grau e notas orfas.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any

import networkx as nx

from ferramentas_obsidian import PASTAS_OCULTAS_IGNORAR, obter_pasta_vault

_RE_BLOCO_CODIGO = re.compile(r"```[\s\S]*?```")
_RE_WIKILINK = re.compile(r"(?<!!)\[\[([^\[\]]+)\]\]")
_RE_TAG_INLINE = re.compile(r"(?<![\w/#&])#([A-Za-zÀ-ÿ_][A-Za-zÀ-ÿ0-9_/-]{1,48})")
_RE_COR_HEX = re.compile(r"^[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")
_RE_PALAVRA = re.compile(r"[a-z0-9]{2,}")

_STOPWORDS_PT: frozenset[str] = frozenset(
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
        "nota",
        "notas",
        "obsidian",
        "vault",
        "grafo",
        "conecta",
        "conectam",
        "conexao",
        "conexoes",
        "se",
        "um",
        "uma",
    }
)


def _normalizar(texto: str) -> str:
    sem_acento = (
        unicodedata.normalize("NFKD", (texto or "").strip())
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"\s+", " ", sem_acento).strip()


def _tem_segmento_oculto(rel: Path) -> bool:
    return any(
        parte.lower() in PASTAS_OCULTAS_IGNORAR or parte.startswith(".")
        for parte in rel.parts
        if parte not in ("", ".")
    )


def _separar_frontmatter(conteudo: str) -> tuple[str, str]:
    texto = (conteudo or "").lstrip("\ufeff")
    linhas = texto.splitlines()
    if not linhas or linhas[0].strip() != "---":
        return "", texto
    for idx in range(1, len(linhas)):
        if linhas[idx].strip() == "---":
            return "\n".join(linhas[1:idx]), "\n".join(linhas[idx + 1 :])
    return "", texto


def _extrair_tags(frontmatter: str, corpo: str) -> list[str]:
    tags: list[str] = []

    def _add(raw: str) -> None:
        limpa = (raw or "").strip().strip("'\"[]#").lower()
        if not limpa or _RE_COR_HEX.match(limpa):
            return
        if limpa not in tags:
            tags.append(limpa)

    # 1) Frontmatter YAML (`tags: [a, b]` ou lista com `- tag`)
    if frontmatter:
        em_tags = False
        for ln in frontmatter.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            low = s.lower()
            if low.startswith("tags:") or low.startswith("tag:"):
                resto = s.split(":", 1)[1].strip()
                if resto:
                    for item in resto.strip("[]").split(","):
                        _add(item)
                    em_tags = False
                else:
                    em_tags = True
                continue
            if em_tags:
                if s.startswith("- "):
                    _add(s[2:])
                elif ":" in s and not ln[0].isspace():
                    em_tags = False

    # 2) Inline `#tags` no corpo (ignorando blocos de codigo)
    corpo_sem_codigo = _RE_BLOCO_CODIGO.sub(" ", corpo or "")
    for m in _RE_TAG_INLINE.finditer(corpo_sem_codigo):
        _add(m.group(1))

    return sorted(tags)


def _extrair_wikilinks(corpo: str) -> list[str]:
    corpo_sem_codigo = _RE_BLOCO_CODIGO.sub(" ", corpo or "")
    alvos: list[str] = []
    for m in _RE_WIKILINK.finditer(corpo_sem_codigo):
        bruto = (m.group(1) or "").strip()
        if not bruto:
            continue
        # Remove alias (`[[Nota|Alias]]`) e ancora (`[[Nota#Secao]]`)
        alvo = bruto.split("|", 1)[0].split("#", 1)[0].strip()
        alvo = alvo.replace("\\", "/").strip("/")
        if alvo.lower().endswith(".md"):
            alvo = alvo[:-3]
        if alvo and alvo not in alvos:
            alvos.append(alvo)
    return alvos


def _gerar_resumo_curto(corpo: str, limite: int = 220) -> str:
    limpo = _RE_BLOCO_CODIGO.sub(" ", corpo or "")
    linhas_uteis: list[str] = []
    for ln in limpo.splitlines():
        s = re.sub(r"^\s*#{1,6}\s+", "", ln).strip()
        if not s or s in ("---", "***"):
            continue
        linhas_uteis.append(s)
        if sum(len(x) for x in linhas_uteis) >= limite:
            break
    texto = " ".join(linhas_uteis)
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) > limite:
        return texto[: limite - 3].rstrip() + "..."
    return texto or "(nota sem conteudo textual)"


def _resolver_pasta_vault(vault_dir: Path | None = None) -> Path:
    if vault_dir is not None:
        return Path(vault_dir).resolve()
    return obter_pasta_vault()


def construir_grafo_vault(vault_dir: Path | None = None) -> nx.DiGraph:
    """Escaneia arquivos `.md` do Obsidian Vault e retorna um `nx.DiGraph` conectado por `[[wikilinks]]`."""
    grafo: nx.DiGraph = nx.DiGraph()
    base = _resolver_pasta_vault(vault_dir)
    if not base.is_dir():
        return grafo

    notas_raw: list[tuple[str, Path, str, str, list[str], list[str]]] = []
    indice_por_caminho: dict[str, str] = {}
    indice_por_stem: dict[str, list[str]] = {}

    for arq in sorted(base.rglob("*.md"), key=lambda p: p.as_posix().lower()):
        try:
            res = arq.resolve()
            if not res.is_file() or base not in res.parents:
                continue
            rel = res.relative_to(base)
        except (OSError, ValueError):
            continue
        if _tem_segmento_oculto(rel):
            continue

        rel_posix = rel.as_posix()
        no_id = rel_posix[:-3] if rel_posix.lower().endswith(".md") else rel_posix
        try:
            conteudo = res.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        fm, corpo = _separar_frontmatter(conteudo)
        tags = _extrair_tags(fm, corpo)
        wikilinks = _extrair_wikilinks(corpo)
        notas_raw.append((no_id, rel, fm, corpo, tags, wikilinks))

        chave_cam = _normalizar(no_id)
        indice_por_caminho[chave_cam] = no_id
        chave_stem = _normalizar(rel.stem)
        indice_por_stem.setdefault(chave_stem, []).append(no_id)

    for no_id, rel, _fm, corpo, tags, _links in notas_raw:
        pasta = rel.parent.as_posix() if rel.parent != Path(".") else "raiz"
        resumo = _gerar_resumo_curto(corpo)
        grafo.add_node(
            no_id,
            titulo=rel.stem,
            caminho=rel.as_posix(),
            pasta=pasta,
            tags=tags,
            resumo=resumo,
            texto_busca=_normalizar(f"{rel.stem} {no_id} {' '.join(tags)} {corpo[:4000]}"),
        )

    for no_id, _rel, _fm, _corpo, _tags, wikilinks in notas_raw:
        for alvo in wikilinks:
            chave_alvo = _normalizar(alvo)
            destino = indice_por_caminho.get(chave_alvo)
            if destino is None:
                stem_alvo = _normalizar(alvo.rsplit("/", 1)[-1])
                cands = indice_por_stem.get(stem_alvo) or []
                if cands:
                    destino = cands[0]
            if destino and destino != no_id and grafo.has_node(destino):
                grafo.add_edge(no_id, destino, tipo="wikilink")

    return grafo


def estatisticas_grafo(vault_dir: Path | None = None) -> dict[str, Any]:
    """Retorna metricas do grafo do Vault: total de notas, conexoes, top 5 hubs e notas orfas."""
    grafo = construir_grafo_vault(vault_dir)
    total_notas = grafo.number_of_nodes()
    total_conexoes = grafo.number_of_edges()

    metricas_nos: list[dict[str, Any]] = []
    orfas: list[dict[str, Any]] = []
    for no_id, attrs in grafo.nodes(data=True):
        entrada = int(grafo.in_degree(no_id))  # type: ignore[arg-type]
        saida = int(grafo.out_degree(no_id))  # type: ignore[arg-type]
        grau = entrada + saida
        item = {
            "nota": no_id,
            "titulo": str(attrs.get("titulo") or no_id),
            "caminho": str(attrs.get("caminho") or f"{no_id}.md"),
            "grau": grau,
            "entrada": entrada,
            "saida": saida,
            "tags": list(attrs.get("tags") or []),
        }
        if grau == 0:
            orfas.append(
                {
                    "nota": no_id,
                    "titulo": item["titulo"],
                    "caminho": item["caminho"],
                    "tags": item["tags"],
                }
            )
        else:
            metricas_nos.append(item)

    metricas_nos.sort(key=lambda x: (-x["grau"], -x["entrada"], x["nota"].lower()))
    orfas.sort(key=lambda x: x["nota"].lower())

    return {
        "total_notas": total_notas,
        "total_conexoes": total_conexoes,
        "top_hubs": metricas_nos[:5],
        "notas_orfas": orfas,
        "total_orfas": len(orfas),
    }


def _pontuar_semente(no_id: str, attrs: dict[str, Any], consulta_norm: str, tokens: list[str]) -> float:
    titulo_norm = _normalizar(str(attrs.get("titulo") or no_id))
    caminho_norm = _normalizar(no_id)
    tags_norm = [_normalizar(t) for t in (attrs.get("tags") or [])]
    texto_norm = str(attrs.get("texto_busca") or "")

    score = 0.0
    if consulta_norm and consulta_norm == titulo_norm:
        score += 12.0
    elif consulta_norm and consulta_norm in titulo_norm:
        score += 8.0
    elif consulta_norm and consulta_norm in caminho_norm:
        score += 6.0

    for tok in tokens:
        if tok == titulo_norm:
            score += 6.0
        elif tok in titulo_norm:
            score += 3.5
        elif tok in caminho_norm:
            score += 2.5
        if any(tok == tg or tok in tg for tg in tags_norm):
            score += 3.0
        if tok in texto_norm:
            score += 1.0

    return score


def buscar_contexto_grafo(
    consulta: str,
    profundidade: int = 2,
    max_notas: int = 6,
    vault_dir: Path | None = None,
) -> str:
    """Descobre notas-semente e executa travessia BFS multi-hop para trazer contexto relacional do Vault."""
    grafo = construir_grafo_vault(vault_dir)
    if grafo.number_of_nodes() == 0:
        return "O Obsidian Vault ainda nao possui notas Markdown indexaveis no grafo."

    consulta_norm = _normalizar(consulta)
    prof = max(1, min(int(profundidade or 2), 4))
    limite = max(1, min(int(max_notas or 6), 15))

    # Perguntas estruturais sobre orfas / hubs / estatisticas do grafo
    pede_estrutura = any(
        kw in consulta_norm
        for kw in ("orfa", "orfas", "desconectad", "hub", "hubs", "estatistica", "visao geral do grafo")
    )
    if pede_estrutura:
        st = estatisticas_grafo(vault_dir)
        linhas_st = [
            f"=== GRAFO DO OBSIDIAN ({st['total_notas']} nota(s) | {st['total_conexoes']} conexao(oes) | {st['total_orfas']} orfa(s)) ===",
        ]
        if st["top_hubs"]:
            linhas_st.append("\nTop Hubs mais conectados:")
            for h in st["top_hubs"]:
                linhas_st.append(
                    f"- [[{h['nota']}]] (grau: {h['grau']} | entrada: {h['entrada']} | saida: {h['saida']})"
                )
        if st["notas_orfas"]:
            linhas_st.append("\nNotas orfas (sem links de entrada nem saida):")
            for o in st["notas_orfas"][:25]:
                linhas_st.append(f"- [[{o['nota']}]] ({o['caminho']})")
        else:
            linhas_st.append("\nNenhuma nota orfa encontrada — todas as notas possuem conexoes.")
        return "\n".join(linhas_st)

    tokens = [t for t in _RE_PALAVRA.findall(consulta_norm) if t not in _STOPWORDS_PT]
    if not tokens and consulta_norm:
        tokens = [consulta_norm]

    ranqueadas: list[tuple[float, str]] = []
    for no_id, attrs in grafo.nodes(data=True):
        sc = _pontuar_semente(no_id, attrs, consulta_norm, tokens)
        if sc > 0:
            ranqueadas.append((sc, no_id))

    if not ranqueadas:
        return f"Nenhuma nota do Obsidian correspondeu a consulta '{consulta}' no grafo."

    ranqueadas.sort(key=lambda x: (-x[0], x[1].lower()))
    sementes = [no_id for _, no_id in ranqueadas[: max(1, min(3, limite))]]

    # Travessia em largura (BFS) no grafo nao-direcionado equivalente ate `prof` saltos
    grafo_nd = grafo.to_undirected()
    visitados: dict[str, tuple[int, list[str]]] = {}
    fila: deque[tuple[str, int, list[str]]] = deque()
    for sem in sementes:
        visitados[sem] = (0, [sem])
        fila.append((sem, 0, [sem]))

    ordem_descoberta: list[str] = list(sementes)
    while fila and len(ordem_descoberta) < limite:
        atual, dist, caminho_nos = fila.popleft()
        if dist >= prof:
            continue
        vizinhos = sorted(
            grafo_nd.neighbors(atual),
            key=lambda n: (-int(grafo_nd.degree(n)), str(n).lower()),  # type: ignore[arg-type]
        )
        for viz in vizinhos:
            viz_id = str(viz)
            if viz_id not in visitados:
                novo_caminho = [*caminho_nos, viz_id]
                visitados[viz_id] = (dist + 1, novo_caminho)
                ordem_descoberta.append(viz_id)
                if len(ordem_descoberta) >= limite:
                    break
                fila.append((viz_id, dist + 1, novo_caminho))

    linhas = [
        f"=== GRAPHRAG OBSIDIAN PARA '{consulta}' ({len(ordem_descoberta)} nota(s) | profundidade <= {prof}) ===",
        f"Sementes: {', '.join(f'[[{s}]]' for s in sementes)}",
    ]

    for idx, no_id in enumerate(ordem_descoberta, start=1):
        attrs = grafo.nodes[no_id]
        dist, caminho_nos = visitados[no_id]
        caminho_fmt = " -> ".join(f"[[{n}]]" for n in caminho_nos)
        tags_list = list(attrs.get("tags") or [])
        tags_txt = " ".join(f"#{t}" for t in tags_list[:8])
        rotulo_dist = "semente" if dist == 0 else f"{dist} salto(s)"
        linhas.append(f"\n{idx}. [[{no_id}]] ({attrs.get('caminho', no_id)} | {rotulo_dist})")
        if dist > 0:
            linhas.append(f"   Caminho: {caminho_fmt}")
        if tags_txt:
            linhas.append(f"   Tags: {tags_txt}")
        linhas.append(f"   Resumo: {attrs.get('resumo', '')}")

    return "\n".join(linhas)
