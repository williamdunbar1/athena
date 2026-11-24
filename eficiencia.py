"""Otimizacao de tokens e eficiencia para a Athena:

1. Filtragem Dinamica de Tools (`selecionar_dominios_para_mensagem`, `filtrar_tool_definitions`):
   - Classificador deterministico ultraleve (< 1 ms, sem chamada de LLM).
   - Saudacoes/mensagens triviais recebem 0 tools (`set()` -> `[]`).
   - Pedidos focados recebem apenas os dominios relevantes (ordenados alfabeticamente para Prompt Cache).
   - Pedidos multi-dominio (2+ dominios) incluem automaticamente `"paralelo"`.
   - Pedidos abertos/ambiguos retornam `None` (fallback seguro: libera todas as tools ordenadas).

2. Orcamento de Saida de Tools (`compactar_saida_tool`):
   - Limpa excesso de espacos e linhas em branco.
   - Impõe teto estrito de `max_chars` (padrao 2800), preservando cabecalho + trecho final
     com aviso curto `[... truncado para economia de tokens ...]`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

AVISO_TRUNCAMENTO_TOKENS = "\n[... truncado para economia de tokens ...]\n"
MAX_CHARS_SAIDA_TOOL_PADRAO = 2800

_RE_ESPACOS_LINHA = re.compile(r"[ \t]{2,}")
_RE_QUEBRAS_EXCESSO = re.compile(r"\n{3,}")
_RE_PONTUACAO_SAUDACAO = re.compile(r"[^\w\s]", re.UNICODE)

# Mapeamento de dominios -> nomes das ferramentas registradas na Orquestradora.
DOMINIO_PARA_TOOLS: dict[str, frozenset[str]] = {
    "estudos": frozenset(
        {
            "buscar_pdfs_faculdade_direto",
            "buscar_semantico",
            "consultar_resumo_ulife_direto",
            "delegar_para_estudos",
            "escrever_nota_vault",
            "ler_transcricao_youtube",
            "search_tools",
        }
    ),
    "investimentos": frozenset(
        {
            "delegar_para_investimentos",
            "escrever_nota_vault",
        }
    ),
    "google": frozenset(
        {
            "delegar_para_google",
            "listar_agenda_direto",
            "listar_emails_direto",
        }
    ),
    "pesquisa": frozenset(
        {
            "delegar_para_pesquisador",
        }
    ),
    "obsidian": frozenset(
        {
            "buscar_no_vault",
            "buscar_obsidian_direto",
            "buscar_semantico",
            "escrever_nota_vault",
            "explorar_grafo_obsidian",
            "ler_nota_vault",
            "listar_notas_vault",
            "search_tools",
        }
    ),
    "paralelo": frozenset(
        {
            "consultar_especialistas_em_paralelo",
            "delegar_para_analista",
        }
    ),
    "memoria": frozenset(
        {
            "consultar_memoria_temporal_tool",
            "guardar_na_memoria",
        }
    ),
    "rotinas": frozenset(
        {
            "criar_rotina_agendada_tool",
            "listar_rotinas_tool",
            "pausar_rotina_tool",
        }
    ),
}

# Padroes compilados por dominio (aplicados sobre texto normalizado sem acento e minusculo).
_PADROES_DOMINIO: dict[str, tuple[re.Pattern[str], ...]] = {
    "estudos": (
        re.compile(
            r"\b(?:ulife|faculdade|provas?|notas?|pdfs?|calculo|fisica|materias?|"
            r"disciplinas?|aulas?|videoaulas?|youtube|transcric(?:ao|oes)|simulados?|"
            r"estudos?|estudar|faltas|frequencia|semestre|a1|a2|a3)\b"
        ),
        re.compile(r"\bplano\s+de\s+estudos?\b|youtu\.be"),
    ),
    "investimentos": (
        re.compile(
            r"\b(?:carteira|acao|acoes|fiis?|selic|cdi|extratos?|investir|"
            r"investimentos?|dolar|cotacao|cotacoes|aportes?|bolsa|ibovespa|"
            r"dividendos?|tesouro|cdb|cripto|bitcoin|btc|patrimonio|financas|"
            r"financeir[ao]s?|economia|economico|orcamento|gastos?|juros)\b"
        ),
        re.compile(r"\b(?:mercado\s+pago|renda\s+fixa|paper\s+trading)\b"),
    ),
    "google": (
        re.compile(
            r"\b(?:gmail|emails?|e-mails?|agenda|calendario|eventos?|drive|"
            r"tarefas?|tasks?|docs|sheets|planilhas?|slides|apresentacao|"
            r"compromissos?|reuniao|reunioes|rascunhos?)\b"
        ),
        re.compile(r"\bgoogle\s+(?:doc|docs|sheet|sheets|slides|drive|tasks|agenda)\b"),
    ),
    "pesquisa": (
        re.compile(
            r"\b(?:pesquisar|pesquise|pesquisa|noticias?|sites?|links?|internet|"
            r"duckduckgo|wikipedia)\b"
        ),
        re.compile(r"\b(?:buscar|busca|procurar|procure)\s+na\s+web\b"),
    ),
    "obsidian": (
        re.compile(
            r"\b(?:obsidian|notas?|vault|grafo|wikilinks?|diario|orfas?|"
            r"hubs?|markdown)\b"
        ),
        re.compile(r"\bsegundo\s+cerebro\b"),
    ),
    "memoria": (
        re.compile(r"\b(?:memorizar|memorize|memoriza|lembra|lembre|lembrar)\b"),
        re.compile(r"\b(?:guard(?:a|ar|e)|salv(?:a|ar|e)|anot(?:a|ar|e))\s+na\s+memoria\b"),
        re.compile(r"\b(?:memoria\s+temporal|linha\s+do\s+tempo|o\s+que\s+mudou|minhas?\s+metas?|preferencias?)\b"),
    ),
    "rotinas": (
        re.compile(r"\b(?:rotinas?|agendar|agendad[ao]s?|cron)\b"),
        re.compile(r"\b(?:todo\s+dia|todos\s+os\s+dias|toda\s+semana|lembrete\s+recorrente|pausar\s+rotina)\b"),
    ),
}

_VERBOS_ACAO = re.compile(
    r"\b(?:faca|faz|fazer|cria|crie|criar|gera|gere|gerar|mostra|mostre|mostrar|"
    r"lista|liste|listar|busca|busque|buscar|procura|procure|procurar|"
    r"explica|explique|explicar|resume|resuma|resumir|calcula|calcule|calcular|"
    r"analisa|analise|analisar|escreva|escreve|escrever|leia|ler|verifica|verifique|"
    r"verificar|checa|cheque|checar|envia|envie|enviar|salva|salve|salvar|"
    r"roda|rode|rodar|executa|execute|executar|abre|abra|abrir|ajuda|ajude|ajudar|"
    r"monte|monta|montar|compare|compara|comparar|traduza|traduz|traduzir|"
    r"revise|revisa|revisar|corrige|corrija|corrigir)\b"
)

_PALAVRAS_SAUDACAO_TRIVIAL: frozenset[str] = frozenset(
    {
        "oi",
        "ola",
        "opa",
        "eae",
        "eai",
        "e",
        "ai",
        "fala",
        "salve",
        "hey",
        "hi",
        "hello",
        "bom",
        "boa",
        "dia",
        "tarde",
        "noite",
        "tudo",
        "td",
        "bem",
        "certo",
        "tranquilo",
        "como",
        "vai",
        "vc",
        "voce",
        "esta",
        "ta",
        "obrigado",
        "obrigada",
        "obg",
        "valeu",
        "vlw",
        "tmj",
        "brigadao",
        "agradeco",
        "muito",
        "demais",
        "ok",
        "okay",
        "beleza",
        "blz",
        "show",
        "legal",
        "perfeito",
        "otimo",
        "entendi",
        "combinado",
        "fechado",
        "teste",
        "testando",
        "test",
        "ping",
        "athena",
        "tchau",
        "ate",
        "mais",
        "logo",
        "flw",
        "falou",
        "sim",
        "nao",
        "pode",
        "deixar",
    }
)


def normalizar_texto_eficiencia(texto: str) -> str:
    """Remove acentos, converte para minusculas e normaliza espacos."""
    sem_acento = (
        unicodedata.normalize("NFKD", (texto or "").strip())
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return " ".join(sem_acento.split())


def extrair_mensagem_atual_prompt(prompt: Any) -> str:
    """Extrai o texto do pedido atual a partir de `ctx.prompt` (suporta str ou lista multimodal)."""
    if prompt is None:
        return ""
    if isinstance(prompt, str):
        texto = prompt
    elif isinstance(prompt, Sequence):
        partes_txt = [p for p in prompt if isinstance(p, str)]
        texto = "\n".join(partes_txt)
    else:
        texto = str(prompt)

    marcador = "[PEDIDO ATUAL DO USUARIO]"
    if marcador in texto:
        texto = texto.rsplit(marcador, 1)[-1]
    return texto.strip()


def _eh_saudacao_trivial(texto_norm: str) -> bool:
    if not texto_norm or len(texto_norm) > 64:
        return False
    if _VERBOS_ACAO.search(texto_norm):
        return False
    limpo = _RE_PONTUACAO_SAUDACAO.sub(" ", texto_norm)
    tokens = [tok for tok in limpo.split() if tok]
    if not tokens or len(tokens) > 8:
        return False
    return all(tok in _PALAVRAS_SAUDACAO_TRIVIAL for tok in tokens)


def selecionar_dominios_para_mensagem(mensagem: str) -> set[str] | None:
    """Classificador deterministico ultraleve para selecao de dominios de ferramentas.

    Retorna:
    - `set()`: saudacao ou conversa trivial curta sem verbos de acao (0 tools).
    - `set[str]`: conjunto de dominios detectados (adiciona `"paralelo"` quando houver 2+ dominios).
    - `None`: mensagem aberta/ambigua ou vazia (fallback seguro: libera todas as tools).
    """
    texto_norm = normalizar_texto_eficiencia(mensagem)
    if not texto_norm:
        return None

    dominios: set[str] = set()
    for dominio, padroes in _PADROES_DOMINIO.items():
        if any(pat.search(texto_norm) for pat in padroes):
            dominios.add(dominio)

    # Desambiguacao fina: "nota"/"notas" em contexto puramente academico nao precisa puxar Obsidian
    # (ex.: "minhas notas da faculdade"), e em contexto puramente Obsidian nao precisa puxar Estudos.
    if "estudos" in dominios and "obsidian" in dominios:
        tem_sinal_obsidian_forte = bool(
            re.search(
                r"\b(?:obsidian|vault|grafo|wikilinks?|diario|orfas?|hubs?|markdown)\b|segundo\s+cerebro",
                texto_norm,
            )
        )
        tem_sinal_estudos_forte = bool(
            re.search(
                r"\b(?:ulife|faculdade|provas?|pdfs?|calculo|fisica|materias?|disciplinas?|"
                r"aulas?|simulados?|estudos?|estudar|faltas|frequencia|semestre|a1|a2|a3)\b",
                texto_norm,
            )
        )
        if tem_sinal_estudos_forte and not tem_sinal_obsidian_forte:
            dominios.discard("obsidian")
        elif tem_sinal_obsidian_forte and not tem_sinal_estudos_forte:
            dominios.discard("estudos")

    if dominios:
        if len(dominios) >= 2:
            dominios.add("paralelo")
        return dominios

    if _eh_saudacao_trivial(texto_norm):
        return set()

    return None


def _nome_tool(tool_def: Any) -> str:
    if isinstance(tool_def, str):
        return tool_def
    return str(getattr(tool_def, "name", "") or "")


def filtrar_tool_definitions[T](mensagem: str, tool_defs: list[T]) -> list[T]:
    """Filtra `ToolDefinition` conforme `selecionar_dominios_para_mensagem` e ordena por `tool.name`.

    A ordenacao alfabetica garante ordem deterministica dos schemas enviados ao modelo,
    maximizando a taxa de acerto do Prompt Cache.
    """
    lista = list(tool_defs or [])
    dominios = selecionar_dominios_para_mensagem(mensagem)

    if dominios is None:
        return sorted(lista, key=_nome_tool)

    if not dominios:
        return []

    permitidas: set[str] = set()
    for dom in dominios:
        permitidas.update(DOMINIO_PARA_TOOLS.get(dom, ()))

    filtradas = [td for td in lista if _nome_tool(td) in permitidas]
    return sorted(filtradas, key=_nome_tool)


def compactar_saida_tool(texto: str, max_chars: int = MAX_CHARS_SAIDA_TOOL_PADRAO) -> str:
    """Normaliza espacos/linhas em branco excessivas e impoe teto estrito de `max_chars`.

    Quando truncar, preserva cabecalho + trecho final com aviso curto de economia de tokens.
    """
    bruto = str(texto or "")
    if not bruto:
        return ""

    teto = max(1, int(max_chars))
    normalizado = bruto.replace("\r\n", "\n").replace("\r", "\n")
    linhas_limpas: list[str] = []
    for linha in normalizado.split("\n"):
        if not linha.strip():
            linhas_limpas.append("")
            continue
        # Preserva recuo curto (ate 2 espacos) e colapsa espacos repetidos internos
        recuo = "  " if linha.startswith("  ") else (" " if linha.startswith(" ") else "")
        miolo = _RE_ESPACOS_LINHA.sub(" ", linha.strip())
        linhas_limpas.append(f"{recuo}{miolo}")

    limpo = _RE_QUEBRAS_EXCESSO.sub("\n\n", "\n".join(linhas_limpas)).strip()
    if len(limpo) <= teto:
        return limpo

    aviso = AVISO_TRUNCAMENTO_TOKENS
    if teto <= len(aviso) + 20:
        aviso_curto = "[... truncado ...]"
        if teto <= len(aviso_curto):
            return limpo[:teto]
        return limpo[: teto - len(aviso_curto)].rstrip() + aviso_curto

    disponivel = teto - len(aviso)
    corte_inicio = max(1, int(disponivel * 0.65))
    corte_fim = max(0, disponivel - corte_inicio)

    cabecalho = limpo[:corte_inicio].rstrip()
    final = limpo[-corte_fim:].lstrip() if corte_fim > 0 else ""

    resultado = f"{cabecalho}{aviso}{final}" if final else f"{cabecalho}{aviso.rstrip()}"

    if len(resultado) > teto:
        resultado = resultado[:teto]
    return resultado
