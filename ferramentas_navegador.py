import asyncio
import contextlib
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from guardrails import sanitizar_conteudo_externo

PASTA_PRINTS = Path(__file__).resolve().parent / "sandbox_files" / "prints"
LIMITE_TEXTO = 8000
MAX_LINKS = 25
MAX_RESULTADOS_BUSCA = 25
TIMEOUT_NAVEGACAO_MS = 30000
TIMEOUT_NETWORKIDLE_MS = 8000


def _validar_url(url: str) -> str:
    bruta = (url or "").strip()
    if not bruta:
        raise ValueError("URL vazia.")
    if "://" not in bruta:
        bruta = f"https://{bruta}"
    parsed = urlparse(bruta)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL invalida. Informe um endereco http:// ou https:// valido.")
    return bruta


def _sanitizar_nome_arquivo(nome_arquivo: str) -> Path:
    base = Path((nome_arquivo or "").strip()).name
    if base.lower().endswith(".png"):
        base = base[:-4]
    limpo = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-")
    if not limpo:
        limpo = "screenshot"

    pasta = PASTA_PRINTS.resolve()
    destino = (pasta / f"{limpo}.png").resolve()
    if pasta not in destino.parents:
        raise ValueError("Nome de arquivo invalido para salvar screenshot.")
    return destino


def _limpar_texto(texto: str, limite: int = LIMITE_TEXTO) -> str:
    linhas = [re.sub(r"[ \t]+", " ", linha).strip() for linha in (texto or "").splitlines()]
    filtradas: list[str] = []
    vazia_anterior = False
    for linha in linhas:
        if not linha:
            if not vazia_anterior and filtradas:
                filtradas.append("")
            vazia_anterior = True
        else:
            filtradas.append(linha)
            vazia_anterior = False
    resultado = "\n".join(filtradas).strip()
    if len(resultado) > limite:
        return resultado[:limite] + "\n...[truncado]"
    return resultado


def _carregar_pagina(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAVEGACAO_MS)
    with contextlib.suppress(PlaywrightTimeoutError):
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_NETWORKIDLE_MS)


def _abrir_e_ler_pagina_sync(url: str) -> str:
    try:
        url_valida = _validar_url(url)
    except ValueError as e:
        return f"Erro ao validar URL: {e}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    locale="pt-BR",
                )
                page = context.new_page()
                _carregar_pagina(page, url_valida)

                titulo = (page.title() or "").strip() or "(sem titulo)"
                url_final = page.url

                dados = page.evaluate(
                    """(maxLinks) => {
                        const texto = document.body ? (document.body.innerText || '') : '';
                        const links = [];
                        const vistos = new Set();
                        for (const a of document.querySelectorAll('a[href]')) {
                            const href = a.href || '';
                            if (!href.startsWith('http://') && !href.startsWith('https://')) continue;
                            const style = window.getComputedStyle(a);
                            if (style.display === 'none' || style.visibility === 'hidden') continue;
                            const rotulo = (a.innerText || a.getAttribute('aria-label') || a.title || '')
                                .replace(/\\s+/g, ' ')
                                .trim();
                            if (!rotulo || vistos.has(href)) continue;
                            vistos.add(href);
                            links.push({ texto: rotulo.slice(0, 120), href });
                            if (links.length >= maxLinks) break;
                        }
                        return { texto, links };
                    }""",
                    MAX_LINKS,
                )
            finally:
                browser.close()

        texto_limpo = _limpar_texto(dados.get("texto", ""), limite=LIMITE_TEXTO)
        links = dados.get("links", [])
        bloco_links = (
            "\n".join(f"- {item['texto']}: {item['href']}" for item in links)
            if links
            else "(nenhum link principal encontrado)"
        )

        bruto = (
            f"Titulo: {titulo}\n"
            f"URL: {url_final}\n\n"
            f"--- Conteudo ---\n"
            f"{texto_limpo or '(sem texto visivel)'}\n\n"
            f"--- Links Principais ---\n"
            f"{bloco_links}"
        )
        sanitizado, _susp, _pads = sanitizar_conteudo_externo(bruto, origem=f"web:{url_final}")
        return sanitizado
    except Exception as e:
        return f"Erro ao abrir e ler pagina ({url_valida}): {e}"


def _tirar_screenshot_sync(url: str, nome_arquivo: str) -> str:
    try:
        url_valida = _validar_url(url)
        destino = _sanitizar_nome_arquivo(nome_arquivo)
    except ValueError as e:
        return f"Erro nos parametros do screenshot: {e}"

    try:
        PASTA_PRINTS.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    locale="pt-BR",
                )
                page = context.new_page()
                _carregar_pagina(page, url_valida)
                titulo = (page.title() or "").strip() or "(sem titulo)"
                url_final = page.url
                page.screenshot(path=str(destino), full_page=True)
            finally:
                browser.close()

        tamanho_kb = destino.stat().st_size // 1024
        return (
            f"Screenshot salvo com sucesso!\n"
            f"- Arquivo: sandbox_files/prints/{destino.name} (/workspace/prints/{destino.name})\n"
            f"- Tamanho: {tamanho_kb} KB\n"
            f"- Pagina: {titulo} ({url_final})"
        )
    except Exception as e:
        return f"Erro ao tirar screenshot ({url_valida}): {e}"


