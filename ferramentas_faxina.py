import hashlib
import os
import shutil
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None

from send2trash import send2trash

DB_PATH = Path(__file__).resolve().parent / "memoria.db"
HOME = Path.home().resolve()

ITENS_PROIBIDOS: frozenset[str] = frozenset({
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "appdata",
    "desktop.ini",
    "thumbs.db",
    ".git",
    ".venv",
    "node_modules",
    ".env",
    "memoria.db",
    "memoria.db-wal",
    "memoria.db-shm",
    "ecossistema-agentes",
    "agente-faculdade",
})

ATALHOS_MANTER_NA_RAIZ_PADRAO: frozenset[str] = frozenset({
    "ecossistema agentes.lnk",
    "ecossistema-agentes.lnk",
    "athena.lnk",
})

PASTAS_ORGANIZACAO_PADRAO: frozenset[str] = frozenset({
    "imagens",
    "pdfs",
    "documentos",
    "planilhas",
    "compactados",
    "instaladores",
    "midia",
    "codigo",
    "outros",
    "atalhos",
    "pastas organizadas",
})

_PALAVRAS_ATALHO_JOGOS: tuple[str, ...] = (
    "steam", "epic", "riot", "valorant", "league", "minecraft", "roblox", "xbox",
    "ubisoft", "battle.net", "gog", "origin", "rockstar", "game", "jogo", "games",
    "fortnite", "genshin", "cs2", "gta", "curseforge", "playnite", "ea app",
)
_PALAVRAS_ATALHO_ESTUDOS: tuple[str, ...] = (
    "ulife", "obsidian", "anki", "notion", "faculdade", "word", "excel",
    "powerpoint", "onenote", "moodle", "classroom", "teams", "zoom", "canvas",
    "zotero", "acrobat", "estudo", "estudos", "pdf",
)


def _tem_arquivos_visiveis(pasta: Path) -> bool:
    try:
        for item in pasta.iterdir():
            if item.name.lower() in ("desktop.ini", "thumbs.db"):
                continue
            return True
    except OSError:
        return False
    return False


def _candidatos_desktop() -> list[Path]:
    return [
        HOME / "OneDrive" / "Desktop",
        HOME / "OneDrive" / "Área de Trabalho",
        HOME / "Desktop",
        HOME / "Área de Trabalho",
    ]


def obter_pasta_desktop() -> Path:
    """Detecta o caminho REAL da Area de Trabalho no Windows via winreg (User Shell Folders -> Desktop),
    expandindo variaveis de ambiente com `os.path.expandvars`, com fallback para OneDrive/local.
    """
    if sys.platform == "win32" and winreg is not None:
        try:
            chave = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, chave) as key:
                valor_bruto, _ = winreg.QueryValueEx(key, "Desktop")
            if isinstance(valor_bruto, str) and valor_bruto.strip():
                expandido = os.path.expandvars(valor_bruto.strip())
                p_reg = Path(expandido).expanduser()
                if p_reg.exists() and p_reg.is_dir():
                    return p_reg.resolve()
        except OSError:
            pass

    existentes: list[Path] = []
    for cand in _candidatos_desktop():
        try:
            if cand.exists() and cand.is_dir():
                res = cand.resolve()
                if res not in existentes:
                    existentes.append(res)
                if _tem_arquivos_visiveis(res):
                    return res
        except OSError:
            continue
    if existentes:
        return existentes[0]
    return (HOME / "Desktop").resolve()


PASTAS_PERMITIDAS: dict[str, Path] = {
    "downloads": (HOME / "Downloads").resolve(),
    "desktop": obter_pasta_desktop(),
    "documents": (HOME / "Documents").resolve(),
    "pictures": (HOME / "Pictures").resolve(),
    "videos": (HOME / "Videos").resolve(),
}

PASTA_EXTRA_AUTORIZADA: Path | None = None

ALIASES_PASTAS: dict[str, str] = {
    "downloads": "downloads",
    "desktop": "desktop",
    "area de trabalho": "desktop",
    "área de trabalho": "desktop",
    "documents": "documents",
    "documentos": "documents",
    "pictures": "pictures",
    "imagens": "pictures",
    "fotos": "pictures",
    "videos": "videos",
    "vídeos": "videos",
}

EXTENSOES_INSTALADORES: frozenset[str] = frozenset({".exe", ".msi", ".iso", ".zip"})

EXTENSOES_ATALHOS: frozenset[str] = frozenset({".lnk", ".url"})

