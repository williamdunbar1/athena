"""Integracao Ulife v2 — API JSON direta com autenticacao via sessao Playwright.

Usa `sync_playwright` (em thread separada via `asyncio.to_thread`) reaproveitando
`PASTA_FACULDADE/perfil_navegador` (padrao `~/Documents/agente-faculdade/perfil_navegador`) APENAS para
autenticar sessao/cookies/token e, em seguida, consulta os endpoints JSON academicos
diretamente via `context.request.get(...)`.

Seguranca:
- Ignora e bloqueia qualquer endpoint `/financeiro/`, `/pagamento/`, boletos ou cartoes.
- Nunca grava headers `Authorization`, `Cookie`, tokens ou senhas no banco, Obsidian ou logs.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
from playwright.sync_api import sync_playwright

from ferramentas_estudos import PASTA_FACULDADE, PASTA_PDFS, sincronizar_indice_pdfs
from ferramentas_obsidian import escrever_nota_vault

DB_PATH = Path(__file__).resolve().parent / "memoria.db"
ARQUIVO_COOKIES = Path(__file__).resolve().parent / "ulife_cookies.json"
PASTA_PERFIL = PASTA_FACULDADE / "perfil_navegador"
URL_HOME_ULIFE = "https://student.ulife.com.br/"
URL_SOCIAL_ULIFE = "https://social.ulife.com.br/"
URL_MIDIAS_ULIFE = "https://student.ulife.com.br/UserMediaClassDiscipline"
NOTA_OBSIDIAN_PAINEL = "01-Faculdade/Painel-Ulife.md"

SEGMENTOS_BLOQUEADOS: tuple[str, ...] = (
    "/financeiro/",
    "/financeiroaluno",
    "/pagamento/",
    "/pagamentos/",
    "/conciliacaocartao/",
    "api-boleto",
    "api-contrato",
    "api-parcela",
    "validar-pagamento",
    "extrato-financeiro",
    "cartoes-armazenados",
    "formaPagamento".lower(),
)

_SYNC_LOCK = asyncio.Lock()


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS ulife_dados (
                secao TEXT PRIMARY KEY,
                dados_json TEXT NOT NULL,
                atualizado_em TEXT NOT NULL
            )
            """
        )
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _eh_url_bloqueada(url: str) -> bool:
    low = (url or "").lower()
    return any(seg in low for seg in SEGMENTOS_BLOQUEADOS)


def _normalizar_ascii(txt: str) -> str:
    sem_acento = (
        unicodedata.normalize("NFKD", (txt or "").strip())
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"\s+", " ", sem_acento).strip()


def _slugificar(txt: str) -> str:
    norm = _normalizar_ascii(txt)
    slug = re.sub(r"[^a-z0-9]+", "_", norm).strip("_")
    return slug or "geral"


def _limpar_html(texto: str, max_chars: int = 900) -> str:
    if not texto:
        return ""
    s = str(texto)
    s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ")
    linhas = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
    limpo = "\n".join(ln for ln in linhas if ln)
    limpo = re.sub(r"\n{3,}", "\n\n", limpo).strip()
    if len(limpo) > max_chars:
        limpo = limpo[: max_chars - 3].rstrip() + "..."
    return limpo


def _sanitizar_nome_arquivo(titulo: str, url: str) -> str:
    base = (titulo or "").strip()
    if not base:
        base = Path(urlparse(url).path).name or "material.pdf"
    base = html.unescape(base)
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", base)
    base = re.sub(r"\s+", " ", base).strip().rstrip(".")
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    if len(base) > 140:
        stem = base[:-4][:135].rstrip(" .-_")
        base = f"{stem}.pdf"
    return base or "material.pdf"


_PREPOSICOES_NOME_PT: frozenset[str] = frozenset({
    "de", "da", "do", "dos", "das", "e", "di", "du", "del",
})

_SLUGS_PROFESSORES_BLOQUEADOS: frozenset[str] = frozenset({
    "bruno_william_wisintainer",
    "joao_michels_cardoso",
    "lucimara_aparecida_schambeck_andrade",
    "otoniel_souza_de_oliveira",
})


def normalizar_nome_pessoa(nome: str) -> str:
    """Normaliza nome proprio em Title Case respeitando preposicoes em minusculo (de, da, do, dos, das, e)."""
    bruto = re.sub(r"\s+", " ", str(nome or "").strip())
    if not bruto:
        return ""

    def _cap_nome_simples(parte: str) -> str:
        palavras = [p for p in parte.strip().split(" ") if p]
        out: list[str] = []
        for idx, p in enumerate(palavras):
            low = p.lower()
            if idx > 0 and low in _PREPOSICOES_NOME_PT:
                out.append(low)
            elif "-" in low:
                out.append("-".join(seg.capitalize() for seg in low.split("-")))
            else:
                out.append(low.capitalize())
        return " ".join(out)

    # Caso haja mais de um nome separado por virgula, normaliza cada um e deduplica
    partes_sep = [p.strip() for p in bruto.split(",") if p.strip()]
    unicos = list(dict.fromkeys(_cap_nome_simples(p) for p in partes_sep))
    return ", ".join(p for p in unicos if p)


def _parece_nome_professor(nome_disciplina: str, professor: str = "") -> bool:
    slug_disc = _slugificar(nome_disciplina)
    if slug_disc in _SLUGS_PROFESSORES_BLOQUEADOS:
        return True
    if professor:
        for p in str(professor).split(","):
            slug_p = _slugificar(p)
            if slug_p and slug_p == slug_disc:
                return True
    return False


def _resolver_pasta_materia(nome_disciplina: str, modalidade: str = "", professor: str = "") -> Path:
    """Resolve a pasta de destino dentro de pdfs_faculdade reutilizando pastas existentes.

    Nunca cria pastas usando nome de professor.
    """
    PASTA_PDFS.mkdir(parents=True, exist_ok=True)
    norm_disc = _normalizar_ascii(nome_disciplina)
    norm_mod = _normalizar_ascii(modalidade)

    existentes = {
        p.name.lower(): p
        for p in PASTA_PDFS.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name.lower() not in _SLUGS_PROFESSORES_BLOQUEADOS
    }

    if "academia" in norm_disc and "futuros" in norm_disc:
        destino = existentes.get("academia_de_futuros_profissionais") or (PASTA_PDFS / "academia_de_futuros_profissionais")
        destino.mkdir(parents=True, exist_ok=True)
        return destino

    if "calculo" in norm_disc and "algebra" in norm_disc:
        destino = existentes.get("calculo_algebra_linear") or (PASTA_PDFS / "calculo_algebra_linear")
        destino.mkdir(parents=True, exist_ok=True)
        return destino

    if "fisica" in norm_disc:
        if "pratica" in norm_mod or "laboratorio" in norm_mod:
            destino = existentes.get("fisica_pratica") or (PASTA_PDFS / "fisica_pratica")
            destino.mkdir(parents=True, exist_ok=True)
            return destino
        destino = existentes.get("fisica_teorica") or (PASTA_PDFS / "fisica_teorica")
        destino.mkdir(parents=True, exist_ok=True)
        return destino

    if _parece_nome_professor(nome_disciplina, professor):
        destino = PASTA_PDFS / "geral"
        destino.mkdir(parents=True, exist_ok=True)
        return destino

    slug_direto = _slugificar(nome_disciplina)
    if slug_direto in existentes:
        return existentes[slug_direto]

    stopwords = {"e", "de", "da", "do", "dos", "das", "para", "com", "em", "na", "no"}
    tokens_disc = {t for t in slug_direto.split("_") if len(t) >= 3 and t not in stopwords}
    if tokens_disc:
        melhor_pasta = None
        melhor_score = 0
        for nome_dir, pasta in existentes.items():
            tokens_dir = {t for t in nome_dir.split("_") if len(t) >= 3 and t not in stopwords}
            comuns = len(tokens_disc & tokens_dir)
            if comuns >= 2 and comuns > melhor_score:
                melhor_score = comuns
                melhor_pasta = pasta
        if melhor_pasta is not None:
            return melhor_pasta

    destino = PASTA_PDFS / slug_direto
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def _pdf_ja_existe(pasta: Path, nome_arquivo: str, url: str) -> Path | None:
    candidato = pasta / nome_arquivo
    if candidato.is_file() and candidato.stat().st_size > 200:
        return candidato

    url_nome = Path(urlparse(url).path).name
    if url_nome:
        cand_url = pasta / url_nome
        if cand_url.is_file() and cand_url.stat().st_size > 200:
            return cand_url

    chave_alvo = re.sub(r"[^a-z0-9]+", "", _normalizar_ascii(Path(nome_arquivo).stem))
    chave_url = re.sub(r"[^a-z0-9]+", "", _normalizar_ascii(Path(url_nome).stem))
    chave_url_sem_ts = re.sub(r"\d{12,14}$", "", chave_url)

    if pasta.exists():
        for arq in pasta.glob("*.pdf"):
            if arq.stat().st_size <= 200:
                continue
            chave_local = re.sub(r"[^a-z0-9]+", "", _normalizar_ascii(arq.stem))
            if chave_alvo and chave_local == chave_alvo:
                return arq
            if chave_url_sem_ts and len(chave_url_sem_ts) >= 8 and chave_local == chave_url_sem_ts:
                return arq
    return None


def _para_float(valor) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    txt = str(valor).strip().replace(",", ".")
    if not txt:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _para_int(valor) -> int | None:
    num = _para_float(valor)
    if num is None:
        return None
    return int(round(num))


def _extrair_primeiro(obj: dict, chaves: tuple[str, ...]):
    if not isinstance(obj, dict):
        return None
    low_map = {str(k).lower(): v for k, v in obj.items()}
    for chave in chaves:
        v = low_map.get(chave.lower())
        if v is not None and v != "":
            return v
    return None


def _decodificar_jwt_payload(token: str) -> dict:
    """Decodifica apenas o payload do JWT em memoria para ler IDs (nunca salvo em disco)."""
    try:
        partes = (token or "").split(".")
        if len(partes) < 2:
            return {}
        payload = partes[1]
        padding = "=" * (-len(payload) % 4)
        bruto = base64.urlsafe_b64decode(payload + padding)
        dados = json.loads(bruto.decode("utf-8", errors="ignore"))
        return dados if isinstance(dados, dict) else {}
    except Exception:
        return {}


def _esta_logado_student(url: str) -> bool:
    low = (url or "").lower()
    if "student.ulife.com.br" not in low:
        return False
    if "login.aspx" in low:
        return False
    return "login" not in urlparse(low).path.lower()


