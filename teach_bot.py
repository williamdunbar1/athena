"""Teach a Bot — Grava passos no navegador e gera automaticamente `skills/<slug>/SKILL.md`.

Seguranca obrigatoria:
- Valida a URL inicial contra `DOMINIOS_BLOQUEADOS_BROWSER_USE` (bloqueando bancos, corretoras e gov.br).
- Nunca grava valores de campos `input[type="password"]` ou nomes/atributos de senha (substitui por `[SENHA_OMITIDA]`).
- Protege a gravacao de `SKILL.md` contra path traversal dentro de `skills/`.
- Registra toda skill criada em `auditoria.registrar_atividade(...)`.
"""

from __future__ import annotations

import asyncio
import re
import threading
import unicodedata
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from auditoria import registrar_atividade
from ferramentas_navegador import _validar_url, verificar_dominio_bloqueado_browser_use
from guardrails import mascarar_segredos_e_pii
from skills_manager import SKILLS_DIR, listar_skills

MARCADOR_SENHA_OMITIDA = "[SENHA_OMITIDA]"
SESSOES_GRAVACAO: dict[str, dict[str, Any]] = {}
_GRAVACAO_LOCK = threading.Lock()
_RE_SLUG_INVALIDO = re.compile(r"[^a-z0-9_-]+")
_RE_CAMPO_SENHA = re.compile(r"(?:pass|senha|pwd|pin|secret|token|cvv)", re.IGNORECASE)

_JS_LISTENER_TEACH_BOT = """
(() => {
  if (window.__athenaTeachBotAtivo) return;
  window.__athenaTeachBotAtivo = true;

  const seletorCurto = (el) => {
    if (!el || !el.tagName) return "";
    const tag = el.tagName.toLowerCase();
    if (el.id) return `${tag}#${el.id}`;
    const aria = el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("name") || el.getAttribute("placeholder"));
    if (aria) return `${tag}[name/label="${aria.slice(0, 50)}"]`;
    const cls = (typeof el.className === "string" ? el.className.trim().split(/\\s+/).slice(0, 2).join(".") : "");
    return cls ? `${tag}.${cls}` : tag;
  };

  const ehCampoSenha = (el) => {
    if (!el) return false;
    const t = String(el.type || "").toLowerCase();
    if (t === "password") return true;
    const ac = String((el.getAttribute && el.getAttribute("autocomplete")) || "").toLowerCase();
    if (ac.includes("password")) return true;
    const nm = String((el.getAttribute && (el.getAttribute("name") || el.id || el.getAttribute("placeholder"))) || "").toLowerCase();
    return /pass|senha|pwd|pin|secret|token/.test(nm);
  };

  const enviar = (passo) => {
    try {
      if (typeof window.__athenaRegistrarPasso === "function") {
        window.__athenaRegistrarPasso(passo);
      }
    } catch (_) {}
  };

  document.addEventListener("click", (ev) => {
    const el = ev.target && ev.target.closest ? ev.target.closest("a,button,input,select,textarea,[role='button']") || ev.target : ev.target;
    if (!el) return;
    const texto = String(el.innerText || el.value || (el.getAttribute && el.getAttribute("aria-label")) || "").replace(/\\s+/g, " ").trim().slice(0, 100);
    enviar({
      tipo: "clique",
      seletor: seletorCurto(el),
      texto: ehCampoSenha(el) ? "[SENHA_OMITIDA]" : texto,
      url: location.href,
    });
  }, true);

  document.addEventListener("change", (ev) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    const tag = el.tagName.toLowerCase();
    if (!["input", "textarea", "select"].includes(tag)) return;
    const senha = ehCampoSenha(el);
    enviar({
      tipo: "preencher",
      tipo_input: String(el.type || tag).toLowerCase(),
      seletor: seletorCurto(el),
      valor: senha ? "[SENHA_OMITIDA]" : String(el.value || "").slice(0, 160),
      eh_senha: senha,
      url: location.href,
    });
  }, true);
})();
"""


