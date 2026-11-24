"""Roteador Fast-Path deterministico (Zero-LLM) da Athena Pessoal.

Intercepta consultas pessoais curtas, diretas e inequivocas (< 50 ms, 0 tokens de API):
1. `estudos_ulife`: consultar notas, faltas ou proximas provas direto do cache local do Ulife no SQLite.
2. `obsidian_grafo`: metricas, hubs e notas orfas do GraphRAG do Obsidian Vault.

Qualquer mensagem aberta, longa (> 100 chars) ou composta retorna `None`, devolvendo
o controle de forma 100% transparente ao fluxo normal da Orquestradora LLM.
"""

from __future__ import annotations

import re
from typing import Any

from eficiencia import compactar_saida_tool, normalizar_texto_eficiencia


def obter_dados_ulife_estruturados(secao: str = "tudo") -> dict[str, Any]:
    from ulife_v2 import obter_dados_ulife_estruturados as _ulife_dados

    return _ulife_dados(secao)


MODELO_FAST_PATH = "Fast-Path (Local)"
MAX_CHARS_FAST_PATH = 100

_RE_COMPOSTO_OU_ANALITICO = re.compile(
    r"(?:;|\be\s+(?:depois|tambem|faz|faca|cria|crie|gera|gere|analis[ae]|resum[aeo]|"
    r"compar[ae]|busca|busque|pesquis[ae]|salv[ae]|envi[ae]|me\s+)\b|"
    r"\b(?:alem\s+disso|depois\s+me|comparando|analis[ae]|resum[aeo]|expliqu[ae]|explic[ae]|"
    r"cri[ae]\s+um|mont[ae]\s+um|planej[ae]|escrev[ae]|redig[ae]|traduz[ae]|otimiz[ae]|"
    r"refator[ae]|simulado|plano\s+de\s+estudo|sincroniz[ae]|atualiz[ae])\b)"
)

_RE_NOTAS_ULIFE = re.compile(
    r"^(?:(?:por\s+favor|athena)\s+)?(?:quais\s+(?:sao\s+)?(?:as\s+)?)?"
    r"(?:ver\s+|mostr[ae]\s+|consultar?\s+)?(?:as\s+|minhas\s+)?"
    r"(?:notas?|boletim|faltas|frequencia)(?:\s+(?:da\s+faculdade|do\s+ulife|no\s+ulife|atuais))?$"
)
_RE_PROVAS_ULIFE = re.compile(
    r"^(?:(?:por\s+favor|athena)\s+)?(?:quais\s+(?:sao\s+)?(?:as\s+|os\s+)?)?"
    r"(?:ver\s+|mostr[ae]\s+|listar?\s+)?(?:as\s+|os\s+|minhas\s+|meus\s+)?"
    r"(?:proximas?\s+|proximos?\s+)?(?:provas?|prazos?|atividades?)"
    r"(?:\s+(?:da\s+faculdade|do\s+ulife|no\s+ulife|pendentes))?$"
)
_RE_OBSIDIAN_GRAFO = re.compile(
    r"^(?:(?:por\s+favor|athena)\s+)?"
    r"(?:(?:ver\s+|mostr[ae]\s+|quais\s+(?:sao\s+)?(?:as\s+)?)?"
    r"(?:estatisticas?|metricas?|status|resumo|hubs)\s+(?:do\s+)?(?:grafo\s+(?:do\s+)?)?(?:obsidian|vault)|"
    r"(?:quais\s+)?(?:sao\s+)?(?:as\s+)?notas?\s+orfas?(?:\s+(?:do|no)\s+(?:obsidian|vault))?|"
    r"grafo\s+do\s+(?:obsidian|vault))$"
)