def _garantir_login_ulife(page, timeout_segundos: float = 60.0) -> tuple[bool, str]:
    """Garante autenticacao silenciosa no Ulife (ate 60s) com ULIFE_LOGIN e autofill Microsoft (sem roubar foco)."""
    login_env = (os.getenv("ULIFE_LOGIN") or "").strip()
    deadline = time.monotonic() + timeout_segundos

    while time.monotonic() < deadline:
        url_atual = (page.url or "").lower()

        if _esta_logado_student(url_atual):
            return True, "Sessao autenticada no Ulife."

        # Etapa 1: Pagina de login do Ulife (Login.aspx)
        if "ulife.com.br" in url_atual and ("login.aspx" in url_atual or "login" in urlparse(url_atual).path.lower()):
            try:
                campo = page.locator(
                    'input[name*="Login"]:visible, input[id*="Login"]:visible, '
                    'input[name*="User"]:visible, input[type="email"]:visible, input[type="text"]:visible'
                ).first
                if campo.count() > 0:
                    val_atual = (campo.input_value() or "").strip()
                    if not val_atual and login_env:
                        campo.fill(login_env)
                        page.wait_for_timeout(300)
                    btn = page.locator(
                        'button:has-text("Entrar"):visible, input[value*="Entrar"]:visible, '
                        '#btnLogin:visible, input[type="submit"]:visible, button[type="submit"]:visible, '
                        'button:has-text("Avançar"):visible, button:has-text("Continuar"):visible'
                    ).first
                    if btn.count() > 0:
                        btn.click(timeout=5000)
                        page.wait_for_timeout(2000)
                        continue
            except Exception:
                pass

        # Etapa 2: Login Microsoft (login.microsoftonline.com / login.live.com) — sem bring_to_front()
        if "login.microsoftonline.com" in url_atual or "login.live.com" in url_atual:
            try:
                # 2a) Card da conta @ulife.com.br
                card_ulife = page.locator(
                    '[data-test-id*="@ulife.com.br"]:visible, '
                    'div.table[role="button"]:has-text("@ulife.com.br"):visible'
                ).first
                if card_ulife.count() > 0:
                    card_ulife.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    continue

                # 2b) Campo de e-mail Microsoft (se aparecer)
                inp_email = page.locator('input[name="loginfmt"]:visible, #i0116:visible').first
                if inp_email.count() > 0:
                    if not (inp_email.input_value() or "").strip() and login_env:
                        inp_email.fill(login_env)
                        page.wait_for_timeout(300)
                    btn_next = page.locator('#idSIButton9:visible').first
                    if btn_next.count() > 0:
                        btn_next.click(timeout=5000)
                        page.wait_for_timeout(2000)
                        continue

                # 2c) Campo de senha com autofill ou tela "Continuar conectado?" (#idSIButton9)
                btn_si = page.locator('#idSIButton9:visible').first
                if btn_si.count() > 0:
                    inp_pass = page.locator('input[name="passwd"]:visible, #i0118:visible').first
                    if inp_pass.count() > 0:
                        page.wait_for_timeout(700)
                        tem_senha = bool((inp_pass.input_value() or "").strip())
                        if not tem_senha:
                            try:
                                tem_senha = (
                                    page.locator('input[name="passwd"]:-webkit-autofill, #i0118:-webkit-autofill').count()
                                    > 0
                                )
                            except Exception:
                                tem_senha = False
                        if tem_senha:
                            btn_si.click(timeout=5000)
                            page.wait_for_timeout(2000)
                            continue
                    else:
                        # Tela "Continuar conectado?" (KMSI)
                        btn_si.click(timeout=5000)
                        page.wait_for_timeout(2000)
                        continue
            except Exception:
                pass

        page.wait_for_timeout(1000)

    if _esta_logado_student(page.url or ""):
        return True, "Sessao autenticada no Ulife."
    return False, f"Tempo limite (60s) para autenticar no Ulife (URL atual: {page.url})."


class _RespHTTP:
    __slots__ = ("status", "ok", "headers", "_body", "location")

    def __init__(self, status: int, headers: dict, body_bytes: bytes):
        self.status = int(status)
        self.ok = 200 <= self.status < 300
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self._body = body_bytes or b""
        self.location = self.headers.get("location", "")

    def body(self) -> bytes:
        return self._body


class _RequisitorHttpx:
    def __init__(self, client: httpx.Client):
        self._client = client

    def get(self, url: str, headers: dict[str, str] | None = None, timeout: int = 20000) -> _RespHTTP:
        r = self._client.get(url, headers=headers, timeout=max(5.0, timeout / 1000.0))
        return _RespHTTP(r.status_code, dict(r.headers), r.content)

    def post(self, url: str, headers: dict[str, str] | None = None, timeout: int = 15000) -> _RespHTTP:
        r = self._client.post(url, headers=headers, timeout=max(5.0, timeout / 1000.0))
        return _RespHTTP(r.status_code, dict(r.headers), r.content)


class _RequisitorPlaywright:
    def __init__(self, pw_request):
        self._req = pw_request

    def get(self, url: str, headers: dict[str, str] | None = None, timeout: int = 20000) -> _RespHTTP:
        r = self._req.get(url, headers=headers, timeout=timeout)
        return _RespHTTP(r.status, dict(r.headers or {}), r.body() if r.ok else b"")


def _eh_resposta_nao_autenticada(resp: _RespHTTP) -> bool:
    if resp.status in (401, 403):
        return True
    if 300 <= resp.status < 400:
        loc = (resp.location or "").lower()
        if "login.aspx" in loc or "microsoftonline.com" in loc or "login" in loc:
            return True
    ctype = resp.headers.get("content-type", "").lower()
    if "text/html" in ctype:
        prev = resp.body()[:500].decode("utf-8", errors="ignore").lower()
        if "login.aspx" in prev or "microsoftonline" in prev or "<form" in prev:
            return True
    return False


def _salvar_sessao_cookies(context, sessao_mem: dict) -> None:
    """Salva cookies atualizados (ulife/anima) + IDs academicos em ulife_cookies.json (ignorado no git)."""
    try:
        brutos = context.cookies()
        filtrados = [
            c
            for c in brutos
            if any(dom in (c.get("domain") or "").lower() for dom in ("ulife", "anima"))
        ]
        payload = {
            "salvo_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "cookies": filtrados,
            "meta": {
                "user_id": sessao_mem.get("user_id"),
                "user_org_id": sessao_mem.get("user_org_id"),
                "org_id": sessao_mem.get("org_id"),
                "student_id": sessao_mem.get("student_id"),
                "course_id": sessao_mem.get("course_id"),
            },
            "access_token": sessao_mem.get("access_token") or "",
            "auth_header": sessao_mem.get("auth_header") or "",
        }
        ARQUIVO_COOKIES.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _carregar_sessao_cookies() -> dict | None:
    if not ARQUIVO_COOKIES.is_file():
        return None
    try:
        dados = json.loads(ARQUIVO_COOKIES.read_text(encoding="utf-8-sig"))
        if isinstance(dados, dict) and isinstance(dados.get("cookies"), list) and dados["cookies"]:
            return dados
    except Exception:
        pass
    return None


def _montar_jar_httpx(cookies_lista: list[dict]) -> httpx.Cookies:
    jar = httpx.Cookies()
    agora = time.time()
    for c in cookies_lista:
        if not isinstance(c, dict):
            continue
        nome = str(c.get("name") or "").strip()
        valor = str(c.get("value") or "")
        dom = str(c.get("domain") or "").strip()
        path = str(c.get("path") or "/")
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0 and exp < agora:
            continue
        if nome and dom:
            jar.set(nome, valor, domain=dom, path=path)
    return jar


def _api_get_json(ctx_request, url: str, headers_base: dict[str, str], timeout_ms: int = 20000):
    """Faz GET direto em endpoint JSON academico (nunca financeiro/pagamento)."""
    if _eh_url_bloqueada(url):
        raise ValueError(f"Endpoint financeiro/pagamento bloqueado por seguranca: {url}")

    tentativas = [headers_base]
    if "Authorization" in headers_base:
        sem_auth = {k: v for k, v in headers_base.items() if k != "Authorization"}
        tentativas.append(sem_auth)

    for hdrs in tentativas:
        try:
            resp = ctx_request.get(url, headers=hdrs, timeout=timeout_ms)
            if resp.status in (401, 403) or (300 <= resp.status < 400):
                continue
            if not resp.ok:
                continue
            raw = resp.body()
            if not raw:
                continue
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
    return None


CHAVES_NOME_DISCIPLINA: tuple[str, ...] = (
    "DisciplineName",
    "ClassDisciplineName",
    "DisciplineTitle",
    "CourseDisciplineName",
    "GroupDisciplineName",
    "SubjectName",
    "Discipline",
    "Title",
    "Description",
    "GroupName",
)

CHAVES_FALTAS: tuple[str, ...] = (
    "Absences",
    "TotalAbsences",
    "AbsenceCount",
    "QuantityAbsences",
    "NumberOfAbsences",
    "StudentAbsences",
    "Faults",
    "TotalFaults",
    "FaultCount",
    "Faltas",
    "TotalFaltas",
    "QtdFaltas",
    "QuantidadeFaltas",
    "MissedClasses",
)

CHAVES_MAX_FALTAS: tuple[str, ...] = (
    "MaxAbsences",
    "MaximumAbsences",
    "LimitAbsences",
    "AllowedAbsences",
    "MaxFaults",
    "LimiteFaltas",
    "MaximoFaltas",
)

CHAVES_FREQUENCIA: tuple[str, ...] = (
    "Frequency",
    "FrequencyPercent",
    "PercentFrequency",
    "StudentFrequency",
    "Attendance",
    "AttendancePercent",
    "Presence",
    "PresencePercent",
    "PercentPresence",
    "Frequencia",
    "PercentualFrequencia",
)

CHAVES_SITUACAO: tuple[str, ...] = (
    "Situation",
    "StatusDescription",
    "ApprovalStatus",
    "StatusName",
    "FrequencyStatus",
)


def _eh_texto_valido_disciplina(val) -> bool:
    if not isinstance(val, str):
        return False
    s = val.strip()
    if not s or len(s) < 2:
        return False
    low = s.lower()
    if low.startswith(("http://", "https://", "/")):
        return False
    return not re.match(r"^[a-z0-9_]+_\d{4}/\d$", low)


def _extrair_professor(obj: dict, *, permitir_lista: bool = False) -> str:
    """Extrai o nome do professor diretamente do no da disciplina (nunca concatena listas globais do curso)."""
    if not isinstance(obj, dict):
        return ""
    for chave in ("TeacherName", "ProfessorName", "MainTeacherName", "Docente", "NomeProfessor"):
        v = _extrair_primeiro(obj, (chave,))
        if isinstance(v, str) and v.strip():
            return normalizar_nome_pessoa(v)

    for chave in ("Teacher", "Professor"):
        v = _extrair_primeiro(obj, (chave,))
        if isinstance(v, str) and v.strip():
            return normalizar_nome_pessoa(v)
        if isinstance(v, dict):
            nm = _extrair_primeiro(v, ("Name", "UserName", "FullName", "TeacherName"))
            if isinstance(nm, str) and nm.strip():
                return normalizar_nome_pessoa(nm)

    if permitir_lista:
        for chave in ("Teachers", "Professors", "TeacherList"):
            v = _extrair_primeiro(obj, (chave,))
            if isinstance(v, list) and v:
                nomes = []
                for el in v:
                    if isinstance(el, str) and el.strip():
                        nomes.append(normalizar_nome_pessoa(el))
                    elif isinstance(el, dict):
                        nm = _extrair_primeiro(el, ("Name", "UserName", "FullName", "TeacherName"))
                        if isinstance(nm, str) and nm.strip():
                            nomes.append(normalizar_nome_pessoa(nm))
                unicos = [n for n in dict.fromkeys(nomes) if n]
                # Uma turma individual tem 1 (ou no maximo 2) docentes; 3+ indica lista global de filtro do curso
                if 1 <= len(unicos) <= 2:
                    return ", ".join(unicos)
    return ""