def normalizar_slug_skill(nome_skill: str) -> str:
    """Normaliza nome livre para slug seguro (`a-z0-9_-`) bloqueando path traversal."""
    bruto = (nome_skill or "").strip()
    if not bruto or ".." in bruto or "/" in bruto or "\\" in bruto or "\x00" in bruto:
        raise ValueError(f"Nome de skill invalido ou inseguro: '{nome_skill}'.")
    sem_acento = unicodedata.normalize("NFKD", bruto).encode("ascii", "ignore").decode("ascii").lower()
    slug = _RE_SLUG_INVALIDO.sub("-", sem_acento).strip("-_")[:60]
    if not slug or not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", slug):
        raise ValueError(f"Nao foi possivel gerar um slug valido para a skill '{nome_skill}'.")
    return slug


def validar_url_teach_bot(url_inicial: str) -> str:
    """Valida a URL inicial do Teach a Bot e bloqueia dominios bancarios/corretoras/gov.br e loopback local."""
    url_valida = _validar_url(url_inicial)
    host = (urlparse(url_valida).hostname or "").strip().lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        raise ValueError(f"Gravacao bloqueada por seguranca: host local nao permitido ('{host}').")
    bloqueado = verificar_dominio_bloqueado_browser_use("", url_valida)
    if bloqueado is not None:
        raise ValueError(
            f"Gravacao bloqueada por seguranca: o dominio '{bloqueado}' faz parte da denylist "
            "(bancos, corretoras e portais governamentais)."
        )
    return url_valida


def sanitizar_passo_gravado(passo: dict[str, Any]) -> dict[str, Any]:
    """Garante que nenhum campo de senha ou segredo/PII seja gravado nos passos da Skill."""
    tipo = str(passo.get("tipo") or "acao").strip().lower()[:30]
    tipo_input = str(passo.get("tipo_input") or "").strip().lower()
    seletor = str(passo.get("seletor") or "").strip()[:160]
    url = str(passo.get("url") or "").strip()[:400]
    texto = str(passo.get("texto") or "").strip()[:180]
    valor = str(passo.get("valor") or "").strip()[:240]

    eh_senha = bool(passo.get("eh_senha")) or tipo_input == "password" or bool(_RE_CAMPO_SENHA.search(seletor))
    if eh_senha:
        valor = "[SENHA_OMITIDA]"
        if tipo == "preencher":
            texto = "[SENHA_OMITIDA]"
    else:
        valor, _ = mascarar_segredos_e_pii(valor)
        texto, _ = mascarar_segredos_e_pii(texto)

    url_seg, _ = mascarar_segredos_e_pii(url)
    return {
        "tipo": tipo,
        "tipo_input": tipo_input,
        "seletor": seletor,
        "texto": texto,
        "valor": valor,
        "url": url_seg,
    }


def gerar_skill_markdown(
    nome_skill: str,
    descricao: str,
    passos: list[dict[str, Any]],
    *,
    url_inicial: str = "",
) -> str:
    """Monta um `SKILL.md` limpo com frontmatter YAML (`name`, `description`) e passos numerados."""
    slug = normalizar_slug_skill(nome_skill)
    desc_limpa = (
        " ".join((descricao or "").split())
        or f"Procedimento ensinado pelo usuario via Teach a Bot ({nome_skill.strip()})."
    )[:220]
    desc_seg, _ = mascarar_segredos_e_pii(desc_limpa)

    linhas: list[str] = [
        "---",
        f"name: {slug}",
        f'description: "{desc_seg.replace(chr(34), chr(39))}"',
        "---",
        "",
        f"# Skill: {nome_skill.strip()}",
        "",
        f"> Gravada via **Teach a Bot** em {datetime.now().strftime('%Y-%m-%d %H:%M')}.",
    ]
    if url_inicial:
        linhas.append(f"> URL inicial: {url_inicial}")

    linhas.extend(["", "## Passos Gravados", ""])
    passos_sanitizados = [sanitizar_passo_gravado(p) for p in (passos or [])]
    if not passos_sanitizados:
        linhas.append("1. Abrir a URL inicial e inspecionar a pagina conforme objetivo do usuario.")
    else:
        for idx, p in enumerate(passos_sanitizados, start=1):
            t = p["tipo"]
            if t == "navegar":
                linhas.append(f"{idx}. Navegar para `{p['url']}`.")
            elif t == "clique":
                alvo = f'"{p["texto"]}" (`{p["seletor"]}`)' if p["texto"] else f"`{p['seletor']}`"
                linhas.append(f"{idx}. Clicar em {alvo}.")
            elif t == "preencher":
                linhas.append(f"{idx}. Preencher o campo `{p['seletor'] or p['tipo_input'] or 'input'}` com `{p['valor']}`.")
            else:
                resumo = p["texto"] or p["valor"] or p["seletor"] or p["url"]
                linhas.append(f"{idx}. Executar `{t}` em {resumo}.")

    linhas.extend(
        [
            "",
            "## Regras de Seguranca",
            "- Nunca preencher senhas automaticamente; quando encontrar `[SENHA_OMITIDA]`, login ou CAPTCHA, chame `pedir_ajuda_humana_navegador` (Take the wheel).",
            "- Nunca executar esta skill em dominios bancarios, corretoras ou portais governamentais bloqueados.",
            "",
        ]
    )
    return "\n".join(linhas)


