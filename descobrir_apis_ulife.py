"""Descobre endpoints JSON reais do Portal Ulife/Anima enquanto voce navega normalmente.

Uso:
    uv run --env-file .env python descobrir_apis_ulife.py

Nunca grava headers Authorization, Cookie ou senhas.
Salva os endpoints unicos (method + path) em apis_ulife_descobertas.json ao fechar o navegador.
"""

import contextlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

_PASTA_FACULDADE = Path(
    os.getenv("PASTA_FACULDADE", "").strip() or (Path.home() / "Documents" / "agente-faculdade")
)
PASTA_PERFIL = _PASTA_FACULDADE / "perfil_navegador"
ARQUIVO_SAIDA = Path(__file__).resolve().parent / "apis_ulife_descobertas.json"
URL_INICIAL = "https://student.ulife.com.br/"
DOMINIOS_ALVO = ("ulife", "anima")
PREVIEW_CHARS = 600


def _eh_dominio_alvo(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return any(tok in host for tok in DOMINIOS_ALVO)


def _chaves_principais(dados) -> list[str]:
    if isinstance(dados, dict):
        return [str(k) for k in list(dados.keys())[:30]]
    if isinstance(dados, list) and dados:
        if isinstance(dados[0], dict):
            return [str(k) for k in list(dados[0].keys())[:30]]
        return [f"list[{len(dados)}]"]
    return []


def _salvar_descobertas(descobertas: dict[str, dict]) -> None:
    ordenado = [descobertas[k] for k in sorted(descobertas.keys())]
    ARQUIVO_SAIDA.write_text(
        json.dumps(ordenado, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    PASTA_PERFIL.mkdir(parents=True, exist_ok=True)
    descobertas: dict[str, dict] = {}

    if ARQUIVO_SAIDA.is_file():
        try:
            existentes = json.loads(ARQUIVO_SAIDA.read_text(encoding="utf-8-sig"))
            if isinstance(existentes, list):
                for item in existentes:
                    if isinstance(item, dict) and item.get("method") and item.get("url"):
                        path = urlparse(item["url"]).path or "/"
                        descobertas[f"{item['method'].upper()} {path}"] = item
        except Exception:
            pass

    print("=== DESCOBRIDOR DE APIS JSON DO ULIFE / ANIMA ===")
    print(f"Perfil: {PASTA_PERFIL}")
    print(f"Saida:  {ARQUIVO_SAIDA}")
    print("Navegue normalmente pelo Ulife (notas, faltas, disciplinas, mensagens, etc.).")
    print("Feche a janela do navegador quando terminar para concluir e salvar.\n")

    def conectar_pagina(page):
        def ao_responder(resp):
            try:
                if resp.status != 200:
                    return
                url = resp.url or ""
                if not _eh_dominio_alvo(url):
                    return
                ctype = (resp.headers.get("content-type") or "").lower()
                if "application/json" not in ctype:
                    return

                method = (resp.request.method or "GET").upper()
                path = urlparse(url).path or "/"
                chave = f"{method} {path}"
                if chave in descobertas:
                    return

                raw = resp.body()
                if not raw:
                    return
                texto = raw.decode("utf-8", errors="replace").strip()
                if not texto:
                    return

                chaves: list[str] = []
                try:
                    parsed = json.loads(texto)
                    chaves = _chaves_principais(parsed)
                except Exception:
                    pass

                # Nunca grava headers Authorization, Cookie ou senhas
                registro = {
                    "method": method,
                    "url": url,
                    "pagina_origem": page.url or "",
                    "tamanho_bytes": len(raw),
                    "chaves_principais": chaves,
                    "preview": texto[:PREVIEW_CHARS],
                }
                descobertas[chave] = registro
                _salvar_descobertas(descobertas)

                chaves_txt = ", ".join(chaves[:6]) if chaves else "-"
                print(f"[API] {method} {path} ({len(raw)} B) | chaves: [{chaves_txt}]")
            except Exception:
                pass

        page.on("response", ao_responder)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PASTA_PERFIL),
            headless=False,
            viewport={"width": 1366, "height": 860},
            locale="pt-BR",
        )
        fechado = {"flag": False}
        context.on("close", lambda _ctx=None: fechado.__setitem__("flag", True))
        context.on("page", conectar_pagina)

        page = context.pages[0] if context.pages else context.new_page()
        conectar_pagina(page)
        with contextlib.suppress(Exception):
            page.goto(URL_INICIAL, wait_until="domcontentloaded", timeout=30000)

        try:
            while not fechado["flag"] and context.pages:
                try:
                    context.pages[0].wait_for_timeout(500)
                except Exception:
                    break
        except KeyboardInterrupt:
            print("\nInterrompido pelo usuario.")
        finally:
            with contextlib.suppress(Exception):
                context.close()

    _salvar_descobertas(descobertas)
    print(f"\n[OK] {len(descobertas)} endpoint(s) JSON unico(s) salvo(s) em: {ARQUIVO_SAIDA}")


if __name__ == "__main__":
    main()
