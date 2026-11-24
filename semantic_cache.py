"""Cache Semantico Local da Athena reutilizando o modelo FastEmbed de `rag_qdrant.py`.

Caracteristicas:
- Armazena embeddings L2-normalizados em RAM (thread-safe) com teto `max_itens=200`,
  `limiar_similaridade=0.94` e TTL configuravel via `ATHENA_SEMANTIC_CACHE_TTL` (padrao 900s = 15 min).
- Lazy-loading do `TextEmbedding` (`paraphrase-multilingual-MiniLM-L12-v2`) de `rag_qdrant.py`,
  com injecao opcional de `embedder` e `relogio` para testes 100% offline.
- Regras estritas em `eh_cacheavel(mensagem)` bloqueando mensagens curtas/contextuais,
  comandos com efeito colateral/acao e consultas de tempo real.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from eficiencia import normalizar_texto_eficiencia

MODELO_CACHE_SEMANTICO = "Cache Semântico (Local)"
MAX_ITENS_PADRAO = 200
LIMIAR_SIMILARIDADE_PADRAO = 0.94
TTL_SEGUNDOS_PADRAO = 900.0
MIN_CHARS_CACHEAVEL = 18

_PREFIXOS_CONTEXTO_IMEDIATO: tuple[str, ...] = (
    "e ",
    "isso",
    "esse ",
    "essa ",
    "aquilo",
    "explica melhor",
    "explique melhor",
    "faz de novo",
    "faca de novo",
    "repita",
    "repete",
    "continua",
    "continue",
    "mais detalhes",
    "detalhe melhor",
    "aprofunde",
)

_RE_COMANDOS_ACAO = re.compile(
    r"\b(?:abrir|abre|abra|mover|mova|move|apagar|apague|apaga|deletar|delete|"
    r"excluir|exclua|organizar|organize|organiza|limpar|limpe|limpa|faxina|"
    r"enviar|envie|envia|criar|crie|cria|agendar|agende|marcar|marque|"
    r"capturar|capture|captura|sincronizar|sincronize|sincroniza|arquivar|arquive|"
    r"remover|remova|salvar|salve|guardar|guarde|executar|execute|rodar|rode|"
    r"comprar|vender|propor|desenhar|desenhe|desenha)\b|"
    r"\b(?:gerar|gere|gera)\s+(?:uma\s+)?imagem\b"
)

_RE_TEMPO_REAL = re.compile(
    r"\b(?:agora|hoje|ontem|amanha|cotacao|cotacoes|logfire|dolar|bitcoin|btc|"
    r"noticias?|clima|temperatura|horario)\b|"
    r"\b(?:preco\s+atual|valor\s+atual|ultima\s+hora|ultimos?\s+minutos?|"
    r"tempo\s+real|neste\s+momento|nesse\s+momento|que\s+horas|hora\s+atual|"
    r"previsao\s+do\s+tempo|selic\s+atual)\b"
)

_PREFIXOS_RESPOSTA_ERRO: tuple[str, ...] = (
    "erro",
    "nao foi possivel",
    "não foi possível",
    "falha ao",
    "todos os modelos disponiveis",
    "no momento estou operando em **modo offline (ollama local)**",
)


def _ler_ttl_env() -> float:
    bruto = (os.getenv("ATHENA_SEMANTIC_CACHE_TTL", "") or "").strip()
    if not bruto:
        return TTL_SEGUNDOS_PADRAO
    try:
        val = float(bruto)
        if val > 0:
            return val
    except ValueError:
        pass
    return TTL_SEGUNDOS_PADRAO


def _normalizar_l2(vetor: Sequence[float]) -> list[float]:
    valores = [float(x) for x in vetor]
    norma = math.sqrt(sum(x * x for x in valores))
    if norma <= 0.0 or not math.isfinite(norma):
        return []
    return [x / norma for x in valores]


def _produto_escalar(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b, strict=False)))


def _embed_fastembed_lazy(texto: str) -> list[float]:
    """Reutiliza de forma lazy o mesmo `TextEmbedding` (`paraphrase-multilingual-MiniLM-L12-v2`) de `rag_qdrant.py`."""
    from rag_qdrant import _obter_modelo_denso, _para_dense_vector

    modelo = _obter_modelo_denso()
    return _para_dense_vector(next(iter(modelo.query_embed(texto))))


def eh_cacheavel(mensagem: str) -> bool:
    """Retorna True apenas para perguntas informacionais/conceituais seguras para cache semantico."""
    norm = normalizar_texto_eficiencia(mensagem)
    if len(norm) < MIN_CHARS_CACHEAVEL:
        return False

    if any(norm == pref.strip() or norm.startswith(pref) for pref in _PREFIXOS_CONTEXTO_IMEDIATO):
        return False

    if _RE_COMANDOS_ACAO.search(norm):
        return False

    return not _RE_TEMPO_REAL.search(norm)


def _resposta_eh_valida_para_cache(resposta: str) -> bool:
    limpa = (resposta or "").strip()
    if len(limpa) < 8:
        return False
    norm = normalizar_texto_eficiencia(limpa)
    return not any(norm.startswith(pref) for pref in _PREFIXOS_RESPOSTA_ERRO)


class SemanticCache:
    """Cache semantico em RAM baseado em similaridade de cosseno sobre embeddings normalizados."""

    def __init__(
        self,
        *,
        max_itens: int = MAX_ITENS_PADRAO,
        limiar_similaridade: float = LIMIAR_SIMILARIDADE_PADRAO,
        ttl_segundos: float | None = None,
        embedder: Callable[[str], Sequence[float]] | None = None,
        relogio: Callable[[], float] | None = None,
    ) -> None:
        self.max_itens = max(1, int(max_itens))
        self.limiar_similaridade = float(limiar_similaridade)
        self._ttl_explicit = float(ttl_segundos) if ttl_segundos is not None else None
        self._embedder = embedder
        self._relogio = relogio or time.monotonic
        self._lock = threading.RLock()
        self._itens: list[dict[str, Any]] = []
        self._hits = 0
        self._misses = 0

    @property
    def ttl_segundos(self) -> float:
        if self._ttl_explicit is not None:
            return self._ttl_explicit
        return _ler_ttl_env()

    def _expurgar_expirados_locked(self, agora: float) -> None:
        ttl = self.ttl_segundos
        self._itens = [item for item in self._itens if (agora - float(item["criado_em"])) <= ttl]

    def _gerar_vetor(self, mensagem: str) -> list[float]:
        fn = self._embedder or _embed_fastembed_lazy
        try:
            bruto = fn(mensagem)
            return _normalizar_l2(bruto)
        except Exception:
            return []

    def consultar(self, mensagem: str) -> dict[str, Any] | None:
        """Consulta o cache semantico; retorna hit com `tokens=0` se similaridade >= limiar dentro do TTL."""
        if not eh_cacheavel(mensagem):
            return None

        agora = float(self._relogio())
        with self._lock:
            self._expurgar_expirados_locked(agora)
            if not self._itens:
                self._misses += 1
                return None

        vec = self._gerar_vetor(mensagem)
        if not vec:
            with self._lock:
                self._misses += 1
            return None

        with self._lock:
            agora_busca = float(self._relogio())
            self._expurgar_expirados_locked(agora_busca)
            melhor_sim = -1.0
            melhor_item: dict[str, Any] | None = None
            for item in self._itens:
                sim = _produto_escalar(vec, item["vetor"])
                if sim > melhor_sim:
                    melhor_sim = sim
                    melhor_item = item

            if melhor_item is not None and melhor_sim >= self.limiar_similaridade:
                self._hits += 1
                return {
                    "resposta": str(melhor_item["resposta"]),
                    "similaridade": round(min(1.0, max(0.0, melhor_sim)), 4),
                    "modelo": MODELO_CACHE_SEMANTICO,
                    "tokens": 0,
                }

            self._misses += 1
            return None

    def salvar(self, mensagem: str, resposta: str) -> bool:
        """Armazena par `(mensagem, resposta)` se `eh_cacheavel` for True e a resposta nao for erro."""
        if not eh_cacheavel(mensagem) or not _resposta_eh_valida_para_cache(resposta):
            return False

        vec = self._gerar_vetor(mensagem)
        if not vec:
            return False

        agora = float(self._relogio())
        resp_limpa = resposta.strip()
        with self._lock:
            self._expurgar_expirados_locked(agora)

            # Evita duplicatas quase identicas atualizando a entrada existente
            for item in self._itens:
                if _produto_escalar(vec, item["vetor"]) >= 0.995:
                    item["resposta"] = resp_limpa
                    item["criado_em"] = agora
                    item["mensagem"] = mensagem.strip()[:300]
                    return True

            self._itens.append(
                {
                    "vetor": vec,
                    "resposta": resp_limpa,
                    "criado_em": agora,
                    "mensagem": mensagem.strip()[:300],
                }
            )
            if len(self._itens) > self.max_itens:
                excesso = len(self._itens) - self.max_itens
                del self._itens[:excesso]
            return True

    def limpar(self) -> int:
        with self._lock:
            removidos = len(self._itens)
            self._itens.clear()
            self._hits = 0
            self._misses = 0
            return removidos

    def estatisticas(self) -> dict[str, Any]:
        agora = float(self._relogio())
        with self._lock:
            self._expurgar_expirados_locked(agora)
            return {
                "hits": self._hits,
                "misses": self._misses,
                "itens_ativos": len(self._itens),
                "max_itens": self.max_itens,
                "ttl_segundos": self.ttl_segundos,
                "limiar_similaridade": self.limiar_similaridade,
            }


_CACHE_PADRAO = SemanticCache()


def consultar_cache_semantico(mensagem: str) -> dict[str, Any] | None:
    """Consulta o cache semantico global da Athena."""
    return _CACHE_PADRAO.consultar(mensagem)


def salvar_cache_semantico(mensagem: str, resposta: str) -> bool:
    """Salva uma resposta valida no cache semantico global da Athena."""
    return _CACHE_PADRAO.salvar(mensagem, resposta)


def limpar_cache_semantico() -> int:
    """Limpa todas as entradas e contadores do cache semantico global."""
    return _CACHE_PADRAO.limpar()


def estatisticas_cache_semantico() -> dict[str, Any]:
    """Retorna metricas (`hits`, `misses`, `itens_ativos`) do cache semantico global."""
    return _CACHE_PADRAO.estatisticas()