CATEGORIAS_EXTENSOES: dict[str, frozenset[str]] = {
    "Imagens": frozenset({
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg", ".ico", ".heic", ".tiff", ".tif", ".psd",
    }),
    "PDFs": frozenset({".pdf"}),
    "Documentos": frozenset({
        ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".ppt", ".pptx", ".odp", ".epub",
    }),
    "Planilhas": frozenset({".xls", ".xlsx", ".xlsm", ".csv", ".ods", ".tsv"}),
    "Compactados": frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}),
    "Instaladores": frozenset({".exe", ".msi", ".msix", ".iso", ".dmg", ".deb", ".apk", ".bat", ".cmd"}),
    "Midia": frozenset({
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
    }),
    "Codigo": frozenset({
        ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss", ".json", ".yaml", ".yml",
        ".xml", ".sql", ".sh", ".ps1", ".c", ".cpp", ".h", ".java", ".go", ".rs", ".php", ".rb",
    }),
}


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS acoes_faxina (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL,
                origem TEXT NOT NULL,
                destino TEXT DEFAULT '',
                motivo TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendente',
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


def _pastas_autorizadas() -> list[Path]:
    bases = list(PASTAS_PERMITIDAS.values())
    desktop_atual = obter_pasta_desktop()
    if desktop_atual not in bases:
        bases.append(desktop_atual)
    for cand in _candidatos_desktop():
        try:
            if cand.exists() and cand.is_dir():
                res = cand.resolve()
                if res not in bases:
                    bases.append(res)
        except OSError:
            continue
    if PASTA_EXTRA_AUTORIZADA is not None and PASTA_EXTRA_AUTORIZADA not in bases:
        bases.append(PASTA_EXTRA_AUTORIZADA)
    return bases


def definir_pasta_extra_autorizada(caminho: str | Path | None) -> Path | None:
    """Autoriza uma pasta extra escolhida pelo usuario (CONFIG['pasta_raiz']), mantendo bloqueios de seguranca."""
    global PASTA_EXTRA_AUTORIZADA
    if caminho is None or not str(caminho).strip():
        PASTA_EXTRA_AUTORIZADA = None
        return None

    p_raw = Path(str(caminho).strip()).expanduser()
    p_res = p_raw.resolve()
    drive_sistema = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\").resolve()

    if p_res == drive_sistema:
        raise ValueError(f"Raiz do disco do sistema nao pode ser autorizada: {p_res}")
    if _tem_segmento_proibido(p_raw) or _tem_segmento_proibido(p_res):
        raise ValueError(f"Pasta bloqueada por regra de seguranca: {p_res}")
    if not p_res.exists() or not p_res.is_dir():
        raise ValueError(f"Pasta invalida ou inexistente: {p_res}")

    PASTA_EXTRA_AUTORIZADA = p_res
    return p_res


def _tem_segmento_proibido(caminho: Path) -> bool:
    return any(parte.lower() in ITENS_PROIBIDOS for parte in caminho.parts)


def _esta_em_pasta_permitida(caminho_resolvido: Path) -> bool:
    return any(
        caminho_resolvido == base or base in caminho_resolvido.parents
        for base in _pastas_autorizadas()
    )


def _eh_pasta_raiz_permitida(caminho_resolvido: Path) -> bool:
    return any(caminho_resolvido == base for base in _pastas_autorizadas())


def _expandir_alias_caminho(caminho_bruto: str | Path) -> Path:
    """Resolve prefixos amigaveis como 'Desktop', 'Desktop\\Atalhos\\App.lnk' ou 'Downloads\\arq.pdf' para o caminho real."""
    if isinstance(caminho_bruto, Path):
        texto = str(caminho_bruto).strip()
    else:
        texto = str(caminho_bruto or "").strip()
    if not texto:
        return Path("")

    # Se ja for caminho absoluto (ex.: C:\Users\...), mantem como esta
    p_teste = Path(texto).expanduser()
    if p_teste.is_absolute():
        return p_teste

    chave = texto.replace("\\", "/").strip("/")
    partes = chave.split("/", 1)
    alias = ALIASES_PASTAS.get(partes[0].lower())
    if alias:
        base = obter_pasta_desktop() if alias == "desktop" else PASTAS_PERMITIDAS[alias]
        return (base / partes[1]) if len(partes) > 1 and partes[1] else base

    if PASTA_EXTRA_AUTORIZADA is not None and partes[0].lower() in (
        "pasta_raiz",
        "pasta_escolhida",
        "workspace",
        PASTA_EXTRA_AUTORIZADA.name.lower(),
    ):
        base = PASTA_EXTRA_AUTORIZADA
        return (base / partes[1]) if len(partes) > 1 and partes[1] else base

    return p_teste


def _validar_caminho_seguro(
    caminho_bruto: str | Path,
    *,
    exigir_existente: bool = True,
    permitir_raiz: bool = False,
) -> Path:
    texto = str(caminho_bruto or "").strip()
    if not texto:
        raise ValueError("Caminho vazio.")

    p_raw = _expandir_alias_caminho(texto)
    if _tem_segmento_proibido(p_raw):
        raise ValueError(f"Acesso bloqueado por regra de seguranca (item proibido): {texto}")

    p_res = p_raw.resolve()
    if _tem_segmento_proibido(p_res):
        raise ValueError(f"Acesso bloqueado por regra de seguranca (item proibido): {p_res}")

    if not _esta_em_pasta_permitida(p_res):
        permitidas = ", ".join(str(v) for v in _pastas_autorizadas())
        raise ValueError(
            f"Acesso negado fora das pastas permitidas ({permitidas}): {p_res}"
        )

    if not permitir_raiz and _eh_pasta_raiz_permitida(p_res):
        raise ValueError(f"Operacao bloqueada na pasta raiz permitida: {p_res}")

    if exigir_existente and not p_res.exists():
        raise ValueError(f"Caminho nao encontrado: {p_res}")

    return p_res


def _resolver_pasta_varredura(nome_pasta: str) -> Path:
    bruto = (nome_pasta or "").strip()
    if not bruto:
        raise ValueError(
            "Informe uma pasta permitida: Downloads, Desktop, Documents, Pictures, Videos ou a pasta escolhida no painel."
        )

    alvo = _expandir_alias_caminho(bruto)
    pasta = _validar_caminho_seguro(alvo, exigir_existente=True, permitir_raiz=True)
    if not pasta.is_dir():
        raise ValueError(f"O caminho informado nao e uma pasta: {pasta}")
    return pasta


def _contem_item_proibido(pasta: Path) -> bool:
    for root, dirs, files in os.walk(pasta, followlinks=False):
        root_path = Path(root)
        if _tem_segmento_proibido(root_path):
            return True
        for d in dirs:
            if d.lower() in ITENS_PROIBIDOS:
                return True
        for f in files:
            if f.lower() in ITENS_PROIBIDOS:
                return True
    return False


def _iterar_arquivos_seguros(pasta: Path):
    for root, dirs, files in os.walk(pasta, followlinks=False):
        root_path = Path(root)
        if _tem_segmento_proibido(root_path):
            dirs[:] = []
            continue

        dirs_seguros = []
        for d in dirs:
            if d.lower() in ITENS_PROIBIDOS:
                continue
            cand = root_path / d
            if cand.is_symlink():
                continue
            try:
                cand_res = cand.resolve()
            except OSError:
                continue
            if _tem_segmento_proibido(cand_res) or not _esta_em_pasta_permitida(cand_res):
                continue
            dirs_seguros.append(d)
        dirs[:] = dirs_seguros

        for f in files:
            if f.lower() in ITENS_PROIBIDOS:
                continue
            arq = root_path / f
            if arq.is_symlink():
                continue
            try:
                arq_res = arq.resolve()
            except OSError:
                continue
            if _tem_segmento_proibido(arq_res) or not _esta_em_pasta_permitida(arq_res):
                continue
            yield arq_res


def _fmt_tamanho(num_bytes: int) -> str:
    if num_bytes >= 1024**3:
        return f"{num_bytes / (1024**3):.2f} GB"
    if num_bytes >= 1024**2:
        return f"{num_bytes / (1024**2):.2f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


def _sha256_arquivo(caminho: Path, bloco: int = 262144) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        while chunk := f.read(bloco):
            h.update(chunk)
    return h.hexdigest()


def listar_itens_pasta(caminho: str = "Desktop", recursivo: bool = True, max_profundidade: int = 3) -> str:
    """Lista arquivos (incluindo atalhos .lnk/.url) e subpastas dentro de qualquer pasta permitida ou subpasta do Desktop (ex.: 'Desktop', 'Desktop\\Atalhos' ou caminho completo).

    Retorna o nome exato e o caminho completo de cada item para uso direto em `propor_acoes_em_lote`.
    """
    try:
        pasta = _resolver_pasta_varredura(caminho)
    except ValueError as e:
        return f"Erro de seguranca/validacao: {e}"

    profundidade_max = max(0, int(max_profundidade)) if recursivo else 0
    subpastas_dir: list[Path] = []
    arquivos_encontrados: list[tuple[str, Path, int]] = []  # (tipo_tag, caminho_resolvido, tamanho)
    limite_itens = 250
    truncado = False

    def _coletar(dir_atual: Path, prof: int) -> None:
        nonlocal truncado
        if len(arquivos_encontrados) >= limite_itens:
            truncado = True
            return
        try:
            entradas = sorted(dir_atual.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return

        for item in entradas:
            if item.name.lower() in ITENS_PROIBIDOS:
                continue
            if item.is_symlink():
                continue
            try:
                res = item.resolve()
            except OSError:
                continue
            if _tem_segmento_proibido(res) or not _esta_em_pasta_permitida(res):
                continue

            if res.is_dir():
                if prof == 0:
                    subpastas_dir.append(res)
                if prof < profundidade_max:
                    _coletar(res, prof + 1)
            elif res.is_file():
                if len(arquivos_encontrados) >= limite_itens:
                    truncado = True
                    return
                try:
                    tam = res.stat().st_size
                except OSError:
                    tam = 0
                ext = res.suffix.lower()
                tag = "ATALHO" if ext in EXTENSOES_ATALHOS else "ARQUIVO"
                arquivos_encontrados.append((tag, res, tam))

    _coletar(pasta, 0)

    qtd_atalhos = sum(1 for tag, _, _ in arquivos_encontrados if tag == "ATALHO")
    linhas = [
        f"=== LISTAGEM REAL DA PASTA: {pasta} ===",
        f"Subpastas diretas: {len(subpastas_dir)} | Arquivos encontrados (recursivo={recursivo}): {len(arquivos_encontrados)} (sendo {qtd_atalhos} atalho(s) .lnk/.url)",
    ]

    if subpastas_dir:
        linhas.append("\nSubpastas:")
        for sp in subpastas_dir:
            linhas.append(f"- [PASTA] {sp.name} | {sp}")

    if not arquivos_encontrados:
        linhas.append("\nNenhum arquivo ou atalho encontrado nesta pasta.")
    else:
        linhas.append("\nArquivos e atalhos (nome exato | caminho completo):")
        for tag, arq, tam in arquivos_encontrados:
            try:
                rel = arq.relative_to(pasta)
            except ValueError:
                rel = arq.name
            linhas.append(f"- [{tag}] {arq.name} (rel: {rel}) | {_fmt_tamanho(tam)} | {arq}")

    if truncado:
        linhas.append(f"... listagem limitada aos primeiros {limite_itens} arquivos.")

    if _eh_pasta_desktop(pasta):
        icones_pub = _listar_icones_desktop_publico()
        if icones_pub:
            amostra = ", ".join(icones_pub[:10])
            mais = f" (+{len(icones_pub) - 10})" if len(icones_pub) > 10 else ""
            linhas.append(
                f"\nAviso (Desktop Publico - somente leitura): {len(icones_pub)} icone(s) em C:\\Users\\Public\\Desktop ({amostra}{mais})."
            )

    return "\n".join(linhas)


def escanear_pasta(nome_pasta: str, dias_sem_uso: int = 60, min_mb: float = 10) -> str:
    """Escaneia uma pasta permitida e lista arquivos grandes, antigos ou instaladores (.exe, .msi, .iso, .zip)."""
    try:
        pasta = _resolver_pasta_varredura(nome_pasta)
    except ValueError as e:
        return f"Erro de seguranca/validacao: {e}"

    agora_ts = datetime.now().timestamp()
    min_bytes = max(0.0, float(min_mb)) * 1024 * 1024
    dias_limite = max(0, int(dias_sem_uso))

    total_varridos = 0
    candidatos: list[tuple[int, int, str, Path, list[str]]] = []

    for arq in _iterar_arquivos_seguros(pasta):
        try:
            st = arq.stat()
        except OSError:
            continue
        total_varridos += 1
        tamanho = st.st_size
        ultimo_acesso_ts = st.st_atime
        dias_inativo = max(0, int((agora_ts - ultimo_acesso_ts) // 86400))
        ext = arq.suffix.lower()

        tags: list[str] = []
        if tamanho >= min_bytes:
            tags.append(f"GRANDE>={min_mb:g}MB")
        if dias_inativo >= dias_limite:
            tags.append(f"ANTIGO({dias_inativo}d)")
        if ext in EXTENSOES_INSTALADORES:
            tags.append(f"INSTALADOR({ext})")

        if tags:
            data_acesso = datetime.fromtimestamp(ultimo_acesso_ts).strftime("%Y-%m-%d")
            candidatos.append((tamanho, dias_inativo, data_acesso, arq, tags))

    candidatos.sort(key=lambda item: (-item[0], -item[1]))
    espaco_total = sum(item[0] for item in candidatos)

    linhas = [
        f"=== VARREDURA SEGURA: {pasta} ===",
        f"Arquivos analisados: {total_varridos} | Candidatos encontrados: {len(candidatos)} | Soma dos candidatos: {_fmt_tamanho(espaco_total)}",
        f"Criterios: tamanho >= {min_mb:g} MB, sem acesso ha >= {dias_limite} dias ou extensao em {sorted(EXTENSOES_INSTALADORES)}",
    ]

    if not candidatos:
        linhas.append("Nenhum arquivo grande, antigo ou instalador encontrado nessa pasta.")
        return "\n".join(linhas)

    limite_exibicao = 40
    for tamanho, _dias, data_acesso, arq, tags in candidatos[:limite_exibicao]:
        linhas.append(
            f"- [{', '.join(tags)}] {_fmt_tamanho(tamanho)} | ultimo acesso: {data_acesso} | {arq}"
        )

    if len(candidatos) > limite_exibicao:
        linhas.append(f"... e mais {len(candidatos) - limite_exibicao} arquivo(s) omitido(s).")

    return "\n".join(linhas)


def encontrar_duplicados(nome_pasta: str, max_mb: float = 200.0) -> str:
    """Encontra arquivos identicos via hash SHA-256 em uma pasta permitida e calcula espaco desperdicado.

    Otimizacao: so calcula SHA-256 para arquivos com exatamente o mesmo tamanho em bytes
    e ignora arquivos maiores que `max_mb` (padrao: 200 MB) para resposta instantanea.
    """
    try:
        pasta = _resolver_pasta_varredura(nome_pasta)
    except ValueError as e:
        return f"Erro de seguranca/validacao: {e}"

    max_bytes = int(max(0.0, float(max_mb)) * 1024 * 1024)
    por_tamanho: dict[int, list[Path]] = {}
    total_varridos = 0
    ignorados_grandes = 0
    for arq in _iterar_arquivos_seguros(pasta):
        try:
            tamanho = arq.stat().st_size
        except OSError:
            continue
        total_varridos += 1
        if tamanho == 0:
            continue
        if max_bytes > 0 and tamanho > max_bytes:
            ignorados_grandes += 1
            continue
        por_tamanho.setdefault(tamanho, []).append(arq)

    por_hash: dict[str, tuple[int, list[Path]]] = {}
    candidatos_hash = 0
    for tamanho, lista in por_tamanho.items():
        if len(lista) < 2:
            continue
        for arq in lista:
            candidatos_hash += 1
            try:
                digest = _sha256_arquivo(arq)
            except OSError:
                continue
            if digest not in por_hash:
                por_hash[digest] = (tamanho, [arq])
            else:
                por_hash[digest][1].append(arq)

    grupos = [
        (tamanho * (len(arqs) - 1), tamanho, digest, sorted(arqs))
        for digest, (tamanho, arqs) in por_hash.items()
        if len(arqs) >= 2
    ]
    grupos.sort(key=lambda g: -g[0])

    desperdicio_total = sum(g[0] for g in grupos)
    info_grandes = f" | Ignorados (>{max_mb:g} MB): {ignorados_grandes}" if ignorados_grandes else ""
    linhas = [
        f"=== DUPLICADOS (SHA-256): {pasta} ===",
        f"Arquivos analisados: {total_varridos} | Hasheados (mesmo tamanho): {candidatos_hash}{info_grandes} | Grupos duplicados: {len(grupos)} | Espaco desperdicado: {_fmt_tamanho(desperdicio_total)}",
    ]

    if not grupos:
        linhas.append("Nenhum arquivo duplicado encontrado nessa pasta.")
        return "\n".join(linhas)

    limite_grupos = 25
    for desperdicio, tamanho, digest, arqs in grupos[:limite_grupos]:
        linhas.append(
            f"\nGrupo SHA-256 {digest[:12]}... ({len(arqs)} copias x {_fmt_tamanho(tamanho)} | desperdicio: {_fmt_tamanho(desperdicio)}):"
        )
        for arq in arqs:
            linhas.append(f"  - {arq}")

    if len(grupos) > limite_grupos:
        linhas.append(f"\n... e mais {len(grupos) - limite_grupos} grupo(s) omitido(s).")

    return "\n".join(linhas)


def _validar_proposta_mover(origem: str, destino: str, motivo: str) -> tuple[str, str, str, str]:
    origem_res = _validar_caminho_seguro(origem, exigir_existente=True, permitir_raiz=False)
    if origem_res.is_dir() and _contem_item_proibido(origem_res):
        raise ValueError(f"A pasta de origem contem itens proibidos: {origem_res}")

    destino_res = _validar_caminho_seguro(destino, exigir_existente=False, permitir_raiz=True)
    if destino_res.exists() and destino_res.is_dir() or _eh_pasta_raiz_permitida(destino_res):
        destino_final = _validar_caminho_seguro(
            destino_res / origem_res.name, exigir_existente=False, permitir_raiz=False
        )
    else:
        destino_final = destino_res

    if origem_res == destino_final:
        raise ValueError("Origem e destino sao identicos.")

    motivo_limpo = (motivo or "Organizacao de arquivos").strip()
    return ("mover", str(origem_res), str(destino_final), motivo_limpo)


def _validar_proposta_lixeira(origem: str, motivo: str) -> tuple[str, str, str, str]:
    origem_res = _validar_caminho_seguro(origem, exigir_existente=True, permitir_raiz=False)
    if origem_res.is_dir() and _contem_item_proibido(origem_res):
        raise ValueError(f"A pasta contem itens proibidos e nao pode ir para a lixeira: {origem_res}")
    motivo_limpo = (motivo or "Limpeza segura").strip()
    return ("lixeira", str(origem_res), "", motivo_limpo)


def propor_mover_arquivo(origem: str, destino: str, motivo: str) -> str:
    """Registra uma proposta de movimentacao como 'pendente' em acoes_faxina (NAO executa ainda)."""
    try:
        tipo, origem_str, destino_str, motivo_limpo = _validar_proposta_mover(origem, destino, motivo)
    except ValueError as e:
        return f"Proposta bloqueada por seguranca: {e}"

    agora = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO acoes_faxina (tipo, origem, destino, motivo, status, criado_em)
            VALUES (?, ?, ?, ?, 'pendente', ?)
            """,
            (tipo, origem_str, destino_str, motivo_limpo, agora),
        )
        acao_id = cur.lastrowid

    return (
        f"Proposta #{acao_id} registrada como PENDENTE (nenhum arquivo foi movido ainda).\n"
        f"- Tipo: MOVER\n"
        f"- Origem: {origem_str}\n"
        f"- Destino: {destino_str}\n"
        f"- Motivo: {motivo_limpo}\n"
        f"Aprove ou recuse na aba 'Faxina & Aprovacoes' do painel."
    )


def propor_enviar_lixeira(origem: str, motivo: str) -> str:
    """Registra uma proposta de envio para a Lixeira como 'pendente' em acoes_faxina (NAO executa ainda)."""
    try:
        tipo, origem_str, destino_str, motivo_limpo = _validar_proposta_lixeira(origem, motivo)
    except ValueError as e:
        return f"Proposta bloqueada por seguranca: {e}"

    agora = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO acoes_faxina (tipo, origem, destino, motivo, status, criado_em)
            VALUES (?, ?, ?, ?, 'pendente', ?)
            """,
            (tipo, origem_str, destino_str, motivo_limpo, agora),
        )
        acao_id = cur.lastrowid

    return (
        f"Proposta #{acao_id} registrada como PENDENTE (nenhum arquivo foi enviado para a lixeira ainda).\n"
        f"- Tipo: LIXEIRA\n"
        f"- Origem: {origem_str}\n"
        f"- Motivo: {motivo_limpo}\n"
        f"Aprove ou recuse na aba 'Faxina & Aprovacoes' do painel."
    )


def propor_acoes_em_lote(acoes: list[dict]) -> str:
    """Valida todas as regras de seguranca e registra multiplas propostas ('mover' ou 'lixeira') no SQLite em uma unica transacao."""
    if not isinstance(acoes, list) or not acoes:
        return "Nenhuma acao fornecida para o lote."

    validas: list[tuple[str, str, str, str]] = []
    bloqueadas: list[str] = []
    chaves_vistas: set[tuple[str, str, str]] = set()

    for idx, item in enumerate(acoes, start=1):
        if not isinstance(item, dict):
            bloqueadas.append(f"Item {idx}: formato invalido (esperado dict).")
            continue

        tipo_raw = str(item.get("tipo") or "").strip().lower()
        origem = str(item.get("origem") or "").strip()
        destino = str(item.get("destino") or "").strip()
        motivo = str(item.get("motivo") or "").strip()

        try:
            if tipo_raw == "mover":
                tupla = _validar_proposta_mover(origem, destino, motivo)
            elif tipo_raw == "lixeira":
                tupla = _validar_proposta_lixeira(origem, motivo)
            else:
                raise ValueError(f"Tipo invalido '{tipo_raw}' (use 'mover' ou 'lixeira').")
        except ValueError as e:
            bloqueadas.append(f"Item {idx} ({origem or 'sem origem'}): {e}")
            continue

        chave_dedup = (tupla[0], tupla[1].lower(), tupla[2].lower())
        if chave_dedup in chaves_vistas:
            continue
        chaves_vistas.add(chave_dedup)
        validas.append(tupla)

    ids_inseridos: list[int] = []
    if validas:
        agora = datetime.now().isoformat(timespec="seconds")
        with _conn() as c:
            for tipo, origem_str, destino_str, motivo_limpo in validas:
                cur = c.execute(
                    """
                    INSERT INTO acoes_faxina (tipo, origem, destino, motivo, status, criado_em)
                    VALUES (?, ?, ?, ?, 'pendente', ?)
                    """,
                    (tipo, origem_str, destino_str, motivo_limpo, agora),
                )
                if cur.lastrowid is not None:
                    ids_inseridos.append(int(cur.lastrowid))

    linhas = [
        "=== LOTE DE PROPOSTAS REGISTRADO (PENDENTE) ===",
        f"Registradas em 1 transacao: {len(ids_inseridos)} | Bloqueadas/invalidas: {len(bloqueadas)}",
    ]
    if ids_inseridos:
        resumo_ids = (
            f"#{ids_inseridos[0]} a #{ids_inseridos[-1]}"
            if len(ids_inseridos) > 1
            else f"#{ids_inseridos[0]}"
        )
        linhas.append(f"IDs criados: {resumo_ids} (nenhum arquivo foi alterado ainda).")
        for acao_id, (tipo, origem_str, destino_str, motivo_limpo) in zip(ids_inseridos[:50], validas[:50], strict=False):
            if tipo == "mover":
                linhas.append(f"- #{acao_id} [MOVER] {origem_str} -> {destino_str} ({motivo_limpo})")
            else:
                linhas.append(f"- #{acao_id} [LIXEIRA] {origem_str} ({motivo_limpo})")
        if len(ids_inseridos) > 50:
            linhas.append(f"... e mais {len(ids_inseridos) - 50} proposta(s) registrada(s).")
        linhas.append("Aprove ou recuse na aba 'Faxina & Aprovacoes' do painel.")

    if bloqueadas:
        linhas.append("\nItens bloqueados por seguranca/validacao:")
        for erro in bloqueadas[:15]:
            linhas.append(f"- {erro}")
        if len(bloqueadas) > 15:
            linhas.append(f"... e mais {len(bloqueadas) - 15} erro(s).")

    return "\n".join(linhas)


def _norm_nome_simples(texto: str) -> str:
    return (
        unicodedata.normalize("NFKD", (texto or "").strip())
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def _classificar_atalho(caminho: Path) -> str:
    nome_norm = _norm_nome_simples(caminho.stem)
    if any(k in nome_norm for k in _PALAVRAS_ATALHO_JOGOS):
        return "Atalhos/Jogos"
    if any(k in nome_norm for k in _PALAVRAS_ATALHO_ESTUDOS):
        return "Atalhos/Estudos"
    return "Atalhos/Apps"


def _classificar_categoria_arquivo(caminho: Path, incluir_atalhos: bool = True) -> str | None:
    ext = caminho.suffix.lower()
    if ext in EXTENSOES_ATALHOS:
        return _classificar_atalho(caminho) if incluir_atalhos else None
    for categoria, extensoes in CATEGORIAS_EXTENSOES.items():
        if ext in extensoes:
            return categoria
    return "Outros"


def _listar_icones_desktop_publico() -> list[str]:
    """Verifica C:\\Users\\Public\\Desktop em modo somente leitura para avisar sobre icones que exigem Admin."""
    pasta_pub = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"
    if not pasta_pub.exists() or not pasta_pub.is_dir():
        return []
    encontrados: list[str] = []
    try:
        for item in sorted(pasta_pub.iterdir(), key=lambda p: p.name.lower()):
            if item.name.lower() in ("desktop.ini", "thumbs.db"):
                continue
            encontrados.append(item.name)
    except OSError:
        return []
    return encontrados


def _eh_pasta_desktop(pasta: Path) -> bool:
    alvos = {obter_pasta_desktop()}
    for c in _candidatos_desktop():
        try:
            if c.exists():
                alvos.add(c.resolve())
        except OSError:
            pass
    return pasta in alvos


def planejar_organizacao_pasta(
    nome_pasta: str = "Desktop",
    incluir_atalhos: bool = True,
    incluir_pastas: bool = True,
    manter_na_raiz: list[str] | None = None,
) -> str:
    """Analisa arquivos soltos, atalhos (.lnk/.url) e pastas soltas fora do padrao, registrando propostas PENDENTES em lote.

    - Mantem `Ecossistema Agentes.lnk` (e itens pedidos em `manter_na_raiz`) na raiz.
    - Quando a pasta for o Desktop, tambem verifica `C:\\Users\\Public\\Desktop` (somente leitura) e avisa quantos icones publicos existem.
    """
    try:
        pasta = _resolver_pasta_varredura(nome_pasta)
    except ValueError as e:
        return f"Erro de seguranca/validacao: {e}"

    manter_set = set(ATALHOS_MANTER_NA_RAIZ_PADRAO)
    for item_m in manter_na_raiz or []:
        m = str(item_m or "").strip().lower()
        if m:
            manter_set.add(m)
            if not m.endswith((".lnk", ".url")):
                manter_set.add(f"{m}.lnk")
                manter_set.add(f"{m}.url")
    manter_stems = {_norm_nome_simples(Path(x).stem) for x in manter_set}

    with _conn() as c:
        pendentes_existentes = {
            str(r[0]).lower()
            for r in c.execute(
                "SELECT origem FROM acoes_faxina WHERE status = 'pendente' AND tipo = 'mover'"
            ).fetchall()
        }

    por_categoria: dict[str, list[tuple[str, str, str, str]]] = {}
    atalhos_mantidos: list[str] = []
    ja_pendentes = 0
    ignorados_seguranca = 0

    try:
        itens = sorted(pasta.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        return f"Erro ao ler a pasta {pasta}: {e}"

    for item in itens:
        nome_low = item.name.lower()
        if nome_low in ITENS_PROIBIDOS:
            ignorados_seguranca += 1
            continue
        if item.is_symlink():
            continue

        # 1) Pastas soltas fora do padrao -> Pastas Organizadas/ (opcional, sempre com aprovacao humana)
        if item.is_dir():
            if not incluir_pastas:
                continue
            if _norm_nome_simples(item.name) in PASTAS_ORGANIZACAO_PADRAO or item.name.startswith("."):
                continue
            if _contem_item_proibido(item):
                ignorados_seguranca += 1
                continue
            try:
                origem_res = _validar_caminho_seguro(item, exigir_existente=True, permitir_raiz=False)
                if str(origem_res).lower() in pendentes_existentes:
                    ja_pendentes += 1
                    continue
                categoria_dir = "Pastas Organizadas"
                destino_alvo = pasta / categoria_dir / origem_res.name
                tupla = _validar_proposta_mover(
                    str(origem_res),
                    str(destino_alvo),
                    "Organizacao automatica (Pasta solta -> Pastas Organizadas)",
                )
                por_categoria.setdefault(categoria_dir, []).append(tupla)
            except ValueError:
                ignorados_seguranca += 1
            continue

        if not item.is_file():
            continue

        # 2) Atalhos protegidos na raiz (ex.: Ecossistema Agentes.lnk ou pedidos pelo usuario)
        if nome_low in manter_set or _norm_nome_simples(item.stem) in manter_stems:
            atalhos_mantidos.append(item.name)
            continue

        categoria = _classificar_categoria_arquivo(item, incluir_atalhos=incluir_atalhos)
        if categoria is None:
            atalhos_mantidos.append(item.name)
            continue

        try:
            origem_res = _validar_caminho_seguro(item, exigir_existente=True, permitir_raiz=False)
            if str(origem_res).lower() in pendentes_existentes:
                ja_pendentes += 1
                continue
            destino_alvo = pasta / Path(categoria) / origem_res.name
            tupla = _validar_proposta_mover(
                str(origem_res),
                str(destino_alvo),
                f"Organizacao automatica ({categoria})",
            )
            por_categoria.setdefault(categoria, []).append(tupla)
        except ValueError:
            ignorados_seguranca += 1
            continue

    icones_publicos = _listar_icones_desktop_publico() if _eh_pasta_desktop(pasta) else []
    aviso_publico = ""
    if icones_publicos:
        amostra = ", ".join(icones_publicos[:8])
        mais = f" (+{len(icones_publicos) - 8})" if len(icones_publicos) > 8 else ""
        aviso_publico = (
            f"\nAviso (Desktop Publico): existem {len(icones_publicos)} icone(s) em C:\\Users\\Public\\Desktop "
            f"({amostra}{mais}). Como ficam na pasta Publica do Windows, exigem permissao de Administrador para mover."
        )

    todas_validas: list[tuple[str, str, str, str]] = []
    for cat in sorted(por_categoria.keys()):
        todas_validas.extend(por_categoria[cat])

    if not todas_validas:
        detalhes = []
        if atalhos_mantidos:
            detalhes.append(f"{len(atalhos_mantidos)} atalho(s) mantido(s) na raiz")
        if ja_pendentes:
            detalhes.append(f"{ja_pendentes} item(ns) ja possuem proposta pendente")
        extra = f" ({'; '.join(detalhes)})" if detalhes else ""
        return f"Nenhum item solto novo para organizar em {pasta}{extra}.{aviso_publico}"

    ids_inseridos: list[int] = []
    agora = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        for tipo, origem_str, destino_str, motivo_limpo in todas_validas:
            cur = c.execute(
                """
                INSERT INTO acoes_faxina (tipo, origem, destino, motivo, status, criado_em)
                VALUES (?, ?, ?, ?, 'pendente', ?)
                """,
                (tipo, origem_str, destino_str, motivo_limpo, agora),
            )
            if cur.lastrowid is not None:
                ids_inseridos.append(int(cur.lastrowid))

    nome_curto = pasta.name or str(pasta)
    resumo_ids = (
        f"#{ids_inseridos[0]} a #{ids_inseridos[-1]}"
        if len(ids_inseridos) > 1
        else f"#{ids_inseridos[0]}"
    )
    linhas = [
        f"=== PLANO DE ORGANIZACAO: {pasta} ===",
        f"Propostas registradas como PENDENTES em 1 transacao: {len(ids_inseridos)} (IDs {resumo_ids} — nenhum item foi movido ainda).",
    ]
    if atalhos_mantidos:
        linhas.append(f"Mantidos na raiz: {', '.join(atalhos_mantidos[:10])}")
    if ja_pendentes:
        linhas.append(f"Ja estavam na fila pendente (nao duplicados): {ja_pendentes}")

    linhas.append("\nResumo por categoria:")
    for cat, itens_cat in sorted(por_categoria.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        linhas.append(f"- {len(itens_cat)} {cat} -> {nome_curto}/{cat}")

    if aviso_publico:
        linhas.append(aviso_publico.lstrip("\n"))

    linhas.append(
        "\nAbra a aba 'Faxina & Aprovacoes' para clicar em 'Aprovar Tudo' ou aprovar/recusar individualmente."
    )
    return "\n".join(linhas)


def _garantir_destino_sem_colisao(destino: Path) -> Path:
    """Se ja existir arquivo/pasta no destino, gera sufixo incremental ` (1)`, ` (2)`, ... sem sobrescrever."""
    if not destino.exists():
        return destino
    parent = destino.parent
    stem = destino.stem if destino.suffix else destino.name
    suffix = destino.suffix
    idx = 1
    while True:
        candidato = parent / f"{stem} ({idx}){suffix}"
        if not candidato.exists():
            return _validar_caminho_seguro(candidato, exigir_existente=False, permitir_raiz=False)
        idx += 1


def executar_acao_aprovada(acao_id: int) -> dict:
    """Executa uma acao previamente aprovada pelo usuario na API: mover (criando subpasta e evitando colisao) ou send2trash."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, tipo, origem, destino, motivo, status, criado_em FROM acoes_faxina WHERE id = ?",
            (acao_id,),
        ).fetchone()

    if not row:
        return {"ok": False, "id": acao_id, "erro": f"Acao #{acao_id} nao encontrada."}

    _, tipo, origem, destino, motivo, status, _ = row
    if status != "pendente":
        return {"ok": False, "id": acao_id, "erro": f"Acao #{acao_id} nao esta pendente (status atual: {status})."}

    destino_real_gravar = destino
    try:
        origem_res = _validar_caminho_seguro(origem, exigir_existente=True, permitir_raiz=False)
        if origem_res.is_dir() and _contem_item_proibido(origem_res):
            raise ValueError("Pasta de origem contem itens proibidos.")

        if tipo == "mover":
            destino_res = _validar_caminho_seguro(destino, exigir_existente=False, permitir_raiz=True)
            if destino_res.exists() and destino_res.is_dir() or _eh_pasta_raiz_permitida(destino_res):
                destino_final = _validar_caminho_seguro(
                    destino_res / origem_res.name, exigir_existente=False, permitir_raiz=False
                )
            else:
                destino_final = destino_res

            os.makedirs(str(destino_final.parent), exist_ok=True)
            destino_final = _garantir_destino_sem_colisao(destino_final)
            shutil.move(str(origem_res), str(destino_final))
            destino_real_gravar = str(destino_final)
            mensagem = f"Movido com sucesso para {destino_final}"
        elif tipo == "lixeira":
            send2trash(str(origem_res))
            mensagem = f"Enviado para a Lixeira do Windows: {origem_res}"
        else:
            return {"ok": False, "id": acao_id, "erro": f"Tipo de acao desconhecido: {tipo}"}
    except PermissionError as e:
        return {
            "ok": False,
            "id": acao_id,
            "erro": f"Permissao negada ou arquivo aberto/em uso ({origem}): {e}",
        }
    except OSError as e:
        return {
            "ok": False,
            "id": acao_id,
            "erro": f"Erro do sistema ao mover/apagar ({origem}): {e}",
        }
    except Exception as e:
        return {"ok": False, "id": acao_id, "erro": str(e)}

    with _conn() as c:
        c.execute(
            "UPDATE acoes_faxina SET status = 'executada', destino = ? WHERE id = ?",
            (destino_real_gravar, acao_id),
        )

    try:
        from auditoria import registrar_atividade

        registrar_atividade(
            agente="Agente Faxineiro",
            acao=f"faxina_{tipo}",
            alvo=str(origem_res),
            status="sucesso",
            detalhes=f"{mensagem} | motivo: {motivo}",
        )
    except Exception:
        pass

    return {
        "ok": True,
        "id": acao_id,
        "tipo": tipo,
        "origem": str(origem_res),
        "destino": destino_real_gravar,
        "motivo": motivo,
        "status": "executada",
        "mensagem": mensagem,
    }


def desfazer_movimentacao(acao_id: int) -> dict:
    """Desfaz uma acao 'mover' ja executada, devolvendo o arquivo de destino para origem e marcando como 'desfeita'."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, tipo, origem, destino, motivo, status FROM acoes_faxina WHERE id = ?",
            (acao_id,),
        ).fetchone()

    if not row:
        return {"ok": False, "erro": f"Acao #{acao_id} nao encontrada."}

    _, tipo, origem, destino, motivo, status = row
    if tipo != "mover":
        return {"ok": False, "erro": f"Acao #{acao_id} e do tipo '{tipo}' e nao pode ser desfeita automaticamente."}
    if status != "executada":
        return {"ok": False, "erro": f"Acao #{acao_id} nao esta executada (status atual: {status})."}

    try:
        destino_res = _validar_caminho_seguro(destino, exigir_existente=True, permitir_raiz=False)
        origem_res = _validar_caminho_seguro(origem, exigir_existente=False, permitir_raiz=False)
        origem_res.parent.mkdir(parents=True, exist_ok=True)
        origem_final = _garantir_destino_sem_colisao(origem_res)
        shutil.move(str(destino_res), str(origem_final))
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    with _conn() as c:
        c.execute("UPDATE acoes_faxina SET status = 'desfeita' WHERE id = ?", (acao_id,))

    return {
        "ok": True,
        "id": acao_id,
        "tipo": tipo,
        "origem": str(origem_final),
        "destino": str(destino_res),
        "motivo": motivo,
        "status": "desfeita",
        "mensagem": f"Movimentacao #{acao_id} desfeita: arquivo devolvido para {origem_final}",
    }


def aprovar_todas_pendentes() -> dict:
    """Executa todas as acoes pendentes em acoes_faxina de uma vez e retorna o resumo."""
    with _conn() as c:
        ids = [
            int(r[0])
            for r in c.execute(
                "SELECT id FROM acoes_faxina WHERE status = 'pendente' ORDER BY id ASC"
            ).fetchall()
        ]

    if not ids:
        return {
            "ok": True,
            "total": 0,
            "executadas": 0,
            "erros": 0,
            "detalhes_erros": [],
            "mensagem": "Nenhuma acao pendente para aprovar.",
        }

    executadas = 0
    detalhes_erros: list[dict] = []
    for aid in ids:
        res = executar_acao_aprovada(aid)
        if res.get("ok"):
            executadas += 1
        else:
            detalhes_erros.append({"id": aid, "erro": res.get("erro", "Falha desconhecida")})

    erros = len(detalhes_erros)
    if erros == 0:
        msg = f"{executadas} de {len(ids)} acao(oes) executada(s) com sucesso."
    else:
        amostra_erros = "; ".join(f"#{e['id']} ({e['erro']})" for e in detalhes_erros[:3])
        msg = f"{executadas} executada(s) e {erros} falha(s): {amostra_erros}"
    return {
        "ok": erros == 0,
        "total": len(ids),
        "executadas": executadas,
        "erros": erros,
        "erro": "" if erros == 0 else msg,
        "detalhes_erros": detalhes_erros[:20],
        "mensagem": msg,
    }


def recusar_acao(acao_id: int) -> dict:
    """Marca uma acao pendente como recusada pelo usuario."""
    with _conn() as c:
        row = c.execute("SELECT status FROM acoes_faxina WHERE id = ?", (acao_id,)).fetchone()
        if not row:
            return {"ok": False, "erro": f"Acao #{acao_id} nao encontrada."}
        if row[0] != "pendente":
            return {"ok": False, "erro": f"Acao #{acao_id} nao esta pendente (status atual: {row[0]})."}
        c.execute("UPDATE acoes_faxina SET status = 'recusada' WHERE id = ?", (acao_id,))

    return {"ok": True, "id": acao_id, "status": "recusada", "mensagem": f"Acao #{acao_id} recusada."}


def listar_acoes_faxina(limite_historico: int = 40) -> dict:
    """Retorna acoes pendentes, historico e contadores para o painel web."""
    with _conn() as c:
        pend_rows = c.execute(
            """
            SELECT id, tipo, origem, destino, motivo, status, criado_em
            FROM acoes_faxina
            WHERE status = 'pendente'
            ORDER BY id DESC
            """
        ).fetchall()
        hist_rows = c.execute(
            """
            SELECT id, tipo, origem, destino, motivo, status, criado_em
            FROM acoes_faxina
            WHERE status != 'pendente'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limite_historico,),
        ).fetchall()
        total_executadas = int(
            c.execute("SELECT COUNT(*) FROM acoes_faxina WHERE status = 'executada'").fetchone()[0]
        )
        total_recusadas = int(
            c.execute("SELECT COUNT(*) FROM acoes_faxina WHERE status = 'recusada'").fetchone()[0]
        )

    def _to_dict(r):
        return {
            "id": r[0],
            "tipo": r[1],
            "origem": r[2],
            "destino": r[3],
            "motivo": r[4],
            "status": r[5],
            "criado_em": r[6],
        }

    pendentes = [_to_dict(r) for r in pend_rows]
    historico = [_to_dict(r) for r in hist_rows]
    return {
        "pendentes": pendentes,
        "historico": historico,
        "resumo": {
            "pendentes": len(pendentes),
            "executadas": total_executadas,
            "recusadas": total_recusadas,
        },
    }