def _buscar_metricas_frequencia(obj, profundidade: int = 0) -> dict:
    """Busca recursivamente campos de faltas, limite, frequencia e situacao no JSON."""
    res = {"faltas": None, "max_faltas": None, "frequencia_pct": None, "situacao": "", "nota_card": None}
    if profundidade > 4 or obj is None:
        return res

    if isinstance(obj, list):
        for el in obj:
            sub = _buscar_metricas_frequencia(el, profundidade + 1)
            for k, v in sub.items():
                if res[k] in (None, "") and v not in (None, ""):
                    res[k] = v
        return res

    if not isinstance(obj, dict):
        return res

    f_val = _para_int(_extrair_primeiro(obj, CHAVES_FALTAS))
    mf_val = _para_int(_extrair_primeiro(obj, CHAVES_MAX_FALTAS))
    fq_val = _para_float(_extrair_primeiro(obj, CHAVES_FREQUENCIA))
    sit_val = str(_extrair_primeiro(obj, CHAVES_SITUACAO) or "").strip()
    nc_val = _para_float(_extrair_primeiro(obj, ("FinalGrade", "TotalGrade", "Grade", "AverageGrade", "StudentGrade")))

    if f_val is not None:
        res["faltas"] = f_val
    if mf_val is not None:
        res["max_faltas"] = mf_val
    if fq_val is not None:
        res["frequencia_pct"] = round(fq_val, 1)
    if sit_val:
        res["situacao"] = sit_val
    if nc_val is not None:
        res["nota_card"] = round(nc_val, 2)

    for k, v in obj.items():
        if k in ("ActivityList", "Activities", "StudentLessons", "StudentCategories"):
            continue
        if isinstance(v, (dict, list)):
            sub = _buscar_metricas_frequencia(v, profundidade + 1)
            for sk, sv in sub.items():
                if res[sk] in (None, "") and sv not in (None, ""):
                    res[sk] = sv
    return res


def _aplicar_nome_disciplina(disc: dict, nome_real: str) -> None:
    """Atualiza o nome real da disciplina preservando eventual nome de professor na coluna professor."""
    novo = (nome_real or "").strip()
    if not _eh_texto_valido_disciplina(novo):
        return
    atual = (disc.get("nome") or "").strip()
    if not atual or _normalizar_ascii(atual) == _normalizar_ascii(novo):
        disc["nome"] = novo
        return
    # Se o nome anterior era na verdade o professor (ex.: campo Name no card), move para professor normalizado
    if not (disc.get("professor") or "").strip():
        disc["professor"] = normalizar_nome_pessoa(atual)
    disc["nome"] = novo


def _extrair_disciplinas_de_payload(payload, meta: dict) -> list[dict]:
    """Extrai lista normalizada de disciplinas do JSON de Student/Disciplines."""
    if not isinstance(payload, dict):
        return []
    res = payload.get("Result")
    if res is None:
        return []

    candidatos_brutos: list[dict] = []
    chaves_vistas: set[str] = set()

    def _coletar(no, pai_ctx: dict | None = None):
        if isinstance(no, list):
            for item in no:
                _coletar(item, pai_ctx)
            return
        if not isinstance(no, dict):
            return

        # Captura IDs globais se presentes
        if not meta.get("student_id"):
            sid = _extrair_primeiro(no, ("StudentID", "StudentId", "IDStudent"))
            if sid and str(sid).isdigit() and int(sid) > 0:
                meta["student_id"] = int(sid)

        cd_id = _extrair_primeiro(no, ("ClassDisciplineID", "ClassDisciplineId"))

        # Nome explicito de disciplina neste no (sem usar 'Name' generico que costuma ser professor)
        nome_disc_no = ""
        for ch in CHAVES_NOME_DISCIPLINA:
            val = _extrair_primeiro(no, (ch,))
            if isinstance(val, dict):
                sub_nm = _extrair_primeiro(val, ("DisciplineName", "Title", "Description", "Name"))
                if _eh_texto_valido_disciplina(sub_nm):
                    nome_disc_no = str(sub_nm).strip()
                    break
            elif _eh_texto_valido_disciplina(val):
                nome_disc_no = str(val).strip()
                break

        # Ignora itens de ActivityList (possuem LessonCategoryName mas nao sao cards de disciplina)
        if cd_id and "LessonCategoryName" not in no:
            chaves_vistas.update(str(k) for k in no)
            mesclado = {**(pai_ctx or {}), **no}
            if nome_disc_no:
                mesclado["_disc_name_resolved"] = nome_disc_no
            elif pai_ctx and pai_ctx.get("_disc_name_resolved"):
                mesclado["_disc_name_resolved"] = pai_ctx["_disc_name_resolved"]
            # Professor SEMPRE extraido apenas do proprio card da disciplina (nunca herdado do container do curso!)
            prof_card = _extrair_professor(no, permitir_lista=True)
            if prof_card:
                mesclado["_prof_resolved"] = prof_card
            candidatos_brutos.append(mesclado)

        # Propaga contexto do pai para filhos (ex.: grupo da disciplina -> turmas teorica/pratica)
        # NUNCA propaga lista de professores do pai para evitar repetir todos os docentes em todas as materias
        ctx_filho = dict(pai_ctx or {})
        if not cd_id:
            if nome_disc_no:
                ctx_filho["_disc_name_resolved"] = nome_disc_no
            else:
                nm_grupo = _extrair_primeiro(no, ("Name",))
                # Em nos de grupo (sem cd_id e sem campos de pessoa), Name pode ser a disciplina
                eh_pessoa = any(k in no for k in ("Email", "UserId", "TeacherId", "UserMiniatureUrl", "PhotoUrl"))
                if not eh_pessoa and _eh_texto_valido_disciplina(nm_grupo):
                    ctx_filho["_disc_name_resolved"] = str(nm_grupo).strip()
        if not ctx_filho.get("OrganizationCourseID"):
            cid = _extrair_primeiro(no, ("OrganizationCourseID", "CourseId", "CourseID"))
            if cid:
                ctx_filho["OrganizationCourseID"] = cid

        for k, v in no.items():
            if k in ("ActivityList", "Activities"):
                continue
            if isinstance(v, (list, dict)):
                _coletar(v, ctx_filho)

    _coletar(res)
    if chaves_vistas:
        meta.setdefault("campos_detectados", {})["disciplines_keys"] = sorted(chaves_vistas)[:80]

    por_id: dict[int, dict] = {}
    for item in candidatos_brutos:
        cd_id = _para_int(_extrair_primeiro(item, ("ClassDisciplineID", "ClassDisciplineId")))
        if not cd_id:
            continue

        # Extrai ClassDisciplineCrpt de campo direto ou de query string cd=...
        crpt = str(_extrair_primeiro(item, ("ClassDisciplineCrpt", "ClassDisciplineCrypt", "Cd")) or "").strip()
        if not crpt:
            for v in item.values():
                if isinstance(v, str) and "cd=" in v:
                    qs = parse_qs(urlparse(v).query)
                    if qs.get("cd"):
                        crpt = unquote(qs["cd"][0])
                        break

        scd_id = _para_int(_extrair_primeiro(item, ("StudentClassDisciplineID", "StudentClassDisciplineId")))
        sid = _para_int(_extrair_primeiro(item, ("StudentID", "StudentId")))
        if sid and not meta.get("student_id"):
            meta["student_id"] = sid

        nome_disc = str(item.get("_disc_name_resolved") or "").strip()
        prof = normalizar_nome_pessoa(str(item.get("_prof_resolved") or _extrair_professor(item, permitir_lista=True) or ""))
        raw_name = str(_extrair_primeiro(item, ("Name",)) or "").strip()

        # No JSON do Ulife, o campo 'Name' dentro do card da turma geralmente e o nome do professor
        if nome_disc:
            if not prof and raw_name and _normalizar_ascii(raw_name) != _normalizar_ascii(nome_disc):
                prof = normalizar_nome_pessoa(raw_name)
        else:
            # Guarda raw_name como professor provisorio; o nome real vira de GetDropdownDiscipline / ResumeCategory
            if not prof and raw_name:
                prof = normalizar_nome_pessoa(raw_name)
            nome_disc = raw_name

        modalidade = str(_extrair_primeiro(item, ("TimeLoadTypeName", "ModalityName", "ClassType")) or "").strip()
        periodo = str(_extrair_primeiro(item, ("ModuleStepName", "PeriodName", "TermName")) or "").strip()
        course_id = _para_int(_extrair_primeiro(item, ("OrganizationCourseID", "CourseId", "CourseID")))

        metricas = _buscar_metricas_frequencia(item)

        reg = {
            "class_discipline_id": cd_id,
            "class_discipline_crpt": crpt,
            "student_class_discipline_id": scd_id,
            "student_id": sid or meta.get("student_id"),
            "course_id": course_id or meta.get("course_id"),
            "nome": nome_disc,
            "modalidade": modalidade,
            "periodo": periodo,
            "professor": prof,
            "faltas": metricas["faltas"],
            "max_faltas": metricas["max_faltas"],
            "frequencia_pct": metricas["frequencia_pct"],
            "nota_card": metricas["nota_card"],
            "situacao": metricas["situacao"],
            "aulas_vistas": None,
            "aulas_total": None,
        }

        if cd_id not in por_id:
            por_id[cd_id] = reg
        else:
            atual = por_id[cd_id]
            for k, v in reg.items():
                if atual.get(k) in (None, "", 0) and v not in (None, "", 0):
                    atual[k] = v

    return sorted(por_id.values(), key=lambda d: (d["nome"].lower(), d["modalidade"].lower()))


def _extrair_atividades_de_payload(payload) -> list[dict]:
    itens = []
    if isinstance(payload, dict):
        res = payload.get("Result")
        if isinstance(res, list):
            itens = res
        elif isinstance(res, dict) and isinstance(res.get("ActivityList"), list):
            itens = res["ActivityList"]

    atividades: list[dict] = []
    vistos: set[tuple[str, str, str]] = set()
    for it in itens:
        if not isinstance(it, dict):
            continue
        disc = str(it.get("DisciplineName") or "").strip()
        titulo = str(it.get("LessonCategoryName") or it.get("Title") or "").strip()
        entrega = str(it.get("DeliveryDate") or "").strip()
        chave = (disc.lower(), titulo.lower(), entrega)
        if not titulo or chave in vistos:
            continue
        vistos.add(chave)
        atividades.append({
            "disciplina": disc,
            "titulo": titulo,
            "tipo": str(it.get("LessonCategoryType") or "").strip(),
            "disponivel_em": str(it.get("AvailableDate") or "").strip(),
            "entrega_em": entrega,
            "entrega_texto": str(it.get("DeliveryDateText") or "").strip(),
            "url": str(it.get("LessonCategoryURL") or "").strip(),
        })
    return atividades