def _buscar_no_site_sync(url: str, texto_busca: str) -> str:
    try:
        url_valida = _validar_url(url)
    except ValueError as e:
        return f"Erro ao validar URL: {e}"

    termo = (texto_busca or "").strip()
    if not termo:
        return "Erro: informe um texto_busca nao vazio."

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    locale="pt-BR",
                )
                page = context.new_page()
                _carregar_pagina(page, url_valida)

                titulo = (page.title() or "").strip() or "(sem titulo)"
                url_final = page.url

                resultado = page.evaluate(
                    """({ termo, maxItens }) => {
                        const norm = (s) => (s || '')
                            .normalize('NFD')
                            .replace(/[\\u0300-\\u036f]/g, '')
                            .toLowerCase();
                        const alvo = norm(termo);

                        const visivel = (el) => {
                            const st = window.getComputedStyle(el);
                            if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') {
                                return false;
                            }
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 || rect.height > 0;
                        };

                        const links = [];
                        const linksVistos = new Set();
                        for (const a of document.querySelectorAll('a[href]')) {
                            if (!visivel(a)) continue;
                            const txt = (a.innerText || a.getAttribute('aria-label') || a.title || '')
                                .replace(/\\s+/g, ' ')
                                .trim();
                            const href = a.href || '';
                            if (norm(txt).includes(alvo) || norm(href).includes(alvo)) {
                                const chave = `${txt}|${href}`;
                                if (linksVistos.has(chave)) continue;
                                linksVistos.add(chave);
                                links.push({
                                    texto: (txt || '(sem texto)').slice(0, 140),
                                    href,
                                });
                                if (links.length >= maxItens) break;
                            }
                        }

                        const elementos = [];
                        const textosVistos = new Set();
                        const seletores = 'h1, h2, h3, h4, h5, button, li, p, td, th, dt, dd, span, label, article';
                        for (const el of document.querySelectorAll(seletores)) {
                            if (!visivel(el)) continue;
                            const txt = (el.innerText || el.getAttribute('aria-label') || '')
                                .replace(/\\s+/g, ' ')
                                .trim();
                            if (!txt || txt.length > 500) continue;
                            if (!norm(txt).includes(alvo)) continue;
                            const temFilhoIgual = Array.from(el.children).some((filho) => {
                                const ft = (filho.innerText || '').replace(/\\s+/g, ' ').trim();
                                return ft && norm(ft).includes(alvo);
                            });
                            if (temFilhoIgual) continue;
                            const chave = norm(txt);
                            if (textosVistos.has(chave)) continue;
                            textosVistos.add(chave);
                            elementos.push({
                                tag: el.tagName.toLowerCase(),
                                texto: txt.slice(0, 240),
                            });
                            if (elementos.length >= maxItens) break;
                        }

                        return { links, elementos };
                    }""",
                    {"termo": termo, "maxItens": MAX_RESULTADOS_BUSCA},
                )
            finally:
                browser.close()

        links = resultado.get("links", [])
        elementos = resultado.get("elementos", [])

        if not links and not elementos:
            return (
                f"Nenhum link ou elemento visivel encontrado para '{termo}' em:\n"
                f"Titulo: {titulo}\n"
                f"URL: {url_final}"
            )

        partes = [
            f"Busca por '{termo}' em: {titulo} ({url_final})",
        ]
        if links:
            partes.append(
                "\n--- Links encontrados ---\n"
                + "\n".join(f"- {item['texto']}: {item['href']}" for item in links)
            )
        if elementos:
            partes.append(
                "\n--- Elementos encontrados ---\n"
                + "\n".join(f"- <{item['tag']}> {item['texto']}" for item in elementos)
            )
        bruto = "\n".join(partes)
        sanitizado, _susp, _pads = sanitizar_conteudo_externo(bruto, origem=f"web:{url_final}")
        return sanitizado
    except Exception as e:
        return f"Erro ao buscar '{termo}' em ({url_valida}): {e}"


async def abrir_e_ler_pagina(url: str) -> str:
    """Abre a URL com Chromium headless, extrai titulo, texto visivel (ate 8000 chars) e links principais."""
    return await asyncio.to_thread(_abrir_e_ler_pagina_sync, url)


async def tirar_screenshot(url: str, nome_arquivo: str) -> str:
    """Abre a URL com Chromium headless e salva um screenshot PNG em sandbox_files/prints/<nome_arquivo>.png."""
    return await asyncio.to_thread(_tirar_screenshot_sync, url, nome_arquivo)


async def buscar_no_site(url: str, texto_busca: str) -> str:
    """Abre a URL e procura links e elementos visiveis contendo o texto informado."""
    return await asyncio.to_thread(_buscar_no_site_sync, url, texto_busca)