def salvar_skill_gerada(
    nome_skill: str,
    descricao: str,
    passos: list[dict[str, Any]],
    *,
    url_inicial: str = "",
    skills_dir: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Salva `skills/<slug>/SKILL.md` com protecao contra path traversal, recarrega skills e audita."""
    slug = normalizar_slug_skill(nome_skill)
    base = (skills_dir or SKILLS_DIR).resolve()
    base.mkdir(parents=True, exist_ok=True)
    pasta_skill = (base / slug).resolve()
    if base not in pasta_skill.parents:
        raise ValueError("Caminho de skill bloqueado por seguranca (path traversal).")

    arquivo_md = (pasta_skill / "SKILL.md").resolve()
    if base not in arquivo_md.parents:
        raise ValueError("Arquivo SKILL.md fora da pasta de skills bloqueado.")

    conteudo = gerar_skill_markdown(nome_skill, descricao, passos, url_inicial=url_inicial)
    pasta_skill.mkdir(parents=True, exist_ok=True)
    arquivo_md.write_text(conteudo, encoding="utf-8")

    catalogo = listar_skills(base)
    with suppress(Exception):
        registrar_atividade(
            agente="Teach a Bot",
            acao="criar_skill",
            alvo=str(arquivo_md),
            status="sucesso",
            detalhes=f"Skill '{slug}' salva com {len(passos)} passo(s).",
            db_path=db_path,
        )

    return {
        "ok": True,
        "slug": slug,
        "caminho": str(arquivo_md),
        "total_passos": len(passos),
        "markdown": conteudo,
        "total_skills": len(catalogo),
    }


def _rodar_gravador_playwright_thread(sessao: dict[str, Any]) -> None:
    """Abre Chromium visivel em thread dedicada, grava eventos de navegacao/clique/input ate parar."""
    parar_event: threading.Event = sessao["parar_event"]
    passos: list[dict[str, Any]] = sessao["passos"]
    url_inicial: str = sessao["url_inicial"]

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 820}, locale="pt-BR")
                context.add_init_script(_JS_LISTENER_TEACH_BOT)
                page = context.new_page()

                def _registrar_js(passo_raw):
                    if not isinstance(passo_raw, dict):
                        return
                    limpo = sanitizar_passo_gravado(passo_raw)
                    with _GRAVACAO_LOCK:
                        if len(passos) < 120:
                            passos.append(limpo)

                page.expose_function("__athenaRegistrarPasso", _registrar_js)

                def _on_nav(frame):
                    if frame == page.main_frame:
                        u = str(frame.url or "")
                        if u.startswith(("http://", "https://")):
                            with _GRAVACAO_LOCK:
                                if not passos or passos[-1].get("tipo") != "navegar" or passos[-1].get("url") != u:
                                    passos.append(sanitizar_passo_gravado({"tipo": "navegar", "url": u}))

                page.on("framenavigated", _on_nav)
                page.goto(url_inicial, wait_until="domcontentloaded", timeout=30000)

                while not parar_event.is_set() and not page.is_closed():
                    with suppress(Exception):
                        page.wait_for_timeout(300)
            finally:
                with suppress(Exception):
                    browser.close()
    except Exception as exc:
        sessao["erro"] = str(exc)
    finally:
        sessao["ativa"] = False
        sessao["encerrada_event"].set()


async def iniciar_gravacao_skill(
    nome_skill: str,
    url_inicial: str,
    descricao: str = "",
    *,
    skills_dir: Path | None = None,
) -> dict[str, Any]:
    """Inicia uma sessao controlada de gravacao de Skill no Chromium (bloqueando bancos/corretoras)."""
    slug = normalizar_slug_skill(nome_skill)
    url_valida = validar_url_teach_bot(url_inicial)

    with _GRAVACAO_LOCK:
        for s in SESSOES_GRAVACAO.values():
            if s.get("ativa"):
                return {
                    "ok": False,
                    "erro": f"Ja existe uma gravacao ativa ('{s['nome_skill']}', id={s['id']}). Finalize-a primeiro.",
                }

        sessao_id = f"tb_{uuid.uuid4().hex[:10]}"
        sessao: dict[str, Any] = {
            "id": sessao_id,
            "nome_skill": nome_skill.strip(),
            "slug": slug,
            "descricao": (descricao or "").strip(),
            "url_inicial": url_valida,
            "skills_dir": skills_dir,
            "passos": [sanitizar_passo_gravado({"tipo": "navegar", "url": url_valida})],
            "ativa": True,
            "salva": False,
            "erro": "",
            "iniciada_em": datetime.now().isoformat(timespec="seconds"),
            "parar_event": threading.Event(),
            "encerrada_event": threading.Event(),
        }
        SESSOES_GRAVACAO[sessao_id] = sessao

    thread = threading.Thread(target=_rodar_gravador_playwright_thread, args=(sessao,), daemon=True)
    sessao["thread"] = thread
    thread.start()

    return {
        "ok": True,
        "sessao_id": sessao_id,
        "slug": slug,
        "url_inicial": url_valida,
        "mensagem": f"Gravacao iniciada para a skill '{slug}'. Navegue na janela aberta e clique em 'Finalizar e Salvar Skill' quando terminar.",
    }


async def finalizar_gravacao_skill(
    sessao_id: str,
    *,
    skills_dir: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Encerra a gravacao, fecha o navegador e salva o arquivo `skills/<slug>/SKILL.md`."""
    sessao = SESSOES_GRAVACAO.get(sessao_id)
    if sessao is None:
        return {"ok": False, "erro": f"Sessao de gravacao '{sessao_id}' nao encontrada."}

    sessao["parar_event"].set()
    encerrada_ev: threading.Event = sessao["encerrada_event"]
    await asyncio.to_thread(encerrada_ev.wait, 8.0)

    passos = list(sessao.get("passos") or [])
    res = salvar_skill_gerada(
        nome_skill=str(sessao["nome_skill"]),
        descricao=str(sessao.get("descricao") or ""),
        passos=passos,
        url_inicial=str(sessao.get("url_inicial") or ""),
        skills_dir=skills_dir or sessao.get("skills_dir"),
        db_path=db_path,
    )
    sessao["ativa"] = False
    sessao["salva"] = True
    return {"sessao_id": sessao_id, **res}


def obter_status_gravacao() -> dict[str, Any]:
    """Retorna o status da gravacao Teach a Bot ativa (se houver)."""
    with _GRAVACAO_LOCK:
        for s in reversed(list(SESSOES_GRAVACAO.values())):
            if s.get("ativa"):
                return {
                    "ok": True,
                    "ativa": True,
                    "sessao_id": s["id"],
                    "nome_skill": s["nome_skill"],
                    "slug": s["slug"],
                    "url_inicial": s["url_inicial"],
                    "total_passos": len(s.get("passos") or []),
                    "erro": str(s.get("erro") or ""),
                    "iniciada_em": s["iniciada_em"],
                }
    return {"ok": True, "ativa": False, "sessao_id": "", "total_passos": 0, "erro": ""}