def _parsear_notas_disciplina(disc: dict, payload_grades, meta: dict | None = None) -> dict:
    avaliacoes: list[dict] = []
    faltas = disc.get("faltas")
    max_faltas = disc.get("max_faltas")
    freq = disc.get("frequencia_pct")
    situacao = disc.get("situacao") or ""
    nota_explicit = disc.get("nota_card")

    res = payload_grades.get("Result") if isinstance(payload_grades, dict) else None
    if isinstance(res, dict):
        if meta is not None and "grades_keys" not in meta.setdefault("campos_detectados", {}):
            meta["campos_detectados"]["grades_keys"] = sorted(str(k) for k in res)[:60]

        metricas = _buscar_metricas_frequencia(res)
        if faltas is None and metricas["faltas"] is not None:
            faltas = metricas["faltas"]
        if max_faltas is None and metricas["max_faltas"] is not None:
            max_faltas = metricas["max_faltas"]
        if freq is None and metricas["frequencia_pct"] is not None:
            freq = metricas["frequencia_pct"]
        if not situacao and metricas["situacao"]:
            situacao = metricas["situacao"]
        if nota_explicit is None and metricas["nota_card"] is not None:
            nota_explicit = metricas["nota_card"]

        for licao in res.get("StudentLessons") or []:
            if not isinstance(licao, dict):
                continue
            for cat in licao.get("StudentCategories") or []:
                if not isinstance(cat, dict):
                    continue
                nota = _para_float(cat.get("Grade"))
                peso = _para_float(cat.get("Weight"))
                pct = _para_float(cat.get("Percent"))
                avaliacoes.append({
                    "item": str(cat.get("EvaluationPlanItem") or "").strip(),
                    "nome": str(cat.get("LessonCategoryName") or licao.get("LessonName") or "").strip(),
                    "nota": round(nota, 2) if nota is not None else None,
                    "peso": round(peso, 2) if peso is not None else None,
                    "percentual": round(pct, 1) if pct is not None else None,
                    "escala": str(cat.get("GradeScaleDescription") or "").strip(),
                })

    notas_validas = [a["nota"] for a in avaliacoes if a["nota"] is not None]
    pesos_validos = [a["peso"] for a in avaliacoes if a["peso"] is not None]
    nota_total = round(sum(notas_validas), 2) if notas_validas else nota_explicit
    peso_total = round(sum(pesos_validos), 2) if pesos_validos else None

    return {
        "class_discipline_id": disc["class_discipline_id"],
        "disciplina": disc["nome"],
        "modalidade": disc.get("modalidade") or "",
        "periodo": disc.get("periodo") or "",
        "professor": disc.get("professor") or "",
        "nota_total": nota_total,
        "peso_total": peso_total,
        "faltas": faltas,
        "max_faltas": max_faltas,
        "frequencia_pct": freq,
        "situacao": situacao,
        "aulas_vistas": disc.get("aulas_vistas"),
        "aulas_total": disc.get("aulas_total"),
        "avaliacoes": avaliacoes,
    }


def _extrair_avisos(notif_payload, feeds_payload, commitments_payload, limite: int = 15) -> list[dict]:
    brutos: list[dict] = []

    # 1) Notifications-summarized ou notifications
    if isinstance(notif_payload, dict):
        res = notif_payload.get("Result")
        lista_notif = []
        if isinstance(res, dict) and isinstance(res.get("Notifications"), list):
            lista_notif = res["Notifications"]
        elif isinstance(res, list):
            lista_notif = res
        for n in lista_notif:
            if not isinstance(n, dict):
                continue
            titulo = _limpar_html(n.get("Title") or "", max_chars=200)
            desc = _limpar_html(n.get("Description") or "", max_chars=500)
            if not titulo and not desc:
                continue
            brutos.append({
                "origem": "Notificacao",
                "categoria": str(n.get("Category") or "Geral").strip(),
                "titulo": titulo or "(Sem titulo)",
                "descricao": desc,
                "data": str(n.get("CreatedDateTime") or n.get("TargetDate") or "").strip(),
                "lido": bool(n.get("Read", True)),
                "importante": bool(n.get("Important", False)),
                "url": str(n.get("Url") or "").strip(),
            })

    # 2) ListFeeds (comunicados do mural social)
    if isinstance(feeds_payload, dict):
        res_f = feeds_payload.get("Result")
        if isinstance(res_f, list):
            for f in res_f:
                if not isinstance(f, dict):
                    continue
                titulo = _limpar_html(f.get("Title") or "", max_chars=200)
                desc = _limpar_html(f.get("Description") or f.get("Message") or "", max_chars=600)
                if not titulo and not desc:
                    continue
                dt = str(
                    _extrair_primeiro(f, ("PublicationDate", "CreatedDate", "CreatedDateTime", "Date", "PostDate")) or ""
                ).strip()
                brutos.append({
                    "origem": "Aviso",
                    "categoria": str(_extrair_primeiro(f, ("CategoryName", "Category")) or "Comunicado").strip(),
                    "titulo": titulo or "(Comunicado)",
                    "descricao": desc,
                    "data": dt,
                    "lido": True,
                    "importante": bool(f.get("Pinned") or f.get("IsPinned")),
                    "url": URL_SOCIAL_ULIFE,
                })

    # 3) Commitments institucionais
    if isinstance(commitments_payload, dict):
        res_c = commitments_payload.get("Result")
        if isinstance(res_c, list):
            for cm in res_c:
                if not isinstance(cm, dict):
                    continue
                titulo = _limpar_html(cm.get("Title") or "", max_chars=200)
                desc = _limpar_html(cm.get("Description") or "", max_chars=500)
                if not titulo:
                    continue
                brutos.append({
                    "origem": "Compromisso",
                    "categoria": str(cm.get("Category") or "Atividade").strip(),
                    "titulo": titulo,
                    "descricao": desc,
                    "data": str(cm.get("StartDate") or cm.get("EndDate") or "").strip(),
                    "lido": False,
                    "importante": True,
                    "url": str(cm.get("ActionUrl") or "").strip(),
                })

    # Deduplica e ordena pelos mais recentes
    unicos: list[dict] = []
    vistos: set[tuple[str, str]] = set()
    for av in sorted(brutos, key=lambda x: x.get("data") or "", reverse=True):
        chave = (_normalizar_ascii(av["titulo"]), (av.get("data") or "")[:10])
        if chave in vistos:
            continue
        vistos.add(chave)
        unicos.append(av)
        if len(unicos) >= limite:
            break

    return unicos


def _salvar_secao_db(secao: str, dados, atualizado_em: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO ulife_dados (secao, dados_json, atualizado_em) VALUES (?, ?, ?)",
            (secao, json.dumps(dados, ensure_ascii=False), atualizado_em),
        )


def _formatar_data_br(iso_str: str) -> str:
    txt = (iso_str or "").strip()
    if not txt:
        return "-"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}:\d{2}))?", txt)
    if not m:
        return txt[:16]
    ano, mes, dia, hora = m.groups()
    return f"{dia}/{mes}/{ano} {hora}".strip() if hora else f"{dia}/{mes}/{ano}"


def _gerar_markdown_obsidian(
    resumo: dict,
    notas_faltas: list[dict],
    atividades: list[dict],
    avisos: list[dict],
    materiais: list[dict],
) -> str:
    linhas = [
        "---",
        "tags:",
        "  - faculdade",
        "  - ulife",
        "  - painel",
        f"atualizado_em: \"{resumo.get('sincronizado_em', '')}\"",
        "---",
        "",
        "# Painel Ulife — Faculdade",
        "",
        f"Sincronizado automaticamente pela [[Perfil-William|Athena]] via API JSON do Ulife em **{resumo.get('sincronizado_em', '-')}**.",
        "",
        f"- **Curso:** {resumo.get('curso') or '-'}",
        f"- **Instituição:** {resumo.get('instituicao') or '-'}",
        f"- **RA:** {resumo.get('ra') or '-'}",
        f"- **Disciplinas:** {resumo.get('total_disciplinas', 0)} | **Materiais:** {resumo.get('total_materiais', 0)} | **PDFs novos baixados:** {resumo.get('pdfs_novos_baixados', 0)}",
        "",
        "## Notas e Faltas",
        "",
        "| Disciplina | Modalidade | Avaliações | Nota Total | Faltas / Freq. |",
        "| --- | --- | --- | --- | --- |",
    ]

    if notas_faltas:
        for nf in notas_faltas:
            avals = []
            for a in nf.get("avaliacoes") or []:
                rot = a.get("item") or a.get("nome") or "Aval."
                nota_s = f"{a['nota']:.1f}" if a.get("nota") is not None else "-"
                peso_s = f"/{a['peso']:.0f}" if a.get("peso") is not None else ""
                avals.append(f"{rot}: {nota_s}{peso_s}")
            avals_txt = "; ".join(avals) if avals else "Sem lançamentos"
            nota_tot = (
                f"{nf['nota_total']:.1f}" + (f" / {nf['peso_total']:.0f}" if nf.get("peso_total") else "")
                if nf.get("nota_total") is not None
                else "N/D"
            )
            partes_f = []
            if nf.get("faltas") is not None:
                partes_f.append(
                    f"{nf['faltas']}/{nf['max_faltas']} faltas" if nf.get("max_faltas") else f"{nf['faltas']} faltas"
                )
            if nf.get("frequencia_pct") is not None:
                partes_f.append(f"{nf['frequencia_pct']:.0f}%")
            faltas_txt = " · ".join(partes_f) if partes_f else "N/D"
            disc_esc = (nf.get("disciplina") or "-").replace("|", "/")
            mod_esc = (nf.get("modalidade") or "-").replace("|", "/")
            linhas.append(f"| {disc_esc} | {mod_esc} | {avals_txt.replace('|', '/')} | **{nota_tot}** | {faltas_txt} |")
    else:
        linhas.append("| Nenhuma disciplina sincronizada | - | - | - | - |")

    linhas.extend(["", "## Próximas Provas e Atividades", ""])
    if atividades:
        for at in atividades:
            dt_txt = at.get("entrega_texto") or _formatar_data_br(at.get("entrega_em", ""))
            linhas.append(f"- **{at.get('disciplina', '-')}** — {at.get('titulo', '-')} ({at.get('tipo') or 'Atividade'}) · Entrega: **{dt_txt}**")
    else:
        linhas.append("- Nenhuma atividade/prova pendente listada na API.")

    linhas.extend(["", "## Avisos e Notificações Recentes", ""])
    if avisos:
        for av in avisos[:15]:
            dt_txt = _formatar_data_br(av.get("data", ""))
            cat = av.get("categoria") or av.get("origem") or "Aviso"
            desc = (av.get("descricao") or "").replace("\n", " ").strip()
            if len(desc) > 220:
                desc = desc[:217] + "..."
            extra = f" — {desc}" if desc else ""
            linhas.append(f"- **[{dt_txt}] [{cat}] {av.get('titulo', '-')}**{extra}")
    else:
        linhas.append("- Nenhum aviso recente retornado.")

    linhas.extend(["", "## Materiais e PDFs", ""])
    if materiais:
        por_disc: dict[str, list[dict]] = {}
        for m in materiais:
            por_disc.setdefault(m.get("disciplina") or "Geral", []).append(m)
        for disc, lista in sorted(por_disc.items()):
            linhas.append(f"### {disc} ({len(lista)})")
            for m in lista[:40]:
                ext = (m.get("extensao") or "").upper()
                tag_local = " *(local)*" if m.get("arquivo_local") else ""
                tag_novo = " **(novo)**" if m.get("baixado_agora") else ""
                linhas.append(f"- [{ext or 'ARQ'}] {m.get('titulo', '-')}{tag_local}{tag_novo}")
            if len(lista) > 40:
                linhas.append(f"- ... e mais {len(lista) - 40} material(is).")
            linhas.append("")
    else:
        linhas.append("- Nenhum material listado na API.")

    return "\n".join(linhas).strip() + "\n"