def _rota_ulife_cache(quer_provas: bool) -> dict[str, Any] | None:
    dados = obter_dados_ulife_estruturados("tudo")
    if not dados.get("sincronizado"):
        return None

    atualizado_em = str(dados.get("atualizado_em") or "-")
    if quer_provas:
        from ferramentas_estudos import listar_provas

        atividades = dados.get("atividades") or []
        provas_locais = listar_provas()
        linhas = [f"### Próximas Provas e Prazos (cache Ulife · {atualizado_em})"]
        if atividades:
            for at in atividades[:12]:
                dt = at.get("entrega_texto") or at.get("entrega_em") or "sem data"
                linhas.append(f"- **[{dt}]** {at.get('disciplina') or '-'} — {at.get('titulo') or 'Atividade'}")
        else:
            linhas.append("- Nenhuma atividade pendente registrada no cache do Ulife.")
        if provas_locais and "nenhuma prova" not in provas_locais.lower():
            linhas.append("\n**Agenda local de provas:**")
            linhas.append(provas_locais)
        return {
            "resposta": compactar_saida_tool("\n".join(linhas)),
            "rota": "estudos_ulife",
            "modelo": MODELO_FAST_PATH,
            "tokens": 0,
        }

    notas_faltas = dados.get("notas_faltas") or []
    if not notas_faltas:
        return None

    linhas = [f"### Notas e Frequência — Ulife (cache · {atualizado_em})"]
    for nf in notas_faltas:
        disc = nf.get("disciplina") or "Disciplina"
        nota = (
            f"{nf['nota_total']:.1f}" + (f"/{nf['peso_total']:.0f}" if nf.get("peso_total") else "")
            if nf.get("nota_total") is not None
            else "N/D"
        )
        extras: list[str] = []
        if nf.get("faltas") is not None:
            extras.append(f"Faltas: {nf['faltas']}" + (f"/{nf['max_faltas']}" if nf.get("max_faltas") else ""))
        if nf.get("frequencia_pct") is not None:
            extras.append(f"Freq: {nf['frequencia_pct']:.0f}%")
        avals = [
            f"{a.get('item') or a.get('nome') or 'Aval.'}={a['nota']:.1f}"
            for a in (nf.get("avaliacoes") or [])
            if a.get("nota") is not None
        ]
        if avals:
            extras.append("Avaliações: " + ", ".join(avals))
        sufixo = " · " + " | ".join(extras) if extras else ""
        linhas.append(f"- **{disc}:** Nota `{nota}`{sufixo}")

    return {
        "resposta": compactar_saida_tool("\n".join(linhas)),
        "rota": "estudos_ulife",
        "modelo": MODELO_FAST_PATH,
        "tokens": 0,
    }


def _rota_obsidian_grafo() -> dict[str, Any]:
    from grafo_obsidian import estatisticas_grafo

    st = estatisticas_grafo()
    linhas = [
        "### Estatísticas do Obsidian Vault (GraphRAG)",
        f"- **Notas indexadas:** {st['total_notas']}",
        f"- **Conexões (`[[wikilinks]]`):** {st['total_conexoes']}",
        f"- **Notas órfãs:** {st['total_orfas']}",
    ]
    hubs = st.get("top_hubs") or []
    if hubs:
        linhas.append("\n**Top Hubs mais conectados:**")
        for h in hubs:
            linhas.append(
                f"- `[[{h['nota']}]]` — grau **{h['grau']}** (entrada: {h['entrada']}, saída: {h['saida']})"
            )
    orfas = st.get("notas_orfas") or []
    if orfas:
        linhas.append("\n**Notas órfãs (sem links):**")
        for o in orfas[:15]:
            linhas.append(f"- `[[{o['nota']}]]` ({o['caminho']})")
        if len(orfas) > 15:
            linhas.append(f"- *... e mais {len(orfas) - 15} nota(s) órfã(s).*")
    return {
        "resposta": compactar_saida_tool("\n".join(linhas)),
        "rota": "obsidian_grafo",
        "modelo": MODELO_FAST_PATH,
        "tokens": 0,
    }


def tentar_fast_path(mensagem: str) -> dict[str, Any] | None:
    """Tenta resolver consultas pessoais curtas e inequivocas localmente (< 50 ms, 0 tokens).

    Retorna `dict` com `resposta`, `rota`, `modelo` e `tokens=0` quando der match,
    ou `None` para delegar transparentemente ao fluxo normal da Orquestradora LLM.
    """
    bruta = (mensagem or "").strip()
    if not bruta or len(bruta) > MAX_CHARS_FAST_PATH or "\n" in bruta:
        return None

    norm = normalizar_texto_eficiencia(bruta)
    norm_sem_pont = re.sub(r"[?!.,]+$", "", norm).strip()
    if not norm_sem_pont:
        return None

    if _RE_COMPOSTO_OU_ANALITICO.search(norm_sem_pont):
        return None

    # 1) Notas e proximas provas direto do cache local do Ulife
    if _RE_NOTAS_ULIFE.match(norm_sem_pont):
        return _rota_ulife_cache(quer_provas=False)
    if _RE_PROVAS_ULIFE.match(norm_sem_pont):
        return _rota_ulife_cache(quer_provas=True)

    # 2) Estatisticas do Grafo do Obsidian
    if _RE_OBSIDIAN_GRAFO.match(norm_sem_pont):
        return _rota_obsidian_grafo()

    return None
