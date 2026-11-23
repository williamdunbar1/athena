"""Roteador Fast-Path deterministico (Zero-LLM) da Athena.

Intercepta comandos operacionais curtos, diretos e inequivocos (< 50 ms, 0 tokens de API):
1. `windows_url`: abrir URLs ou sites conhecidos no navegador (com card de permissao inline).
2. `windows_app`: abrir aplicativos conhecidos no Windows (card inline ou whitelist 'Sempre permitir').
3. `estudos_ulife`: consultar notas, faltas ou proximas provas direto do cache local do Ulife no SQLite.
4. `status_pc`: coletar uso de CPU, RAM e disco localmente sem chamadas externas.
5. `skills_lista` e `obsidian_grafo`: listar Agent Skills reais e metricas do GraphRAG do Obsidian.

Qualquer mensagem aberta, longa (> 100 chars) ou composta retorna `None`, devolvendo
o controle de forma 100% transparente ao fluxo normal da Orquestradora LLM.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ferramentas_windows as fw
from eficiencia import compactar_saida_tool, normalizar_texto_eficiencia
from ferramentas_estudos import listar_provas
from grafo_obsidian import estatisticas_grafo
from skills_manager import listar_skills
from ulife_v2 import obter_dados_ulife_estruturados

MODELO_FAST_PATH = "Fast-Path (Local)"
MAX_CHARS_FAST_PATH = 100

_SITES_POPULARES: dict[str, tuple[str, str]] = {
    "youtube": ("YouTube", "https://www.youtube.com"),
    "github": ("GitHub", "https://github.com"),
    "gmail": ("Gmail", "https://mail.google.com"),
    "google": ("Google", "https://www.google.com"),
    "ulife": ("Ulife", "https://student.ulife.com.br"),
    "chatgpt": ("ChatGPT", "https://chatgpt.com"),
    "gemini": ("Gemini", "https://gemini.google.com"),
    "mercado pago": ("Mercado Pago", "https://www.mercadopago.com.br"),
    "whatsapp": ("WhatsApp Web", "https://web.whatsapp.com"),
    "whatsapp web": ("WhatsApp Web", "https://web.whatsapp.com"),
    "google drive": ("Google Drive", "https://drive.google.com"),
    "drive": ("Google Drive", "https://drive.google.com"),
    "google agenda": ("Google Agenda", "https://calendar.google.com"),
    "google docs": ("Google Docs", "https://docs.google.com"),
    "reddit": ("Reddit", "https://www.reddit.com"),
    "linkedin": ("LinkedIn", "https://www.linkedin.com"),
    "netflix": ("Netflix", "https://www.netflix.com"),
    "twitch": ("Twitch", "https://www.twitch.tv"),
}

_RE_COMPOSTO_OU_ANALITICO = re.compile(
    r"(?:;|\be\s+(?:depois|tambem|faz|faca|cria|crie|gera|gere|analis[ae]|resum[aeo]|"
    r"compar[ae]|busca|busque|pesquis[ae]|salv[ae]|envi[ae]|me\s+|abre|abrir|abra)\b|"
    r"\b(?:alem\s+disso|depois\s+me|comparando|analis[ae]|resum[aeo]|expliqu[ae]|explic[ae]|"
    r"cri[ae]\s+um|mont[ae]\s+um|planej[ae]|escrev[ae]|redig[ae]|traduz[ae]|otimiz[ae]|"
    r"refator[ae]|simulado|plano\s+de\s+estudo|sincroniz[ae]|atualiz[ae])\b)"
)

_RE_ABRIR_RAW = re.compile(
    r"^\s*(?:(?:por\s+favor|athena|pode|pfv)\s*,?\s*)*"
    r"(?:abre|abrir|abra)\s+(?:pra\s+mim\s+)?(.+?)\s*[?!.]*$",
    re.IGNORECASE,
)
_RE_URL_EXPLICITA = re.compile(r"^(https?://\S+)(?:\s+no\s+(brave|chrome|edge|firefox))?$", re.IGNORECASE)
_RE_DOMINIO_DIRETO = re.compile(
    r"^([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?)"
    r"(?:\s+no\s+(brave|chrome|edge|firefox))?$",
    re.IGNORECASE,
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
_RE_STATUS_PC = re.compile(
    r"^(?:(?:por\s+favor|athena)\s+)?(?:qual\s+(?:e\s+)?(?:o\s+)?)?"
    r"(?:ver\s+|mostr[ae]\s+|checa(?:r)?\s+)?"
    r"(?:status\s+(?:do\s+pc|do\s+sistema|da\s+maquina)|"
    r"uso\s+de\s+(?:cpu|ram|memoria|disco)|"
    r"(?:cpu|ram|memoria)\s+(?:e\s+(?:ram|cpu|disco|memoria))?|"
    r"recursos\s+do\s+pc|saude\s+do\s+pc)$"
)
_RE_SKILLS = re.compile(
    r"^(?:(?:por\s+favor|athena)\s+)?"
    r"(?:(?:quais\s+(?:sao\s+)?(?:as\s+)?)?(?:suas\s+|as\s+)?skills(?:\s+(?:voce\s+tem|vc\s+tem|disponiveis|da\s+athena|instaladas|ativas))?|"
    r"(?:listar?|mostr[ae]|ver)\s+(?:as\s+|suas\s+)?skills(?:\s+(?:disponiveis|instaladas))?)$"
)
_RE_OBSIDIAN_GRAFO = re.compile(
    r"^(?:(?:por\s+favor|athena)\s+)?"
    r"(?:(?:ver\s+|mostr[ae]\s+|quais\s+(?:sao\s+)?(?:as\s+)?)?"
    r"(?:estatisticas?|metricas?|status|resumo|hubs)\s+(?:do\s+)?(?:grafo\s+(?:do\s+)?)?(?:obsidian|vault)|"
    r"(?:quais\s+)?(?:sao\s+)?(?:as\s+)?notas?\s+orfas?(?:\s+(?:do|no)\s+(?:obsidian|vault))?|"
    r"grafo\s+do\s+(?:obsidian|vault))$"
)


def _montar_indice_aliases_apps() -> dict[str, tuple[str, str]]:
    """Mapeia aliases normalizados de `fw.CATALOGO_APPS` para `(chave_canonica, nome_exibicao)`."""
    indice: dict[str, tuple[str, str]] = {}
    for chave, info in fw.CATALOGO_APPS.items():
        nome_exib = str(info.get("nome") or chave)
        indice[normalizar_texto_eficiencia(chave)] = (chave, nome_exib)
        indice[normalizar_texto_eficiencia(nome_exib)] = (chave, nome_exib)
        for alias in info.get("aliases") or ():
            norm_a = normalizar_texto_eficiencia(str(alias))
            if norm_a:
                indice[norm_a] = (chave, nome_exib)
    return indice


def _limpar_artigos_alvo(alvo_norm: str) -> str:
    txt = alvo_norm.strip()
    prefixos = (
        "uma aba do ",
        "uma aba da ",
        "uma aba de ",
        "uma aba no ",
        "uma aba na ",
        "a aba do ",
        "a aba da ",
        "a aba de ",
        "o site do ",
        "o site da ",
        "o site de ",
        "o site ",
        "site do ",
        "site da ",
        "site ",
        "o aplicativo ",
        "aplicativo ",
        "o app ",
        "app ",
        "o programa ",
        "programa ",
        "o navegador ",
        "navegador ",
        "o ",
        "a ",
    )
    for pref in prefixos:
        if txt.startswith(pref):
            txt = txt[len(pref) :].strip()
            break
    if txt.endswith(" por favor"):
        txt = txt[: -len(" por favor")].strip()
    return txt


def _coletar_uso_ram_mb() -> tuple[float | None, float | None, float | None]:
    """Retorna `(usada_gb, total_gb, pct)` usando API nativa do SO (zero dependencias externas)."""
    gb = 1024.0 ** 3
    if sys.platform == "win32":
        try:
            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
            windll = getattr(ctypes, "windll", None)
            if windll is not None and windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total_gb = stat.ullTotalPhys / gb
                usada_gb = (stat.ullTotalPhys - stat.ullAvailPhys) / gb
                pct = float(stat.dwMemoryLoad)
                return usada_gb, total_gb, pct
        except Exception:
            return None, None, None
    else:
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
            vals: dict[str, int] = {}
            for ln in meminfo.splitlines():
                if ":" in ln:
                    k, rest = ln.split(":", 1)
                    partes = rest.strip().split()
                    if partes and partes[0].isdigit():
                        vals[k.strip()] = int(partes[0]) * 1024
            total = vals.get("MemTotal")
            avail = vals.get("MemAvailable", vals.get("MemFree"))
            if total and avail is not None:
                usada = max(0, total - avail)
                return usada / gb, total / gb, (usada / total) * 100.0
        except OSError:
            return None, None, None
    return None, None, None


def _coletar_uso_cpu_pct() -> str:
    """Retorna estimativa rapida (< 60 ms) de CPU no Windows ou load average no Linux."""
    if sys.platform == "win32":
        try:
            class _FileTime(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

            def _ft_int(ft: _FileTime) -> int:
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            windll = getattr(ctypes, "windll", None)
            if windll is not None:
                idle1, kern1, user1 = _FileTime(), _FileTime(), _FileTime()
                idle2, kern2, user2 = _FileTime(), _FileTime(), _FileTime()
                if windll.kernel32.GetSystemTimes(ctypes.byref(idle1), ctypes.byref(kern1), ctypes.byref(user1)):
                    time.sleep(0.03)
                    if windll.kernel32.GetSystemTimes(ctypes.byref(idle2), ctypes.byref(kern2), ctypes.byref(user2)):
                        d_idle = _ft_int(idle2) - _ft_int(idle1)
                        d_total = (_ft_int(kern2) - _ft_int(kern1)) + (_ft_int(user2) - _ft_int(user1))
                        if d_total > 0:
                            uso = max(0.0, min(100.0, (1.0 - (d_idle / d_total)) * 100.0))
                            return f"{uso:.1f}%"
        except Exception:
            pass
    else:
        getloadavg = getattr(os, "getloadavg", None)
        if callable(getloadavg):
            try:
                load1 = float(getloadavg()[0])  # type: ignore[index]
                cpus = max(1, os.cpu_count() or 1)
                return f"load 1m {load1:.2f} (~{min(100.0, (load1 / cpus) * 100.0):.0f}%)"
            except OSError:
                pass
    return "indisponível"


def _rota_status_pc() -> dict[str, Any]:
    try:
        fuso = ZoneInfo("America/Sao_Paulo")
    except Exception:
        fuso = timezone(timedelta(hours=-3))
    agora = datetime.now(fuso).strftime("%d/%m/%Y %H:%M:%S")

    cpus = os.cpu_count() or "?"
    cpu_pct = _coletar_uso_cpu_pct()
    ram_usada, ram_total, ram_pct = _coletar_uso_ram_mb()
    ram_txt = (
        f"**{ram_usada:.1f} GB / {ram_total:.1f} GB** ({ram_pct:.0f}%)"
        if ram_usada is not None and ram_total is not None and ram_pct is not None
        else "Indisponível"
    )

    drive = (os.environ.get("SYSTEMDRIVE", "C:") + "\\") if sys.platform == "win32" else "/"
    try:
        uso_d = shutil.disk_usage(drive)
        gb = 1024.0 ** 3
        pct_d = (uso_d.used / uso_d.total) * 100.0 if uso_d.total else 0.0
        disco_txt = f"**{uso_d.free / gb:.1f} GB livres** de {uso_d.total / gb:.1f} GB ({pct_d:.1f}% usado)"
    except OSError:
        disco_txt = "Indisponível"

    md = (
        "### Status Rápido do PC\n"
        f"- **Sistema:** {platform.system()} {platform.release()} ({platform.machine()}) · Python {platform.python_version()}\n"
        f"- **CPU:** {cpu_pct} ({cpus} núcleos lógicos)\n"
        f"- **RAM:** {ram_txt}\n"
        f"- **Disco (`{drive}`):** {disco_txt}\n"
        f"- **Horário local:** {agora}"
    )
    return {
        "resposta": md,
        "rota": "status_pc",
        "acao_windows": None,
        "modelo": MODELO_FAST_PATH,
        "tokens": 0,
    }


def _rota_abrir_windows(msg_bruta: str, msg_norm: str) -> dict[str, Any] | None:
    m_raw = _RE_ABRIR_RAW.match(msg_bruta.strip())
    if not m_raw:
        return None

    alvo_raw = m_raw.group(1).strip()
    if not alvo_raw:
        return None

    # 1) URL explicita (https://...) ou dominio direto (github.com)
    alvo_sem_artigo_raw = re.sub(
        r"^(?:uma\s+aba\s+(?:do|da|de|no|na)\s+|o\s+site\s+(?:do|da|de)\s+|site\s+(?:do|da|de)\s+|o\s+|a\s+)",
        "",
        alvo_raw,
        flags=re.IGNORECASE,
    ).strip()
    m_url = _RE_URL_EXPLICITA.match(alvo_sem_artigo_raw) or _RE_DOMINIO_DIRETO.match(alvo_sem_artigo_raw)
    if m_url:
        url_alvo = m_url.group(1).strip()
        nav = (m_url.group(2) or "brave").strip().lower()
        res_url = fw.solicitar_abrir_url_dados(url_alvo, navegador=nav)
        if res_url.get("ok") and isinstance(res_url.get("acao"), dict):
            acao = res_url["acao"]
            return {
                "resposta": (
                    f"Enviei a solicitação para abrir **{acao.get('alvo', url_alvo)}** no navegador. "
                    "Clique em **Permitir** ou **Recusar** no card abaixo."
                ),
                "rota": "windows_url",
                "acao_windows": acao,
                "modelo": MODELO_FAST_PATH,
                "tokens": 0,
            }
        return None

    # 2) Site popular pelo nome (ex.: "abre o youtube", "abre o github")
    alvo_norm = _limpar_artigos_alvo(normalizar_texto_eficiencia(alvo_raw))
    nav_sufixo = "brave"
    m_nav = re.match(r"^(.+?)\s+no\s+(brave|chrome|edge|firefox)$", alvo_norm)
    if m_nav:
        alvo_norm = m_nav.group(1).strip()
        nav_sufixo = m_nav.group(2).strip()

    if alvo_norm in _SITES_POPULARES:
        rotulo_site, url_site = _SITES_POPULARES[alvo_norm]
        res_site = fw.solicitar_abrir_url_dados(url_site, navegador=nav_sufixo)
        if res_site.get("ok") and isinstance(res_site.get("acao"), dict):
            acao = res_site["acao"]
            return {
                "resposta": (
                    f"Enviei a solicitação para abrir **{rotulo_site}** ({url_site}). "
                    "Clique em **Permitir** ou **Recusar** no card abaixo."
                ),
                "rota": "windows_url",
                "acao_windows": acao,
                "modelo": MODELO_FAST_PATH,
                "tokens": 0,
            }
        return None

    # 3) Aplicativo conhecido no Windows (ex.: "abre o brave", "abrir obsidian", "abre o vs code")
    aliases_apps = _montar_indice_aliases_apps()
    if alvo_norm in aliases_apps:
        chave_app, nome_exib = aliases_apps[alvo_norm]
        res_app = fw.solicitar_abrir_app_dados(chave_app)
        if res_app.get("ok") and isinstance(res_app.get("acao"), dict):
            acao = res_app["acao"]
            if res_app.get("auto_executada"):
                texto = str(res_app.get("mensagem") or f"**{nome_exib}** aberto automaticamente (Sempre permitir).")
            else:
                texto = (
                    f"Enviei a solicitação para abrir **{nome_exib}**. "
                    "Clique em **Permitir** (ou **Sempre permitir**) no card abaixo."
                )
            return {
                "resposta": texto,
                "rota": "windows_app",
                "acao_windows": acao,
                "modelo": MODELO_FAST_PATH,
                "tokens": 0,
            }
        return None

    return None


def _rota_ulife_cache(quer_provas: bool) -> dict[str, Any] | None:
    dados = obter_dados_ulife_estruturados("tudo")
    if not dados.get("sincronizado"):
        return None

    atualizado_em = str(dados.get("atualizado_em") or "-")
    if quer_provas:
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
            "acao_windows": None,
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
        "acao_windows": None,
        "modelo": MODELO_FAST_PATH,
        "tokens": 0,
    }


def _rota_skills() -> dict[str, Any]:
    skills = listar_skills()
    if not skills:
        md = "Nenhuma Agent Skill encontrada na pasta `skills/`."
    else:
        linhas = [f"### Agent Skills Disponíveis ({len(skills)})"]
        for s in skills:
            linhas.append(f"- **`{s['name']}`**: {s['description']}")
        md = "\n".join(linhas)
    return {
        "resposta": md,
        "rota": "skills_lista",
        "acao_windows": None,
        "modelo": MODELO_FAST_PATH,
        "tokens": 0,
    }


def _rota_obsidian_grafo() -> dict[str, Any]:
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
        "acao_windows": None,
        "modelo": MODELO_FAST_PATH,
        "tokens": 0,
    }


def tentar_fast_path(mensagem: str) -> dict[str, Any] | None:
    """Tenta resolver comandos operacionais curtos e inequivocos localmente (< 50 ms, 0 tokens).

    Retorna `dict` com `resposta`, `rota`, `acao_windows`, `modelo` e `tokens=0` quando der match,
    ou `None` para delegar transparentemente ao fluxo normal da Orquestradora LLM.
    """
    bruta = (mensagem or "").strip()
    if not bruta or len(bruta) > MAX_CHARS_FAST_PATH or "\n" in bruta:
        return None

    norm = normalizar_texto_eficiencia(bruta)
    norm_sem_pont = re.sub(r"[?!.,]+$", "", norm).strip()
    if not norm_sem_pont:
        return None

    # Rejeita mensagens compostas ou com verbos analiticos/generativos (devem ir para a LLM)
    if _RE_COMPOSTO_OU_ANALITICO.search(norm_sem_pont):
        return None

    # 1 & 2) Abrir URL / site ou aplicativo no Windows
    if _RE_ABRIR_RAW.match(bruta):
        return _rota_abrir_windows(bruta, norm_sem_pont)

    # 3) Notas e proximas provas direto do cache local do Ulife
    if _RE_NOTAS_ULIFE.match(norm_sem_pont):
        return _rota_ulife_cache(quer_provas=False)
    if _RE_PROVAS_ULIFE.match(norm_sem_pont):
        return _rota_ulife_cache(quer_provas=True)

    # 4) Status rapido do PC (CPU, RAM, disco)
    if _RE_STATUS_PC.match(norm_sem_pont):
        return _rota_status_pc()

    # 5) Skills instaladas e estatisticas do Grafo do Obsidian
    if _RE_SKILLS.match(norm_sem_pont):
        return _rota_skills()
    if _RE_OBSIDIAN_GRAFO.match(norm_sem_pont):
        return _rota_obsidian_grafo()

    return None