def _coletar_dados_academicos(
    req,
    sessao_mem: dict,
    headers_api: dict[str, str],
    avisos_sync: list[str],
    page=None,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], int]:
    """Coleta curso, disciplinas, notas/faltas, atividades, materiais/PDFs e avisos via API JSON."""
    disciplinas: list[dict] = []
    notas_faltas: list[dict] = []
    atividades: list[dict] = []
    materiais: list[dict] = []
    avisos: list[dict] = []
    pdfs_novos_baixados = 0

    # 1) Cursos (MyCourses/courses)
    courses_json = sessao_mem["capturados"].get("courses") or _api_get_json(
        req,
        "https://apistudent.ulife.com.br/MyCourses/courses?"
        + urlencode({
            "EnableOrderByFilter": "true",
            "EnableResumeCategory": "true",
            "EnableSearchDisciplines": "false",
            "GetUnenrolledDisciplineFromElasticSearch": "true",
            "HideCalendarLink": "true",
            "HideGradeLink": "false",
            "HideGradeName": "false",
            "ShowGradeInStudentDisciplineCard": "false",
            "ShowGradeInStudentDisciplineCardHome": "true",
            "ShowUnenrolledList": "false",
            "ViewTeacherMessage": "true",
        }),
        headers_api,
    )
    if isinstance(courses_json, dict) and isinstance(courses_json.get("Result"), list) and courses_json["Result"]:
        c0 = courses_json["Result"][0]
        if isinstance(c0, dict):
            sessao_mem["course_id"] = _para_int(c0.get("CourseId")) or sessao_mem["course_id"]
            sessao_mem["org_id"] = _para_int(c0.get("OrganizationId")) or sessao_mem["org_id"]
            sessao_mem["course_name"] = str(c0.get("CourseName") or "").strip()
            sessao_mem["institution_name"] = str(c0.get("OrganizationName") or "").strip()
            sessao_mem["ra"] = str(c0.get("RA") or "").strip()
            sessao_mem["course_status"] = str(c0.get("CourseStatus") or c0.get("StatusDescription") or "").strip()

    # 2) Disciplinas (Student/Disciplines + GetDropdownDiscipline + ResumeCategory)
    disc_json = sessao_mem["capturados"].get("disciplines") or _api_get_json(
        req,
        "https://apistudent.ulife.com.br/Student/Disciplines?"
        + urlencode({
            "EnableOrderByFilter": "true",
            "EnableResumeCategory": "true",
            "EnableSearchDisciplines": "false",
            "FirstLoad": "true",
            "GetUnenrolledDisciplineFromElasticSearch": "true",
            "HideCalendarLink": "true",
            "HideGradeLink": "false",
            "HideGradeName": "false",
            "PageIndex": "1",
            "PageSize": "50",
            "ReplaceDisciplineCardImages": "false",
            "SearchCriteria": "",
            "ShowGradeInStudentDisciplineCard": "false",
            "ShowGradeInStudentDisciplineCardHome": "true",
            "ShowUnenrolledList": "false",
            "ViewTeacherMessage": "true",
            "orderByOption": "0",
        }),
        headers_api,
    )
    disciplinas = _extrair_disciplinas_de_payload(disc_json, sessao_mem)
    atividades = _extrair_atividades_de_payload(disc_json)

    # Fallback leve para capturar StudentID se nao veio na Home (apenas quando Playwright esta ativo)
    if not sessao_mem["student_id"] and page is not None:
        try:
            page.goto(URL_MIDIAS_ULIFE, wait_until="domcontentloaded", timeout=15000)
            for _ in range(12):
                if sessao_mem["student_id"] or sessao_mem["capturados"].get("dropdown"):
                    break
                page.wait_for_timeout(400)
            if not sessao_mem["student_id"]:
                html_mid = page.content() or ""
                m_sid = re.search(r"StudentID[\"']?\s*[:=]\s*[\"']?(\d{5,12})", html_mid, re.I)
                if m_sid:
                    sessao_mem["student_id"] = int(m_sid.group(1))
        except Exception:
            pass

    por_cd = {d["class_discipline_id"]: d for d in disciplinas}
    for d in disciplinas:
        if not d.get("student_id") and sessao_mem.get("student_id"):
            d["student_id"] = sessao_mem["student_id"]

    # Complementa e corrige nomes reais de disciplinas via GetDropdownDiscipline
    dd_params = {"FixedDisciplineID": "false", "PageIndex": "1", "PageSize": "50"}
    if sessao_mem["student_id"]:
        dd_params["StudentID"] = str(sessao_mem["student_id"])
    dd_json = sessao_mem["capturados"].get("dropdown") or _api_get_json(
        req,
        "https://apistudent.ulife.com.br/UserMediaClassDiscipline/GetDropdownDiscipline?" + urlencode(dd_params),
        headers_api,
    )
    if isinstance(dd_json, dict) and isinstance(dd_json.get("Result"), list):
        for item in dd_json["Result"]:
            if not isinstance(item, dict):
                continue
            cd_id = _para_int(item.get("value"))
            nome = str(item.get("description") or "").strip()
            if not cd_id or not nome:
                continue
            if cd_id in por_cd:
                _aplicar_nome_disciplina(por_cd[cd_id], nome)
            else:
                novo_d = {
                    "class_discipline_id": cd_id,
                    "class_discipline_crpt": "",
                    "student_class_discipline_id": None,
                    "student_id": sessao_mem.get("student_id"),
                    "course_id": sessao_mem.get("course_id"),
                    "nome": nome,
                    "modalidade": "",
                    "periodo": "",
                    "professor": "",
                    "faltas": None,
                    "max_faltas": None,
                    "frequencia_pct": None,
                    "nota_card": None,
                    "situacao": "",
                    "aulas_vistas": None,
                    "aulas_total": None,
                }
                disciplinas.append(novo_d)
                por_cd[cd_id] = novo_d

    # Enriquece aulas vistas e nome real via ResumeCategory/disciplines
    resume_json = sessao_mem["capturados"].get("resume") or _api_get_json(
        req,
        "https://apistudent.ulife.com.br/ResumeCategory/disciplines?"
        + urlencode({
            "EnableOrderByFilter": "true",
            "EnableResumeCategory": "true",
            "GroupHomeClassDisciplineStudent": "true",
            "ViewTeacherMessage": "true",
        }),
        headers_api,
    )
    if isinstance(resume_json, dict) and isinstance(resume_json.get("Result"), list):
        for r_item in resume_json["Result"]:
            if not isinstance(r_item, dict):
                continue
            cd_id = _para_int(r_item.get("ClassDisciplineId") or r_item.get("ClassDisciplineID"))
            if cd_id and cd_id in por_cd:
                por_cd[cd_id]["aulas_vistas"] = _para_int(r_item.get("LessonCategoryViewedCount"))
                por_cd[cd_id]["aulas_total"] = _para_int(r_item.get("LessonCategoryCount"))
                nm_r = str(r_item.get("DisciplineName") or "").strip()
                if nm_r:
                    _aplicar_nome_disciplina(por_cd[cd_id], nm_r)

    # Enriquece nomes reais e professores via servico-quadro-aulas (fonte autoritativa por codigoDiario)
    if sessao_mem["student_id"]:
        hoje = date.today()
        ini_sem = (hoje - timedelta(days=21)).isoformat()
        fim_sem = (hoje + timedelta(days=21)).isoformat()
        aulas_json = _api_get_json(
            req,
            f"https://cloudapp.animaeducacao.com.br/servico-quadro-aulas/api/v1/alunos/{sessao_mem['student_id']}/aulas?"
            + urlencode({"dataInicio": ini_sem, "dataFim": fim_sem}),
            headers_api,
            timeout_ms=12000,
        )
        if isinstance(aulas_json, dict):
            obj_ret = aulas_json.get("objetoRetorno") or {}
            por_cd_str = {str(d["class_discipline_id"]): d for d in disciplinas}
            for aula in obj_ret.get("aulas") or []:
                if not isinstance(aula, dict):
                    continue
                cd_str = str(aula.get("codigoDiario") or "")
                if cd_str not in por_cd_str:
                    continue
                disc_alvo = por_cd_str[cd_str]
                nm_a = str(aula.get("disciplina") or "").strip()
                if nm_a:
                    _aplicar_nome_disciplina(disc_alvo, nm_a)
                profs_diario: list[str] = []
                for dt_item in aula.get("datas") or []:
                    for h_item in (dt_item.get("horarios") or []) if isinstance(dt_item, dict) else []:
                        prof = normalizar_nome_pessoa(str((h_item or {}).get("professor") or ""))
                        if prof and prof not in profs_diario:
                            profs_diario.append(prof)
                if profs_diario:
                    # Sobrescreve sempre com o(s) docente(s) reais deste codigoDiario especifico
                    disc_alvo["professor"] = ", ".join(profs_diario[:2])

        comp_json = _api_get_json(
            req,
            f"https://cloudapp.animaeducacao.com.br/servico-quadro-aulas/api/v1/alunos/{sessao_mem['student_id']}/componentes-curriculares?"
            + urlencode({"inicioJanela": f"{hoje.year}-01-01", "fimJanela": f"{hoje.year}-12-31"}),
            headers_api,
            timeout_ms=12000,
        )
        if isinstance(comp_json, dict):
            comps = ((comp_json.get("objetoRetorno") or {}).get("componentesCurriculares")) or []
            por_cd_str = {str(d["class_discipline_id"]): d for d in disciplinas}
            for comp in comps:
                if not isinstance(comp, dict):
                    continue
                nm_comp = str(comp.get("descricao") or "").strip()
                for ch_item in comp.get("cargasHorarias") or []:
                    for diario in (ch_item.get("diarios") or []) if isinstance(ch_item, dict) else []:
                        cd_str = str((diario or {}).get("codigoDiario") or "")
                        if cd_str not in por_cd_str:
                            continue
                        disc_alvo = por_cd_str[cd_str]
                        if nm_comp:
                            _aplicar_nome_disciplina(disc_alvo, nm_comp)
                        if not disc_alvo.get("professor"):
                            for h_item in (diario or {}).get("horarios") or []:
                                prof_h = normalizar_nome_pessoa(str((h_item or {}).get("professor") or ""))
                                if prof_h:
                                    disc_alvo["professor"] = prof_h
                                    break

    # Atividades / Provas online (ActivityList/GetActivities)
    act_json = sessao_mem["capturados"].get("activities") or _api_get_json(
        req,
        "https://apistudent.ulife.com.br/ActivityList/GetActivities?"
        + urlencode({
            "ActivityTabs": "0",
            "FirstLoad": "true",
            "PageIndex": "1",
            "PageSize": "30",
            "SearchCriteria": "",
        }),
        headers_api,
    )
    extras_at = _extrair_atividades_de_payload(act_json)
    if extras_at:
        vistos_at = {(a["disciplina"].lower(), a["titulo"].lower(), a["entrega_em"]) for a in atividades}
        for a in extras_at:
            ch = (a["disciplina"].lower(), a["titulo"].lower(), a["entrega_em"])
            if ch not in vistos_at:
                atividades.append(a)
                vistos_at.add(ch)

    # Cruzamento final: usa cd=<ClassDisciplineCrpt> das atividades para garantir o nome real da disciplina
    por_crpt = {
        unquote(str(d.get("class_discipline_crpt") or "")).strip(): d
        for d in disciplinas
        if d.get("class_discipline_crpt")
    }
    for at in atividades:
        url_at = at.get("url") or ""
        nm_at = (at.get("disciplina") or "").strip()
        if not url_at or not nm_at:
            continue
        qs_at = parse_qs(urlparse(url_at).query)
        cd_at = unquote(qs_at["cd"][0]).strip() if qs_at.get("cd") else ""
        if cd_at and cd_at in por_crpt:
            _aplicar_nome_disciplina(por_crpt[cd_at], nm_at)

    for d in disciplinas:
        d["professor"] = normalizar_nome_pessoa(d.get("professor") or "")
    disciplinas.sort(key=lambda d: (d["nome"].lower(), d["modalidade"].lower()))

    # 3) Notas e Faltas (StudentGrade/GetStudentGrades por disciplina)
    for disc in disciplinas:
        cd_id = disc["class_discipline_id"]
        params_grade = {
            "ClassDisciplineID": str(cd_id),
            "ClassDisciplineId": str(cd_id),
            "DisciplineName": disc["nome"],
            "EnableThanksTeacher": "false",
            "FirstLoad": "true",
            "GroupHomeClassDisciplineStudent": "true",
            "PageIndex": "1",
            "PageSize": "50",
            "SearchCriteria": "",
            "ViewTeacherMessage": "true",
            "lessonPageTab": "0",
        }
        if disc.get("periodo"):
            params_grade["ModuleStepName"] = disc["periodo"]
        if disc.get("course_id"):
            params_grade["OrganizationCourseID"] = str(disc["course_id"])
        if disc.get("student_class_discipline_id"):
            params_grade["StudentClassDisciplineID"] = str(disc["student_class_discipline_id"])
        if disc.get("modalidade"):
            params_grade["TimeLoadTypeName"] = disc["modalidade"]

        grades_json = _api_get_json(
            req,
            "https://apistudent.ulife.com.br/StudentGrade/GetStudentGrades?" + urlencode(params_grade),
            headers_api,
        )
        nf = _parsear_notas_disciplina(disc, grades_json, sessao_mem)
        notas_faltas.append(nf)
        disc["nota_total"] = nf["nota_total"]
        disc["peso_total"] = nf["peso_total"]
        if nf["faltas"] is not None:
            disc["faltas"] = nf["faltas"]
        if nf["frequencia_pct"] is not None:
            disc["frequencia_pct"] = nf["frequencia_pct"]

    # 4) Materiais e PDFs (UserMediaClassDiscipline/GetUserMediaList por disciplina)
    vistos_midia: set[str] = set()
    for disc in disciplinas:
        cd_id = disc["class_discipline_id"]
        pasta_mat = _resolver_pasta_materia(disc["nome"], disc.get("modalidade", ""), disc.get("professor", ""))

        for pagina_idx in range(1, 5):
            params_midia = {
                "ClassDisciplineID": str(cd_id),
                "ClassDisciplineId": str(cd_id),
                "DisciplineName": disc["nome"],
                "EnableThanksTeacher": "false",
                "FirstLoad": "true" if pagina_idx == 1 else "false",
                "FixedDisciplineID": "true",
                "GroupHomeClassDisciplineStudent": "true",
                "PageIndex": str(pagina_idx),
                "PageSize": "50",
                "SearchCriteria": "",
                "ViewTeacherMessage": "true",
                "lessonPageTab": "0",
            }
            if disc.get("class_discipline_crpt"):
                params_midia["ClassDisciplineCrpt"] = disc["class_discipline_crpt"]
            if disc.get("periodo"):
                params_midia["ModuleStepName"] = disc["periodo"]
            if disc.get("course_id"):
                params_midia["OrganizationCourseID"] = str(disc["course_id"])
            if disc.get("student_class_discipline_id"):
                params_midia["StudentClassDisciplineID"] = str(disc["student_class_discipline_id"])
            if disc.get("student_id") or sessao_mem.get("student_id"):
                params_midia["StudentID"] = str(disc.get("student_id") or sessao_mem["student_id"])
            if disc.get("modalidade"):
                params_midia["TimeLoadTypeName"] = disc["modalidade"]

            midia_json = _api_get_json(
                req,
                "https://apistudent.ulife.com.br/UserMediaClassDiscipline/GetUserMediaList?"
                + urlencode(params_midia),
                headers_api,
            )
            lista_m = midia_json.get("Result") if isinstance(midia_json, dict) else None
            if not isinstance(lista_m, list) or not lista_m:
                break

            for m in lista_m:
                if not isinstance(m, dict):
                    continue
                url_m = str(m.get("Url") or "").strip()
                mid_id = str(m.get("UserMediaID") or url_m or m.get("Title") or "")
                chave_m = f"{cd_id}:{mid_id}"
                if not mid_id or chave_m in vistos_midia:
                    continue
                vistos_midia.add(chave_m)

                titulo_m = str(m.get("Title") or m.get("Description") or "").strip()
                ext_m = str(m.get("Extension") or "").strip().upper()
                eh_pdf = (ext_m == "PDF") or (url_m.lower().split("?")[0].endswith(".pdf"))
                arquivo_local = ""
                baixado_agora = False

                if eh_pdf and url_m and not _eh_url_bloqueada(url_m):
                    nome_pdf = _sanitizar_nome_arquivo(titulo_m, url_m)
                    existente = _pdf_ja_existe(pasta_mat, nome_pdf, url_m)
                    if existente is not None:
                        arquivo_local = f"{pasta_mat.name}/{existente.name}"
                    else:
                        try:
                            r_pdf = req.get(url_m, timeout=30000)
                            if r_pdf.ok:
                                conteudo_pdf = r_pdf.body()
                                if conteudo_pdf and (
                                    conteudo_pdf[:4] == b"%PDF"
                                    or ("pdf" in (r_pdf.headers.get("content-type") or "").lower() and len(conteudo_pdf) > 500)
                                ):
                                    destino_pdf = pasta_mat / nome_pdf
                                    destino_pdf.write_bytes(conteudo_pdf)
                                    arquivo_local = f"{pasta_mat.name}/{destino_pdf.name}"
                                    baixado_agora = True
                                    pdfs_novos_baixados += 1
                        except Exception as e_dl:
                            avisos_sync.append(f"Falha ao baixar PDF '{nome_pdf}': {e_dl}")

                materiais.append({
                    "id": m.get("UserMediaID"),
                    "disciplina": disc["nome"],
                    "titulo": titulo_m,
                    "extensao": ext_m or ("PDF" if eh_pdf else "LINK"),
                    "url": url_m,
                    "arquivo_local": arquivo_local,
                    "baixado_agora": baixado_agora,
                })

            if len(lista_m) < 50:
                break

    # 5) Avisos e Notificacoes (apisocial.ulife.com.br)
    if not sessao_mem["user_id"] and page is not None:
        try:
            page.goto(URL_SOCIAL_ULIFE, wait_until="domcontentloaded", timeout=15000)
            for _ in range(8):
                if sessao_mem["user_id"] and sessao_mem["capturados"].get("feeds"):
                    break
                page.wait_for_timeout(400)
        except Exception:
            pass

    headers_social = {
        **headers_api,
        "Origin": "https://social.ulife.com.br",
        "Referer": "https://social.ulife.com.br/",
    }

    notif_json = sessao_mem["capturados"].get("notifications")
    if not notif_json and sessao_mem["user_id"]:
        notif_json = _api_get_json(
            req,
            f"https://apisocial.ulife.com.br/users/{sessao_mem['user_id']}/notifications-summarized",
            headers_social,
        ) or _api_get_json(
            req,
            f"https://apisocial.ulife.com.br/users/{sessao_mem['user_id']}/notifications?pageIndex=1",
            headers_social,
        )

    feeds_json = sessao_mem["capturados"].get("feeds")
    if not feeds_json and sessao_mem["user_id"]:
        feed_params = {
            "FeedId": "0",
            "FeedParentId": str(sessao_mem["user_id"]),
            "FeedType": "0",
            "FirstLoad": "true",
            "HideFeedComment": "false",
            "OnlyPinned": "false",
            "PageIndex": "1",
            "PageSize": "15",
            "SearchCriteria": "",
            "UserId": str(sessao_mem["user_id"]),
        }
        if sessao_mem["org_id"]:
            feed_params["OrganizationId"] = str(sessao_mem["org_id"])
        if sessao_mem["user_org_id"]:
            feed_params["UserOrganizationId"] = str(sessao_mem["user_org_id"])
        feeds_json = _api_get_json(
            req,
            "https://apisocial.ulife.com.br/Feed/ListFeeds?" + urlencode(feed_params),
            headers_social,
        )

    commitments_json = sessao_mem["capturados"].get("commitments")
    if not commitments_json and sessao_mem["user_org_id"]:
        commitments_json = _api_get_json(
            req,
            f"https://apisocial.ulife.com.br/users/{sessao_mem['user_org_id']}/commitments",
            headers_social,
        )

    avisos = _extrair_avisos(notif_json, feeds_json, commitments_json, limite=15)
    return disciplinas, notas_faltas, atividades, materiais, avisos, pdfs_novos_baixados


