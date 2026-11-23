"""Execucao nativa segura no Windows 11 com confirmacao humana e whitelist ("Sempre permitir").

Todas as solicitacoes de abrir aplicativos, URLs/abas no navegador ou arquivos/pastas
sao gravadas na tabela `acoes_windows` em `memoria.db` (SQLite WAL).
- Se o aplicativo estiver na whitelist (`windows_whitelist`) e sem argumentos arriscados,
  e executado imediatamente e marcado como `executada`.
- Caso contrario, fica como `pendente` ate o usuario clicar em Permitir / Sempre permitir / Recusar.

Seguranca:
- Usa apenas `os.startfile` ou `subprocess.Popen([...], shell=False)`. NUNCA usa `shell=True`.
- Bloqueia comandos destrutivos/administrativos (`format`, `del`, `rd`, `reg`, `shutdown`, `cmd`, `powershell`, etc.).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from ferramentas_estudos import PASTA_PDFS
from ferramentas_faxina import obter_pasta_desktop

DB_PATH = Path(__file__).resolve().parent / "memoria.db"
HOME = Path.home().resolve()

COMANDOS_BLOQUEADOS: frozenset[str] = frozenset({
    "format",
    "del",
    "erase",
    "rd",
    "rmdir",
    "reg",
    "regedit",
    "regedt32",
    "shutdown",
    "restart",
    "logoff",
    "bcdedit",
    "diskpart",
    "cipher",
    "takeown",
    "icacls",
    "cacls",
    "schtasks",
    "at",
    "wmic",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "cmd",
    "cmd.exe",
    "mshta",
    "mshta.exe",
    "wscript",
    "wscript.exe",
    "cscript",
    "cscript.exe",
    "rundll32",
    "rundll32.exe",
    "taskkill",
    "taskkill.exe",
    "net",
    "net1",
    "sc",
    "sfc",
    "dism",
    "vssadmin",
    "wbadmin",
    "certutil",
    "bitsadmin",
})

PADROES_PERIGOSOS_ARGS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:&&|\|\||[;|`<>]|\$\()"),
    re.compile(r"\b(?:format|del|erase|rmdir|rd|reg\s+delete|shutdown|diskpart|bcdedit|vssadmin|taskkill|rm\s+-rf)\b", re.I),
)

EXTENSOES_ARQUIVO_BLOQUEADAS: frozenset[str] = frozenset({
    ".bat",
    ".cmd",
    ".ps1",
    ".psm1",
    ".vbs",
    ".vbe",
    ".js",
    ".jse",
    ".wsf",
    ".wsh",
    ".hta",
    ".scr",
    ".com",
    ".pif",
    ".reg",
    ".msc",
    ".msi",
    ".exe",
})

ESQUEMAS_URL_PERMITIDOS: frozenset[str] = frozenset({"http", "https"})

PROGRAM_FILES = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
PROGRAM_FILES_X86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", str(HOME / "AppData" / "Local")))
APPDATA = Path(os.environ.get("APPDATA", str(HOME / "AppData" / "Roaming")))
PROGRAM_DATA = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
WIN_DIR = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")).resolve()

# Catalogo de aliases comuns no Windows 11 (nome canonico -> executaveis, atalhos e caminhos conhecidos)
CATALOGO_APPS: dict[str, dict] = {
    "brave": {
        "nome": "Brave",
        "aliases": ("brave", "brave browser", "navegador brave"),
        "exes": ("brave.exe",),
        "atalhos": ("brave.lnk", "brave browser.lnk"),
        "caminhos": (
            PROGRAM_FILES / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
            PROGRAM_FILES_X86 / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
            LOCAL_APPDATA / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        ),
    },
    "vscode": {
        "nome": "Visual Studio Code",
        "aliases": ("vscode", "vs code", "visual studio code", "code"),
        "exes": ("Code.exe", "code.exe", "code.cmd"),
        "atalhos": ("visual studio code.lnk", "vscode.lnk", "vs code.lnk"),
        "caminhos": (
            LOCAL_APPDATA / "Programs" / "Microsoft VS Code" / "Code.exe",
            PROGRAM_FILES / "Microsoft VS Code" / "Code.exe",
        ),
    },
    "obsidian": {
        "nome": "Obsidian",
        "aliases": ("obsidian",),
        "exes": ("Obsidian.exe",),
        "atalhos": ("obsidian.lnk",),
        "caminhos": (
            LOCAL_APPDATA / "Programs" / "obsidian" / "Obsidian.exe",
            LOCAL_APPDATA / "Obsidian" / "Obsidian.exe",
            PROGRAM_FILES / "Obsidian" / "Obsidian.exe",
        ),
    },
    "spotify": {
        "nome": "Spotify",
        "aliases": ("spotify",),
        "exes": ("Spotify.exe",),
        "atalhos": ("spotify.lnk",),
        "caminhos": (
            APPDATA / "Spotify" / "Spotify.exe",
            LOCAL_APPDATA / "Microsoft" / "WindowsApps" / "Spotify.exe",
        ),
    },
    "discord": {
        "nome": "Discord",
        "aliases": ("discord",),
        "exes": ("Discord.exe",),
        "atalhos": ("discord.lnk",),
        "caminhos": (
            LOCAL_APPDATA / "Discord" / "Update.exe",
        ),
        "args_update": ("--processStart", "Discord.exe"),
    },
    "chrome": {
        "nome": "Google Chrome",
        "aliases": ("chrome", "google chrome"),
        "exes": ("chrome.exe",),
        "atalhos": ("google chrome.lnk", "chrome.lnk"),
        "caminhos": (
            PROGRAM_FILES / "Google" / "Chrome" / "Application" / "chrome.exe",
            PROGRAM_FILES_X86 / "Google" / "Chrome" / "Application" / "chrome.exe",
            LOCAL_APPDATA / "Google" / "Chrome" / "Application" / "chrome.exe",
        ),
    },
    "edge": {
        "nome": "Microsoft Edge",
        "aliases": ("edge", "microsoft edge", "msedge"),
        "exes": ("msedge.exe",),
        "atalhos": ("microsoft edge.lnk", "edge.lnk"),
        "caminhos": (
            PROGRAM_FILES_X86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            PROGRAM_FILES / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ),
    },
    "whatsapp": {
        "nome": "WhatsApp",
        "aliases": ("whatsapp", "zap"),
        "exes": ("WhatsApp.exe",),
        "atalhos": ("whatsapp.lnk",),
        "caminhos": (
            LOCAL_APPDATA / "WhatsApp" / "WhatsApp.exe",
        ),
    },
    "steam": {
        "nome": "Steam",
        "aliases": ("steam",),
        "exes": ("steam.exe",),
        "atalhos": ("steam.lnk",),
        "caminhos": (
            PROGRAM_FILES_X86 / "Steam" / "steam.exe",
            PROGRAM_FILES / "Steam" / "steam.exe",
        ),
    },
    "notepad": {
        "nome": "Bloco de Notas",
        "aliases": ("notepad", "bloco de notas"),
        "exes": ("notepad.exe",),
        "atalhos": ("notepad.lnk", "bloco de notas.lnk"),
        "caminhos": (WIN_DIR / "System32" / "notepad.exe",),
    },
    "calc": {
        "nome": "Calculadora",
        "aliases": ("calc", "calculadora"),
        "exes": ("calc.exe",),
        "atalhos": ("calculadora.lnk",),
        "caminhos": (WIN_DIR / "System32" / "calc.exe",),
    },
    "explorer": {
        "nome": "Explorador de Arquivos",
        "aliases": ("explorer", "explorador", "explorador de arquivos", "windows explorer"),
        "exes": ("explorer.exe",),
        "atalhos": (),
        "caminhos": (WIN_DIR / "explorer.exe",),
    },
    "cursor": {
        "nome": "Cursor",
        "aliases": ("cursor",),
        "exes": ("Cursor.exe",),
        "atalhos": ("cursor.lnk",),
        "caminhos": (
            LOCAL_APPDATA / "Programs" / "cursor" / "Cursor.exe",
        ),
    },
    "notion": {
        "nome": "Notion",
        "aliases": ("notion",),
        "exes": ("Notion.exe",),
        "atalhos": ("notion.lnk",),
        "caminhos": (
            LOCAL_APPDATA / "Programs" / "Notion" / "Notion.exe",
        ),
    },
    "telegram": {
        "nome": "Telegram",
        "aliases": ("telegram",),
        "exes": ("Telegram.exe",),
        "atalhos": ("telegram.lnk",),
        "caminhos": (
            APPDATA / "Telegram Desktop" / "Telegram.exe",
        ),
    },
}


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS acoes_windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL,
                chave_whitelist TEXT NOT NULL DEFAULT '',
                alvo TEXT NOT NULL,
                argumentos TEXT DEFAULT '',
                descricao TEXT NOT NULL,
                risco TEXT NOT NULL DEFAULT 'baixo',
                status TEXT NOT NULL DEFAULT 'pendente',
                erro TEXT DEFAULT '',
                criado_em TEXT NOT NULL,
                executado_em TEXT DEFAULT ''
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS windows_whitelist (
                item TEXT PRIMARY KEY,
                nome_exibicao TEXT NOT NULL,
                criado_em TEXT NOT NULL
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


def _normalizar_ascii(txt: str) -> str:
    sem = unicodedata.normalize("NFKD", (txt or "").strip()).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", sem).strip()


def _normalizar_chave_app(nome_app: str) -> tuple[str, str]:
    """Retorna (chave_whitelist, nome_exibicao) para um app."""
    norm = _normalizar_ascii(nome_app)
    for chave, info in CATALOGO_APPS.items():
        if norm == chave or norm in info["aliases"] or norm == _normalizar_ascii(info["nome"]):
            return chave, info["nome"]
    slug = re.sub(r"[^a-z0-9_-]+", "_", norm).strip("_")
    return (slug or "app"), (nome_app or "").strip() or "Aplicativo"


def _validar_seguranca_comando(nome_ou_alvo: str, args: str = "") -> None:
    """Bloqueia executaveis administrativos/destrutivos e argumentos com injecao de shell."""
    alvo_norm = _normalizar_ascii(nome_ou_alvo)
    if not alvo_norm:
        raise ValueError("Alvo vazio.")

    nome_base = Path(nome_ou_alvo.strip().strip('"')).name.lower()
    stem_base = Path(nome_base).stem.lower()
    tokens_alvo = re.split(r"[\s\\/]+", alvo_norm)

    if nome_base in COMANDOS_BLOQUEADOS or stem_base in COMANDOS_BLOQUEADOS:
        raise ValueError(f"Comando/aplicativo bloqueado por seguranca: '{nome_ou_alvo}'.")
    for tok in tokens_alvo:
        if tok in COMANDOS_BLOQUEADOS:
            raise ValueError(f"Comando bloqueado por seguranca: '{tok}'.")

    args_str = (args or "").strip()
    if args_str:
        for pat in PADROES_PERIGOSOS_ARGS:
            if pat.search(args_str):
                raise ValueError("Argumentos bloqueados por seguranca (padrao potencialmente destrutivo ou operador de shell).")


def _pastas_atalhos_busca() -> list[Path]:
    desktop = obter_pasta_desktop()
    candidatas = [
        desktop / "Atalhos",
        desktop,
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
        APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        PROGRAM_DATA / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    unicas: list[Path] = []
    for p in candidatas:
        try:
            if p.exists() and p.is_dir():
                r = p.resolve()
                if r not in unicas:
                    unicas.append(r)
        except OSError:
            continue
    return unicas


def _buscar_atalho_por_nome(termos: list[str]) -> Path | None:
    termos_norm = [_normalizar_ascii(t).removesuffix(".lnk").removesuffix(".exe") for t in termos if t]
    termos_norm = [t for t in termos_norm if t]
    if not termos_norm:
        return None

    melhor_parcial: Path | None = None
    for base in _pastas_atalhos_busca():
        try:
            for root, dirs, files in os.walk(base, followlinks=False):
                # Limita profundidade a 3 niveis para resposta instantanea
                rel_parts = Path(root).relative_to(base).parts
                if len(rel_parts) >= 3:
                    dirs[:] = []
                for fname in files:
                    if not fname.lower().endswith(".lnk"):
                        continue
                    stem_norm = _normalizar_ascii(Path(fname).stem)
                    if stem_norm in termos_norm:
                        return (Path(root) / fname).resolve()
                    if melhor_parcial is None and any(t in stem_norm for t in termos_norm if len(t) >= 3):
                        melhor_parcial = (Path(root) / fname).resolve()
        except OSError:
            continue
    return melhor_parcial


def _buscar_exe_raso(nome_norm: str) -> Path | None:
    if len(nome_norm) < 3:
        return None
    raizes = [LOCAL_APPDATA / "Programs", PROGRAM_FILES, PROGRAM_FILES_X86]
    alvo_exe = f"{nome_norm}.exe"
    for raiz in raizes:
        if not raiz.exists():
            continue
        try:
            for item in raiz.iterdir():
                if not item.is_dir():
                    continue
                if nome_norm not in _normalizar_ascii(item.name):
                    continue
                for cand in item.rglob("*.exe"):
                    try:
                        if len(cand.relative_to(item).parts) > 3:
                            continue
                    except ValueError:
                        continue
                    if cand.name.lower() == alvo_exe or _normalizar_ascii(cand.stem) == nome_norm:
                        return cand.resolve()
        except OSError:
            continue
    return None


def _resolver_aplicativo(nome_app: str) -> tuple[str, str, str, list[str]]:
    """Resolve um nome de app para `(chave_whitelist, nome_exibicao, caminho_alvo, args_extras)`.

    Procura em: PATH, caminhos conhecidos (`Program Files`, `AppData\\Local`),
    `Desktop\\Atalhos`, `Desktop` e Menu Iniciar do Windows.
    """
    bruto = (nome_app or "").strip()
    _validar_seguranca_comando(bruto)

    # Caso o usuario passe um caminho direto para um .exe ou .lnk existente
    p_direto = Path(os.path.expandvars(bruto)).expanduser()
    if p_direto.is_absolute() and p_direto.is_file() and p_direto.suffix.lower() in (".exe", ".lnk"):
        _validar_seguranca_comando(str(p_direto))
        chave, exib = _normalizar_chave_app(p_direto.stem)
        return chave, exib, str(p_direto.resolve()), []

    chave, exib = _normalizar_chave_app(bruto)
    info = CATALOGO_APPS.get(chave)

    if info:
        # 1) Caminhos conhecidos primeiro (ex.: Brave, VS Code, Obsidian)
        for cand in info.get("caminhos", ()):
            try:
                if cand.is_file():
                    args_extra = list(info.get("args_update", ())) if cand.name.lower() == "update.exe" else []
                    return chave, exib, str(cand.resolve()), args_extra
            except OSError:
                continue

        # 2) PATH
        for exe in info.get("exes", ()):
            achado = shutil.which(exe)
            if achado:
                return chave, exib, str(Path(achado).resolve()), []

        # 3) Atalhos no Desktop\Atalhos, Desktop e Menu Iniciar
        atalho = _buscar_atalho_por_nome([*info.get("atalhos", ()), *info.get("aliases", ()), exib])
        if atalho:
            return chave, exib, str(atalho), []

    # Busca generica para qualquer outro app instalado
    norm = _normalizar_ascii(bruto)
    for cand_which in (bruto, f"{bruto}.exe", norm, f"{norm}.exe"):
        achado = shutil.which(cand_which)
        if achado and Path(achado).suffix.lower() in (".exe", ".lnk"):
            _validar_seguranca_comando(achado)
            return chave, exib, str(Path(achado).resolve()), []

    atalho_gen = _buscar_atalho_por_nome([bruto, norm])
    if atalho_gen:
        return chave, exib, str(atalho_gen), []

    exe_gen = _buscar_exe_raso(norm)
    if exe_gen:
        _validar_seguranca_comando(str(exe_gen))
        return chave, exib, str(exe_gen), []

    raise ValueError(
        f"Aplicativo '{bruto}' nao encontrado no PATH, Desktop\\Atalhos, Desktop, Program Files, AppData\\Local ou Menu Iniciar."
    )


def _resolver_caminho_local(caminho: str) -> Path:
    bruto = (caminho or "").strip().strip('"')
    if not bruto:
        raise ValueError("Informe o caminho do arquivo ou pasta.")

    expandido = os.path.expandvars(bruto)
    p_teste = Path(expandido).expanduser()
    if p_teste.is_absolute():
        alvo = p_teste
    else:
        chave = bruto.replace("\\", "/").strip("/")
        partes = chave.split("/", 1)
        head = _normalizar_ascii(partes[0])
        rest = partes[1] if len(partes) > 1 else ""
        mapa_bases = {
            "desktop": obter_pasta_desktop(),
            "area de trabalho": obter_pasta_desktop(),
            "downloads": (HOME / "Downloads").resolve(),
            "documents": (HOME / "Documents").resolve(),
            "documentos": (HOME / "Documents").resolve(),
            "pictures": (HOME / "Pictures").resolve(),
            "imagens": (HOME / "Pictures").resolve(),
            "videos": (HOME / "Videos").resolve(),
            "faculdade": PASTA_PDFS.resolve(),
            "pdfs_faculdade": PASTA_PDFS.resolve(),
            "obsidian": Path(os.getenv("OBSIDIAN_VAULT", str(HOME / "Documents" / "Athena-Vault"))).expanduser().resolve(),
        }
        if head in mapa_bases:
            alvo = (mapa_bases[head] / rest) if rest else mapa_bases[head]
        else:
            # Tenta tambem dentro de pdfs_faculdade se parecer nome de PDF
            cand_pdf = PASTA_PDFS / bruto
            alvo = cand_pdf if cand_pdf.exists() else p_teste

    res = alvo.resolve()
    if not res.exists():
        raise ValueError(f"Arquivo ou pasta nao encontrado: {res}")

    # Bloqueia abrir pastas criticas do sistema ou scripts executaveis via abrir_arquivo
    if res == WIN_DIR or WIN_DIR in res.parents:
        raise ValueError(f"Acesso bloqueado a pasta do sistema Windows: {res}")
    if res.is_file() and res.suffix.lower() in EXTENSOES_ARQUIVO_BLOQUEADAS:
        raise ValueError(
            f"Extensao '{res.suffix}' bloqueada em abrir arquivo/pasta por seguranca. Para abrir programas, use solicitar_abrir_app."
        )
    return res


def _normalizar_url(url: str) -> str:
    bruta = (url or "").strip()
    if not bruta:
        raise ValueError("Informe uma URL valida.")
    if "://" not in bruta:
        if re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/.*)?$", bruta):
            bruta = "https://" + bruta
        else:
            raise ValueError(f"URL invalida: '{bruta}'. Use http:// ou https://.")
    parsed = urlparse(bruta)
    if (parsed.scheme or "").lower() not in ESQUEMAS_URL_PERMITIDOS or not parsed.netloc:
        raise ValueError(f"Esquema de URL nao permitido (use apenas http/https): '{bruta}'.")
    if any(ch in bruta for ch in ("\r", "\n", "\x00")):
        raise ValueError("URL contem caracteres invalidos.")
    return bruta


def _row_para_dict(row) -> dict:
    return {
        "id": int(row[0]),
        "tipo": row[1],
        "chave_whitelist": row[2],
        "alvo": row[3],
        "argumentos": row[4] or "",
        "descricao": row[5],
        "risco": row[6],
        "status": row[7],
        "erro": row[8] or "",
        "criado_em": row[9],
        "executado_em": row[10] or "",
    }


def esta_na_whitelist(item: str) -> bool:
    chave = _normalizar_ascii(item)
    chave_slug = re.sub(r"[^a-z0-9_-]+", "_", chave).strip("_")
    if not chave_slug:
        return False
    with _conn() as c:
        row = c.execute("SELECT 1 FROM windows_whitelist WHERE item = ?", (chave_slug,)).fetchone()
    return row is not None


def adicionar_whitelist(item: str, nome_exibicao: str = "") -> dict:
    chave, exib_padrao = _normalizar_chave_app(item)
    _validar_seguranca_comando(chave)
    nome_final = (nome_exibicao or exib_padrao or chave).strip()
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO windows_whitelist (item, nome_exibicao, criado_em)
            VALUES (?, ?, ?)
            """,
            (chave, nome_final, agora),
        )
    return {"ok": True, "item": chave, "nome_exibicao": nome_final, "criado_em": agora}


