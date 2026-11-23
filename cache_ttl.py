"""Cache leve em memoria (RAM) com TTL em segundos usando apenas stdlib.

Suporta:
- Decorador `@cache_ttl(ttl_segundos=300)` thread-safe com `time.monotonic()`.
- Bypass sob demanda passando `forcar=True` na chamada da funcao decorada.
- Metodos auxiliares `.limpar_cache()` e `.invalidar_cache(*args, **kwargs)`.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from functools import wraps
from typing import Any


class CacheTTL:
    """Estrutura simples de cache em RAM thread-safe com expiracao por TTL."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dados: dict[Any, tuple[float, Any]] = {}

    def get(self, chave: Any) -> tuple[bool, Any]:
        agora = time.monotonic()
        with self._lock:
            item = self._dados.get(chave)
            if item is None:
                return False, None
            expira_em, valor = item
            if agora >= expira_em:
                self._dados.pop(chave, None)
                return False, None
            return True, valor

    def set(self, chave: Any, valor: Any, ttl_segundos: float) -> None:
        expira_em = time.monotonic() + max(0.0, float(ttl_segundos))
        with self._lock:
            self._dados[chave] = (expira_em, valor)

    def invalidar(self, chave: Any) -> None:
        with self._lock:
            self._dados.pop(chave, None)

    def limpar(self) -> None:
        with self._lock:
            self._dados.clear()

    def tamanho(self) -> int:
        agora = time.monotonic()
        with self._lock:
            expiradas = [k for k, (exp, _) in self._dados.items() if agora >= exp]
            for k in expiradas:
                self._dados.pop(k, None)
            return len(self._dados)


def _clonar_se_mutavel(valor: Any) -> Any:
    if isinstance(valor, dict):
        return dict(valor)
    if isinstance(valor, list):
        return list(valor)
    return valor


def _chave_padrao(args: tuple, kwargs: dict) -> tuple:
    itens_kw = tuple(sorted((k, v) for k, v in kwargs.items() if k != "forcar"))
    return (args, itens_kw)


def cache_ttl(
    ttl_segundos: float = 300.0,
    *,
    chave_fn: Callable[..., Any] | None = None,
    armazenar_se: Callable[[Any], bool] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorador de cache em RAM com expiracao em segundos e suporte a `forcar=True`."""

    def _decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        cache = CacheTTL()
        sig = inspect.signature(func)
        aceita_forcar = "forcar" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )

        def _construir_chave(args: tuple, kwargs: dict) -> Any:
            kw_limpo = {k: v for k, v in kwargs.items() if k != "forcar"}
            if chave_fn is not None:
                return chave_fn(*args, **kw_limpo)
            return _chave_padrao(args, kw_limpo)

        @wraps(func)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            forcar = bool(kwargs.get("forcar", False))
            chave = _construir_chave(args, kwargs)

            if not forcar:
                hit, valor_cache = cache.get(chave)
                if hit:
                    return _clonar_se_mutavel(valor_cache)

            kw_chamada = kwargs if aceita_forcar else {k: v for k, v in kwargs.items() if k != "forcar"}
            resultado = func(*args, **kw_chamada)
            if armazenar_se is None or armazenar_se(resultado):
                cache.set(chave, _clonar_se_mutavel(resultado), ttl_segundos)
            return _clonar_se_mutavel(resultado)

        def _limpar_cache() -> None:
            cache.limpar()

        def _invalidar_cache(*args: Any, **kwargs: Any) -> None:
            cache.invalidar(_construir_chave(args, kwargs))

        _wrapper.cache = cache  # type: ignore[attr-defined]
        _wrapper.limpar_cache = _limpar_cache  # type: ignore[attr-defined]
        _wrapper.invalidar_cache = _invalidar_cache  # type: ignore[attr-defined]
        return _wrapper

    return _decorator