def _tentar_sync_via_httpx(sessao_mem: dict, avisos_sync: list[str]):
    """Tenta sincronizar em ~1s usando apenas ulife_cookies.json + httpx (zero navegador)."""
    salvo = _carregar_sessao_cookies()
    if not salvo:
        return None

    meta_val = salvo.get("meta")
    meta_salva: dict = meta_val if isinstance(meta_val, dict) else {}
    for k in ("user_id", "user_org_id", "org_id", "student_id", "course_id"):
        if meta_salva.get(k) and not sessao_mem.get(k):
            sessao_mem[k] = meta_salva[k]
    if salvo.get("access_token"):
        sessao_mem["access_token"] = str(salvo["access_token"])
    if salvo.get("auth_header"):
        sessao_mem["auth_header"] = str(salvo["auth_header"])

    jar = _montar_jar_httpx(salvo.get("cookies") or [])
    if not jar:
        return None

    headers_api = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://student.ulife.com.br",
        "Referer": "https://student.ulife.com.br/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if sessao_mem["auth_header"]:
        headers_api["Authorization"] = sessao_mem["auth_header"]
    elif sessao_mem["access_token"]:
        headers_api["Authorization"] = f"Bearer {sessao_mem['access_token']}"

    url_probe = (
        "https://apistudent.ulife.com.br/Student/Disciplines?"
        + urlencode({
            "EnableOrderByFilter": "true",
            "EnableResumeCategory": "true",
            "EnableSearchDisciplines": "false",
            "FirstLoad": "true",
            "GetUnenrolledDisciplineFromElasticSearch": "true",
            "HideCalendarLink": "true",
            "HideGradeLink": "false",
            "HideGradeName": "false",
            "PageIndex": "1",
            "PageSize": "50",
            "ReplaceDisciplineCardImages": "false",
            "SearchCriteria": "",
            "ShowGradeInStudentDisciplineCard": "false",
            "ShowGradeInStudentDisciplineCardHome": "true",
            "ShowUnenrolledList": "false",
            "ViewTeacherMessage": "true",
            "orderByOption": "0",
        })
    )

    try:
        with httpx.Client(cookies=jar, follow_redirects=False, timeout=20.0) as client:
            req_h = _RequisitorHttpx(client)

            # Tenta renovar AccessToken silenciosamente via cookies se possivel
            try:
                r_tok = req_h.post("https://api.ulife.com.br/authentication/token", headers=headers_api, timeout=10000)
                if r_tok.ok and r_tok.body():
                    d_tok = json.loads(r_tok.body().decode("utf-8", errors="replace"))
                    tok_novo = (d_tok.get("Result") or {}).get("AccessToken") if isinstance(d_tok, dict) else None
                    if tok_novo:
                        sessao_mem["access_token"] = str(tok_novo)
                        headers_api["Authorization"] = f"Bearer {tok_novo}"
            except Exception:
                pass

            # Probe de autenticacao em Student/Disciplines
            r_probe = req_h.get(url_probe, headers=headers_api, timeout=15000)
            if _eh_resposta_nao_autenticada(r_probe) and "Authorization" in headers_api:
                sem_auth = {k: v for k, v in headers_api.items() if k != "Authorization"}
                r_probe = req_h.get(url_probe, headers=sem_auth, timeout=15000)
                if r_probe.ok and not _eh_resposta_nao_autenticada(r_probe):
                    headers_api = sem_auth

            if not r_probe.ok or _eh_resposta_nao_autenticada(r_probe):
                return None

            disc_json = json.loads(r_probe.body().decode("utf-8", errors="replace"))
            if not isinstance(disc_json, dict) or disc_json.get("Success") is False:
                return None

            sessao_mem["capturados"]["disciplines"] = disc_json
            coleta = _coletar_dados_academicos(req_h, sessao_mem, headers_api, avisos_sync, page=None)
            if not coleta[0]:
                return None
            return coleta
    except Exception:
        return None