def remover_whitelist(item: str) -> dict:
    chave, _ = _normalizar_chave_app(item)
    chave_raw = _normalizar_ascii(item)
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM windows_whitelist WHERE item = ? OR item = ?",
            (chave, chave_raw),
        )
        removidos = cur.rowcount
    if removidos == 0:
        return {"ok": False, "erro": f"Item '{item}' nao encontrado na whitelist."}
    return {"ok": True, "item": chave, "mensagem": f"'{item}' removido da whitelist."}


def listar_whitelist() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT item, nome_exibicao, criado_em FROM windows_whitelist ORDER BY nome_exibicao ASC"
        ).fetchall()
    return [{"item": r[0], "nome_exibicao": r[1], "criado_em": r[2]} for r in rows]


def obter_status_acoes_windows(ids: list[int]) -> dict[int, dict]:
    ids_limpos = [int(i) for i in ids if isinstance(i, int) or str(i).isdigit()]
    if not ids_limpos:
        return {}
    placeholders = ",".join("?" for _ in ids_limpos)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT id, tipo, chave_whitelist, alvo, argumentos, descricao, risco, status, erro, criado_em, executado_em
            FROM acoes_windows
            WHERE id IN ({placeholders})
            """,
            ids_limpos,
        ).fetchall()
    return {int(r[0]): _row_para_dict(r) for r in rows}


def _executar_acao_no_windows(tipo: str, alvo: str, argumentos: str = "") -> None:
    """Executa a acao aprovada no Windows 11 usando APENAS `os.startfile` ou `subprocess.Popen(..., shell=False)`."""
    if tipo == "abrir_url":
        url = _normalizar_url(alvo)
        nav_exe = (argumentos or "").strip()
        if nav_exe and Path(nav_exe).is_file() and Path(nav_exe).suffix.lower() == ".exe":
            _validar_seguranca_comando(nav_exe)
            subprocess.Popen([nav_exe, url], shell=False, close_fds=True)
        else:
            os.startfile(url)
        return

    if tipo == "abrir_caminho":
        caminho_res = _resolver_caminho_local(alvo)
        os.startfile(str(caminho_res))
        return

    if tipo == "abrir_app":
        _validar_seguranca_comando(alvo, argumentos)
        p_alvo = Path(alvo)
        if not p_alvo.is_file():
            raise FileNotFoundError(f"Executavel ou atalho nao encontrado: {alvo}")
        ext = p_alvo.suffix.lower()
        args_lista = shlex.split(argumentos, posix=False) if (argumentos or "").strip() else []
        if ext == ".lnk" and not args_lista:
            os.startfile(str(p_alvo))
        elif ext == ".exe":
            subprocess.Popen([str(p_alvo), *args_lista], shell=False, close_fds=True)
        elif ext == ".lnk":
            # Atalho .lnk sempre via os.startfile (sem shell)
            os.startfile(str(p_alvo))
        else:
            raise ValueError(f"Tipo de executavel nao suportado: {ext}")
        return

    raise ValueError(f"Tipo de acao desconhecido: {tipo}")


def _inserir_acao(
    *,
    tipo: str,
    chave_whitelist: str,
    alvo: str,
    argumentos: str,
    descricao: str,
    risco: str,
    status: str = "pendente",
    executado_em: str = "",
) -> dict:
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO acoes_windows
                (tipo, chave_whitelist, alvo, argumentos, descricao, risco, status, erro, criado_em, executado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (tipo, chave_whitelist, alvo, argumentos, descricao, risco, status, agora, executado_em),
        )
        acao_id = int(cur.lastrowid or 0)
        row = c.execute(
            """
            SELECT id, tipo, chave_whitelist, alvo, argumentos, descricao, risco, status, erro, criado_em, executado_em
            FROM acoes_windows WHERE id = ?
            """,
            (acao_id,),
        ).fetchone()
    return _row_para_dict(row)


def solicitar_abrir_app_dados(nome_app: str, args: str = "") -> dict:
    """Cria solicitacao pendente (ou executa direto se estiver na whitelist) para abrir um app no Windows."""
    try:
        _validar_seguranca_comando(nome_app, args)
        chave, exib, caminho_alvo, args_extras = _resolver_aplicativo(nome_app)
    except ValueError as e:
        return {"ok": False, "erro": str(e)}

    args_combinados = " ".join([*args_extras, (args or "").strip()]).strip()
    risco = "medio" if (args or "").strip() else "baixo"
    nome_exe = Path(caminho_alvo).name
    descricao = f"Abrir aplicativo {exib} ({nome_exe})"
    if (args or "").strip():
        descricao += f" com argumentos: {(args or '').strip()}"

    # Whitelist ("Sempre permitir"): so executa automaticamente para risco baixo (sem args customizados)
    if risco == "baixo" and esta_na_whitelist(chave):
        agora = datetime.now().isoformat(timespec="seconds")
        try:
            _executar_acao_no_windows("abrir_app", caminho_alvo, args_combinados)
            acao = _inserir_acao(
                tipo="abrir_app",
                chave_whitelist=chave,
                alvo=caminho_alvo,
                argumentos=args_combinados,
                descricao=descricao,
                risco=risco,
                status="executada",
                executado_em=agora,
            )
            acao["auto_executada"] = True
            return {
                "ok": True,
                "auto_executada": True,
                "acao": acao,
                "mensagem": f"{exib} ({nome_exe}) foi aberto automaticamente via permissao pre-aprovada ('Sempre permitir').",
            }
        except Exception as e:
            return {"ok": False, "erro": f"Falha ao abrir {exib} (pre-aprovado): {e}"}

    acao = _inserir_acao(
        tipo="abrir_app",
        chave_whitelist=chave,
        alvo=caminho_alvo,
        argumentos=args_combinados,
        descricao=descricao,
        risco=risco,
        status="pendente",
    )
    acao["auto_executada"] = False
    return {
        "ok": True,
        "auto_executada": False,
        "acao": acao,
        "mensagem": (
            f"Solicitacao #{acao['id']} criada como PENDENTE (o aplicativo ainda NAO foi aberto).\n"
            f"- Acao: {descricao}\n"
            f"- Alvo: {caminho_alvo}\n"
            f"- Risco: {risco}\n"
            f"Aguarde o usuario clicar em 'Permitir', 'Sempre permitir' ou 'Recusar' no card exibido no chat."
        ),
    }


def solicitar_abrir_url_dados(url: str, navegador: str = "brave") -> dict:
    """Cria solicitacao pendente para abrir uma URL no Brave (ou navegador padrao se Brave nao encontrado)."""
    try:
        url_norm = _normalizar_url(url)
    except ValueError as e:
        return {"ok": False, "erro": str(e)}

    nav_escolhido = (navegador or "brave").strip()
    nav_exe = ""
    nav_nome = "navegador padrao"
    if nav_escolhido and _normalizar_ascii(nav_escolhido) not in ("padrao", "default"):
        try:
            _ch, exib, alvo_nav, _ex = _resolver_aplicativo(nav_escolhido)
            if Path(alvo_nav).suffix.lower() == ".exe":
                nav_exe = alvo_nav
                nav_nome = exib
        except ValueError:
            nav_exe = ""
            nav_nome = "navegador padrao"

    host = urlparse(url_norm).netloc
    descricao = f"Abrir {host} no {nav_nome}"
    acao = _inserir_acao(
        tipo="abrir_url",
        chave_whitelist=f"url:{_normalizar_ascii(nav_nome)}",
        alvo=url_norm,
        argumentos=nav_exe,
        descricao=descricao,
        risco="baixo",
        status="pendente",
    )
    acao["auto_executada"] = False
    return {
        "ok": True,
        "auto_executada": False,
        "acao": acao,
        "mensagem": (
            f"Solicitacao #{acao['id']} criada como PENDENTE (a URL ainda NAO foi aberta).\n"
            f"- Acao: {descricao}\n"
            f"- URL: {url_norm}\n"
            f"Aguarde o usuario clicar em 'Permitir' ou 'Recusar' no card exibido no chat."
        ),
    }


def solicitar_abrir_arquivo_ou_pasta_dados(caminho: str) -> dict:
    """Cria solicitacao pendente para abrir um arquivo (ex.: PDF) ou pasta no Windows Explorer."""
    try:
        caminho_res = _resolver_caminho_local(caminho)
    except ValueError as e:
        return {"ok": False, "erro": str(e)}

    eh_pasta = caminho_res.is_dir()
    rotulo = "pasta" if eh_pasta else "arquivo"
    descricao = f"Abrir {rotulo} '{caminho_res.name}' no Windows"
    acao = _inserir_acao(
        tipo="abrir_caminho",
        chave_whitelist="",
        alvo=str(caminho_res),
        argumentos="",
        descricao=descricao,
        risco="baixo",
        status="pendente",
    )
    acao["auto_executada"] = False
    return {
        "ok": True,
        "auto_executada": False,
        "acao": acao,
        "mensagem": (
            f"Solicitacao #{acao['id']} criada como PENDENTE (o {rotulo} ainda NAO foi aberto).\n"
            f"- Acao: {descricao}\n"
            f"- Caminho: {caminho_res}\n"
            f"Aguarde o usuario clicar em 'Permitir' ou 'Recusar' no card exibido no chat."
        ),
    }


def solicitar_abrir_app(nome_app: str, args: str = "") -> str:
    res = solicitar_abrir_app_dados(nome_app, args)
    return res["mensagem"] if res.get("ok") else f"Bloqueado/erro: {res.get('erro')}"


def solicitar_abrir_url(url: str, navegador: str = "brave") -> str:
    res = solicitar_abrir_url_dados(url, navegador)
    return res["mensagem"] if res.get("ok") else f"Bloqueado/erro: {res.get('erro')}"


def solicitar_abrir_arquivo_ou_pasta(caminho: str) -> str:
    res = solicitar_abrir_arquivo_ou_pasta_dados(caminho)
    return res["mensagem"] if res.get("ok") else f"Bloqueado/erro: {res.get('erro')}"


def aprovar_acao_windows(acao_id: int, sempre_permitir: bool = False) -> dict:
    """Aprova e executa uma solicitacao pendente no Windows 11; opcionalmente adiciona o app na whitelist."""
    with _conn() as c:
        row = c.execute(
            """
            SELECT id, tipo, chave_whitelist, alvo, argumentos, descricao, risco, status, erro, criado_em, executado_em
            FROM acoes_windows WHERE id = ?
            """,
            (acao_id,),
        ).fetchone()

    if not row:
        return {"ok": False, "id": acao_id, "erro": f"Solicitacao #{acao_id} nao encontrada."}

    acao = _row_para_dict(row)
    if acao["status"] == "executada":
        return {"ok": True, "acao": acao, "mensagem": "Acao ja havia sido executada."}
    if acao["status"] != "pendente":
        return {"ok": False, "id": acao_id, "erro": f"Solicitacao #{acao_id} nao esta pendente (status: {acao['status']})."}

    try:
        _executar_acao_no_windows(acao["tipo"], acao["alvo"], acao["argumentos"])
    except Exception as e:
        with _conn() as c:
            c.execute("UPDATE acoes_windows SET erro = ? WHERE id = ?", (str(e), acao_id))
        return {"ok": False, "id": acao_id, "erro": f"Falha ao executar no Windows: {e}"}

    agora = datetime.now().isoformat(timespec="seconds")
    whitelist_add = False
    if sempre_permitir and acao["tipo"] == "abrir_app" and acao["risco"] == "baixo" and acao["chave_whitelist"]:
        adicionar_whitelist(acao["chave_whitelist"])
        whitelist_add = True

    with _conn() as c:
        c.execute(
            "UPDATE acoes_windows SET status = 'executada', erro = '', executado_em = ? WHERE id = ?",
            (agora, acao_id),
        )
        row_att = c.execute(
            """
            SELECT id, tipo, chave_whitelist, alvo, argumentos, descricao, risco, status, erro, criado_em, executado_em
            FROM acoes_windows WHERE id = ?
            """,
            (acao_id,),
        ).fetchone()

    acao_att = _row_para_dict(row_att)
    msg = f"Executado: {acao_att['descricao']}"
    if whitelist_add:
        msg += " (adicionado a 'Sempre permitir')."
    return {
        "ok": True,
        "acao": acao_att,
        "sempre_permitir": whitelist_add,
        "mensagem": msg,
    }


def recusar_acao_windows(acao_id: int) -> dict:
    """Marca uma solicitacao pendente como recusada pelo usuario."""
    with _conn() as c:
        row = c.execute(
            """
            SELECT id, tipo, chave_whitelist, alvo, argumentos, descricao, risco, status, erro, criado_em, executado_em
            FROM acoes_windows WHERE id = ?
            """,
            (acao_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "id": acao_id, "erro": f"Solicitacao #{acao_id} nao encontrada."}
        acao = _row_para_dict(row)
        if acao["status"] != "pendente":
            return {"ok": False, "id": acao_id, "erro": f"Solicitacao #{acao_id} nao esta pendente (status: {acao['status']})."}
        c.execute("UPDATE acoes_windows SET status = 'recusada' WHERE id = ?", (acao_id,))
        acao["status"] = "recusada"

    return {
        "ok": True,
        "acao": acao,
        "mensagem": f"Solicitacao #{acao_id} recusada.",
    }