def _sincronizar_ulife_sync() -> dict:
    """Executa sincronizacao silenciosa do Ulife (httpx+cookies primeiro; Playwright fora da tela so se expirado)."""
    PASTA_PERFIL.mkdir(parents=True, exist_ok=True)
    PASTA_PDFS.mkdir(parents=True, exist_ok=True)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Estado em memoria durante a sessao (nunca salvo em memoria.db ou Obsidian)
    sessao_mem: dict = {
        "auth_header": "",
        "access_token": "",
        "user_id": None,
        "user_org_id": None,
        "org_id": None,
        "student_id": None,
        "course_id": None,
        "course_name": "",
        "institution_name": "",
        "ra": "",
        "course_status": "",
        "capturados": {},
    }
    avisos_sync: list[str] = []

    # Caminho rapido (zero navegador): reutiliza ulife_cookies.json via httpx
    modo_sync = "httpx_cookies"
    coleta_rapida = _tentar_sync_via_httpx(sessao_mem, avisos_sync)

    if coleta_rapida is not None:
        disciplinas, notas_faltas, atividades, materiais, avisos, pdfs_novos_baixados = coleta_rapida
    else:
        # Fallback silencioso: Playwright fora da tela (sem roubar foco) para renovar cookies
        modo_sync = "playwright_silencioso"
        sessao_mem["capturados"].clear()

        def _inspecionar_url_ids(url: str):
            if _eh_url_bloqueada(url):
                return
            m_u = re.search(r"apisocial\.ulife\.com\.br/users/(\d+)/notifications", url, re.I)
            if m_u and not sessao_mem["user_id"]:
                sessao_mem["user_id"] = int(m_u.group(1))
            m_c = re.search(r"apisocial\.ulife\.com\.br/users/(\d+)/commitments", url, re.I)
            if m_c and not sessao_mem["user_org_id"]:
                sessao_mem["user_org_id"] = int(m_c.group(1))
            qs = parse_qs(urlparse(url).query)
            for chave_qs, chave_mem in (
                ("StudentID", "student_id"),
                ("UserId", "user_id"),
                ("UserOrganizationId", "user_org_id"),
                ("OrganizationId", "org_id"),
            ):
                vals = qs.get(chave_qs)
                if vals and vals[0].isdigit() and not sessao_mem[chave_mem]:
                    sessao_mem[chave_mem] = int(vals[0])

        def _ao_requisitar(req):
            try:
                url = req.url or ""
                if _eh_url_bloqueada(url):
                    return
                _inspecionar_url_ids(url)
                if "ulife.com.br" in url.lower():
                    auth = req.headers.get("authorization") or req.headers.get("Authorization")
                    if auth and not sessao_mem["auth_header"]:
                        sessao_mem["auth_header"] = auth
            except Exception:
                pass

        def _ao_responder(resp):
            try:
                url = resp.url or ""
                if _eh_url_bloqueada(url) or resp.status != 200:
                    return
                _inspecionar_url_ids(url)
                ctype = (resp.headers.get("content-type") or "").lower()
                if "application/json" not in ctype:
                    return
                low = url.lower()
                alvos = (
                    "/authentication/token",
                    "/mycourses/courses",
                    "/student/disciplines",
                    "/activitylist/getactivities",
                    "/resumecategory/disciplines",
                    "/notifications-summarized",
                    "/feed/listfeeds",
                    "/commitments",
                    "/getdropdowndiscipline",
                )
                if not any(a in low for a in alvos):
                    return
                raw = resp.body()
                if not raw:
                    return
                dados = json.loads(raw.decode("utf-8", errors="replace"))
                if "/authentication/token" in low and isinstance(dados, dict):
                    tok = (dados.get("Result") or {}).get("AccessToken")
                    if tok:
                        sessao_mem["access_token"] = str(tok)
                        jwt_data = _decodificar_jwt_payload(str(tok))
                        for k_jwt, k_mem in (
                            ("UserId", "user_id"),
                            ("userId", "user_id"),
                            ("StudentId", "student_id"),
                            ("studentId", "student_id"),
                            ("UserOrganizationId", "user_org_id"),
                            ("OrganizationId", "org_id"),
                        ):
                            v = _para_int(jwt_data.get(k_jwt))
                            if v and not sessao_mem[k_mem]:
                                sessao_mem[k_mem] = v
                elif "/mycourses/courses" in low:
                    sessao_mem["capturados"]["courses"] = dados
                elif "/student/disciplines" in low:
                    sessao_mem["capturados"]["disciplines"] = dados
                elif "/activitylist/getactivities" in low:
                    sessao_mem["capturados"]["activities"] = dados
                elif "/resumecategory/disciplines" in low:
                    sessao_mem["capturados"]["resume"] = dados
                elif "/notifications-summarized" in low:
                    sessao_mem["capturados"]["notifications"] = dados
                elif "/feed/listfeeds" in low:
                    sessao_mem["capturados"]["feeds"] = dados
                elif "/commitments" in low:
                    sessao_mem["capturados"]["commitments"] = dados
                elif "/getdropdowndiscipline" in low:
                    sessao_mem["capturados"]["dropdown"] = dados
            except Exception:
                pass

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PASTA_PERFIL),
                headless=False,
                args=["--window-position=-32000,-32000", "--window-size=1280,800"],
                viewport={"width": 1280, "height": 800},
                locale="pt-BR",
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.on("request", _ao_requisitar)
                page.on("response", _ao_responder)

                page.goto(URL_HOME_ULIFE, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)

                autenticado, msg_login = _garantir_login_ulife(page)
                if not autenticado:
                    return {"ok": False, "erro": msg_login}

                # Aguarda os XHRs iniciais da Home para obter cookies, AccessToken e IDs
                for _ in range(10):
                    if sessao_mem["capturados"].get("disciplines") and (
                        sessao_mem["access_token"] or sessao_mem["auth_header"]
                    ):
                        break
                    page.wait_for_timeout(450)

                # Tenta ler IDs extras do storage/DOM (sem gravar nada sensivel)
                try:
                    js_ids = page.evaluate(
                        """() => {
                            const out = {};
                            const sources = [window.localStorage, window.sessionStorage];
                            for (const st of sources) {
                                if (!st) continue;
                                for (let i = 0; i < st.length; i++) {
                                    const k = st.key(i) || '';
                                    const v = st.getItem(k) || '';
                                    if (/userid|studentid|organizationid/i.test(k) && /^\\d+$/.test(v.trim())) {
                                        out[k] = Number(v.trim());
                                    }
                                }
                            }
                            return out;
                        }"""
                    )
                    if isinstance(js_ids, dict):
                        for k, v in js_ids.items():
                            kl = k.lower()
                            if "userorganizationid" in kl and not sessao_mem["user_org_id"]:
                                sessao_mem["user_org_id"] = int(v)
                            elif "userid" in kl and not sessao_mem["user_id"]:
                                sessao_mem["user_id"] = int(v)
                            elif "studentid" in kl and not sessao_mem["student_id"]:
                                sessao_mem["student_id"] = int(v)
                            elif "organizationid" in kl and not sessao_mem["org_id"]:
                                sessao_mem["org_id"] = int(v)
                except Exception:
                    pass

                # Salva cookies atualizados logo apos o login bem-sucedido
                _salvar_sessao_cookies(context, sessao_mem)

                headers_api = {
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://student.ulife.com.br",
                    "Referer": "https://student.ulife.com.br/",
                }
                if sessao_mem["auth_header"]:
                    headers_api["Authorization"] = sessao_mem["auth_header"]
                elif sessao_mem["access_token"]:
                    headers_api["Authorization"] = f"Bearer {sessao_mem['access_token']}"

                req_pw = _RequisitorPlaywright(context.request)
                disciplinas, notas_faltas, atividades, materiais, avisos, pdfs_novos_baixados = (
                    _coletar_dados_academicos(req_pw, sessao_mem, headers_api, avisos_sync, page=page)
                )

                # Re-salva cookies + IDs (student_id/user_id descobertos durante a coleta)
                _salvar_sessao_cookies(context, sessao_mem)
            finally:
                with suppress(Exception):
                    context.close()

    # Indexa PDFs novos no banco de estudos
    pdfs_indexados = sincronizar_indice_pdfs()

    resumo = {
        "curso": sessao_mem["course_name"],
        "instituicao": sessao_mem["institution_name"],
        "ra": sessao_mem["ra"],
        "status_curso": sessao_mem["course_status"],
        "total_disciplinas": len(disciplinas),
        "total_materiais": len(materiais),
        "pdfs_novos_baixados": pdfs_novos_baixados,
        "pdfs_indexados": pdfs_indexados,
        "total_avisos": len(avisos),
        "total_atividades": len(atividades),
        "sincronizado_em": agora,
        "modo_sync": modo_sync,
        "avisos_sync": avisos_sync[:10],
        "campos_detectados": sessao_mem.get("campos_detectados") or {},
    }

    # Salva secoes estruturadas no memoria.db
    _salvar_secao_db("resumo", resumo, agora)
    _salvar_secao_db("disciplinas", disciplinas, agora)
    _salvar_secao_db("notas_faltas", notas_faltas, agora)
    _salvar_secao_db("atividades", atividades, agora)
    _salvar_secao_db("materiais", materiais, agora)
    _salvar_secao_db("avisos", avisos, agora)

    # Gera/atualiza 01-Faculdade/Painel-Ulife.md no Obsidian Vault
    md_obsidian = _gerar_markdown_obsidian(resumo, notas_faltas, atividades, avisos, materiais)
    status_obsidian = escrever_nota_vault(NOTA_OBSIDIAN_PAINEL, md_obsidian, anexar=False)

    return {
        "ok": True,
        "resumo": resumo,
        "disciplinas": disciplinas,
        "notas_faltas": notas_faltas,
        "atividades": atividades,
        "materiais": materiais,
        "avisos": avisos,
        "obsidian": status_obsidian,
    }


async def sincronizar_ulife() -> dict:
    """Executa sincronizacao Ulife v2 em thread isolada (com lock para evitar conflito no perfil)."""
    async with _SYNC_LOCK:
        return await asyncio.to_thread(_sincronizar_ulife_sync)


def obter_dados_ulife_estruturados(secao: str = "tudo") -> dict:
    """Le instantaneamente os dados estruturados do Ulife salvos no memoria.db (sem abrir navegador)."""
    chave = _normalizar_ascii(secao or "tudo")
    with _conn() as c:
        rows = c.execute("SELECT secao, dados_json, atualizado_em FROM ulife_dados").fetchall()

    banco: dict[str, dict | list] = {}
    ultima_att = ""
    for sec, djson, att in rows:
        try:
            banco[sec] = json.loads(djson)
        except Exception:
            banco[sec] = []
        if att and att > ultima_att:
            ultima_att = att

    if not banco:
        return {
            "sincronizado": False,
            "atualizado_em": None,
            "resumo": {},
            "disciplinas": [],
            "notas_faltas": [],
            "atividades": [],
            "materiais": [],
            "avisos": [],
        }

    mapa_alias = {
        "resumo": "resumo",
        "disciplinas": "disciplinas",
        "materias": "disciplinas",
        "notas": "notas_faltas",
        "faltas": "notas_faltas",
        "frequencia": "notas_faltas",
        "boletim": "notas_faltas",
        "notas_faltas": "notas_faltas",
        "atividades": "atividades",
        "provas": "atividades",
        "materiais": "materiais",
        "pdfs": "materiais",
        "avisos": "avisos",
        "notificacoes": "avisos",
    }

    for sec in ("disciplinas", "notas_faltas"):
        lista_sec = banco.get(sec)
        if isinstance(lista_sec, list):
            for item_sec in lista_sec:
                if isinstance(item_sec, dict) and item_sec.get("professor"):
                    item_sec["professor"] = normalizar_nome_pessoa(item_sec["professor"])

    if chave != "tudo" and chave in mapa_alias:
        alvo = mapa_alias[chave]
        return {
            "sincronizado": True,
            "atualizado_em": ultima_att,
            "secao": alvo,
            alvo: banco.get(alvo, [] if alvo != "resumo" else {}),
        }

    return {
        "sincronizado": True,
        "atualizado_em": ultima_att,
        "resumo": banco.get("resumo", {}),
        "disciplinas": banco.get("disciplinas", []),
        "notas_faltas": banco.get("notas_faltas", []),
        "atividades": banco.get("atividades", []),
        "materiais": banco.get("materiais", []),
        "avisos": banco.get("avisos", []),
    }


def consultar_dados_ulife(secao: str = "tudo") -> str:
    """Consulta instantanea no memoria.db (sem abrir navegador) dos dados sincronizados do Ulife."""
    dados = obter_dados_ulife_estruturados("tudo")
    if not dados.get("sincronizado"):
        return (
            "Nenhum dado do Ulife sincronizado no banco ainda. "
            "Use `sincronizar_ulife_agora()` ou clique no botao 'Sincronizar Ulife' na aba Estudos."
        )

    alvo = _normalizar_ascii(secao or "tudo")
    resumo = dados.get("resumo") or {}
    notas_faltas = dados.get("notas_faltas") or []
    atividades = dados.get("atividades") or []
    materiais = dados.get("materiais") or []
    avisos = dados.get("avisos") or []

    linhas = [
        f"=== DADOS ULIFE (atualizado em {dados.get('atualizado_em') or '-'}) ===",
        f"Curso: {resumo.get('curso') or '-'} | Instituicao: {resumo.get('instituicao') or '-'} | RA: {resumo.get('ra') or '-'}",
    ]

    quer_tudo = alvo in ("", "tudo", "all")

    if quer_tudo or alvo in ("disciplinas", "materias", "notas", "faltas", "frequencia", "boletim", "notas_faltas"):
        linhas.append("\n## Disciplinas, Notas e Faltas")
        if not notas_faltas:
            linhas.append("- Nenhuma disciplina encontrada.")
        for nf in notas_faltas:
            avals = []
            for a in nf.get("avaliacoes") or []:
                rot = a.get("item") or a.get("nome") or "Aval."
                n_s = f"{a['nota']:.1f}" if a.get("nota") is not None else "-"
                p_s = f"/{a['peso']:.0f}" if a.get("peso") is not None else ""
                avals.append(f"{rot}={n_s}{p_s}")
            nota_txt = (
                f"{nf['nota_total']:.1f}" + (f"/{nf['peso_total']:.0f}" if nf.get("peso_total") else "")
                if nf.get("nota_total") is not None
                else "N/D"
            )
            f_parts = []
            if nf.get("faltas") is not None:
                f_parts.append(f"faltas={nf['faltas']}" + (f"/{nf['max_faltas']}" if nf.get("max_faltas") else ""))
            if nf.get("frequencia_pct") is not None:
                f_parts.append(f"freq={nf['frequencia_pct']:.0f}%")
            if nf.get("aulas_vistas") is not None and nf.get("aulas_total") is not None:
                f_parts.append(f"aulas={nf['aulas_vistas']}/{nf['aulas_total']}")
            mod = f" ({nf['modalidade']})" if nf.get("modalidade") else ""
            prof = f" | Prof.: {nf['professor']}" if nf.get("professor") else ""
            extra_f = " | " + ", ".join(f_parts) if f_parts else " | Faltas/Freq: N/D"
            extra_av = " | Avaliacoes: " + "; ".join(avals) if avals else ""
            linhas.append(f"- {nf.get('disciplina')}{mod}: Nota={nota_txt}{extra_f}{extra_av}{prof}")

    if quer_tudo or alvo in ("atividades", "provas"):
        linhas.append("\n## Proximas Provas e Atividades (Ulife)")
        if not atividades:
            linhas.append("- Nenhuma atividade pendente.")
        for at in atividades:
            dt = at.get("entrega_texto") or _formatar_data_br(at.get("entrega_em", ""))
            linhas.append(f"- [{dt}] {at.get('disciplina')} — {at.get('titulo')} ({at.get('tipo') or 'Atividade'})")

    if quer_tudo or alvo in ("avisos", "notificacoes"):
        linhas.append(f"\n## Avisos e Notificacoes Recentes ({len(avisos)})")
        if not avisos:
            linhas.append("- Nenhum aviso recente.")
        for av in avisos[:15]:
            dt = _formatar_data_br(av.get("data", ""))
            desc = (av.get("descricao") or "").replace("\n", " ").strip()
            if len(desc) > 200:
                desc = desc[:197] + "..."
            linhas.append(f"- [{dt}] [{av.get('categoria') or av.get('origem')}] {av.get('titulo')}: {desc}")

    if quer_tudo or alvo in ("materiais", "pdfs"):
        linhas.append(f"\n## Materiais e PDFs ({len(materiais)})")
        if not materiais:
            linhas.append("- Nenhum material registrado.")
        else:
            for m in materiais[:40]:
                loc = f" -> local: {m['arquivo_local']}" if m.get("arquivo_local") else ""
                linhas.append(f"- [{m.get('disciplina')}] ({m.get('extensao')}) {m.get('titulo')}{loc}")
            if len(materiais) > 40:
                linhas.append(f"... e mais {len(materiais) - 40} material(is).")

    return "\n".join(linhas)


async def sincronizar_ulife_agora() -> str:
    """Roda a sincronizacao Ulife v2 via API JSON, atualiza memoria.db, PDFs e Obsidian."""
    res = await sincronizar_ulife()
    if not res.get("ok"):
        return f"Erro ao sincronizar Ulife: {res.get('erro', 'falha desconhecida')}"

    r = res["resumo"]
    return (
        f"Sincronizacao Ulife v2 concluida em {r['sincronizado_em']}!\n"
        f"- Curso: {r.get('curso') or '-'} ({r.get('instituicao') or '-'})\n"
        f"- Disciplinas: {r['total_disciplinas']}\n"
        f"- Provas/Atividades: {r['total_atividades']}\n"
        f"- Avisos recentes: {r['total_avisos']}\n"
        f"- Materiais listados: {r['total_materiais']} | PDFs novos baixados: {r['pdfs_novos_baixados']} | PDFs indexados: {r['pdfs_indexados']}\n"
        f"- Obsidian: {res.get('obsidian', NOTA_OBSIDIAN_PAINEL)}"
    )
