"""Gerenciador nativo de Agent Skills (`skills/*/SKILL.md`) com progressive disclosure.

- Escaneia `skills/*/SKILL.md` usando apenas stdlib (parser YAML frontmatter leve).
- Gera um catalogo ultra enxuto (`- name: description`, 1 linha por skill) para o system prompt.
- Carrega o corpo completo sob demanda via `carregar_skill(nome)` com protecao contra path traversal.
"""

from __future__ import annotations

import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = BASE_DIR / "skills"

_NOME_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_CORPO_SKILL_CHARS = 12000


def _limpar_valor_yaml(valor: str) -> str:
    txt = (valor or "").strip()
    if len(txt) >= 2 and txt[0] == txt[-1] and txt[0] in ("'", '"'):
        txt = txt[1:-1].strip()
    return " ".join(txt.split())


def _parse_frontmatter(conteudo: str) -> tuple[dict[str, str], str]:
    """Extrai frontmatter YAML simples (`chave: valor`) e retorna `(metadados, corpo)`."""
    texto = (conteudo or "").lstrip("\ufeff")
    linhas = texto.splitlines()
    if not linhas or linhas[0].strip() != "---":
        return {}, texto.strip()

    fim_idx: int | None = None
    for idx in range(1, len(linhas)):
        if linhas[idx].strip() == "---":
            fim_idx = idx
            break

    if fim_idx is None:
        return {}, texto.strip()

    meta: dict[str, str] = {}
    chave_atual: str | None = None
    for ln in linhas[1:fim_idx]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln[0] in (" ", "\t") and chave_atual is not None:
            extra = _limpar_valor_yaml(ln)
            if extra:
                anterior = meta.get(chave_atual, "")
                meta[chave_atual] = f"{anterior} {extra}".strip() if anterior else extra
            continue
        if ":" in ln:
            chave, bruto = ln.split(":", 1)
            k = chave.strip().lower()
            v = _limpar_valor_yaml(bruto)
            if k:
                chave_atual = k
                meta[k] = "" if v in ("|", ">", "|-", ">-") else v

    corpo = "\n".join(linhas[fim_idx + 1 :]).strip()
    return meta, corpo


def _eh_arquivo_dentro_de(arquivo: Path, base: Path) -> bool:
    try:
        base_res = base.resolve()
        arq_res = arquivo.resolve()
        return base_res in arq_res.parents and arq_res.is_file()
    except OSError:
        return False


def _validar_nome_skill(nome: str) -> str:
    """Normaliza e valida o nome da skill bloqueando qualquer tentativa de path traversal."""
    bruto = (nome or "").strip()
    if not bruto:
        raise ValueError("Nome de skill vazio.")
    if (
        "/" in bruto
        or "\\" in bruto
        or ".." in bruto
        or ":" in bruto
        or "\x00" in bruto
    ):
        raise ValueError(f"Nome de skill bloqueado por seguranca (path traversal): '{bruto}'.")

    norm = bruto.lower()
    if not _NOME_SKILL_RE.match(norm):
        raise ValueError(
            f"Nome de skill invalido: '{bruto}'. Use apenas letras minusculas, numeros, '-' ou '_'."
        )
    return norm


def _escanear_arquivos_skills(diretorio: Path | None = None) -> list[tuple[dict[str, str], Path]]:
    base = (diretorio or SKILLS_DIR).resolve()
    if not base.is_dir():
        return []

    encontradas: list[tuple[dict[str, str], Path]] = []
    vistos: set[str] = set()
    for pasta in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not pasta.is_dir():
            continue
        if not _NOME_SKILL_RE.match(pasta.name.lower()):
            continue
        arquivo = pasta / "SKILL.md"
        if not _eh_arquivo_dentro_de(arquivo, base):
            continue
        try:
            conteudo = arquivo.read_text(encoding="utf-8")
        except OSError:
            continue

        meta, _ = _parse_frontmatter(conteudo)
        nome_raw = (meta.get("name") or pasta.name).strip().lower()
        if not _NOME_SKILL_RE.match(nome_raw):
            nome_raw = pasta.name.lower()
        if nome_raw in vistos:
            continue
        vistos.add(nome_raw)

        desc = _limpar_valor_yaml(meta.get("description") or "") or f"Skill {nome_raw}."
        encontradas.append(
            (
                {
                    "name": nome_raw,
                    "description": desc,
                    "slug": pasta.name,
                },
                arquivo.resolve(),
            )
        )
    return encontradas


def listar_skills(diretorio: Path | None = None) -> list[dict[str, str]]:
    """Retorna metadados enxutos (`name`, `description`) de todas as skills validas em `skills/*/SKILL.md`."""
    return [
        {"name": item["name"], "description": item["description"]}
        for item, _arq in _escanear_arquivos_skills(diretorio)
    ]


def gerar_catalogo_skills(diretorio: Path | None = None) -> str:
    """Gera o catalogo enxuto (1 linha por skill: `- name: description`) para o system prompt."""
    itens = listar_skills(diretorio)
    if not itens:
        return ""
    return "\n".join(f"- {s['name']}: {s['description']}" for s in itens)


def carregar_skill(nome: str, diretorio: Path | None = None) -> str:
    """Carrega sob demanda o conteudo completo do `SKILL.md` correspondente (com protecao contra path traversal)."""
    try:
        nome_norm = _validar_nome_skill(nome)
    except ValueError as exc:
        return f"Erro ao carregar skill: {exc}"

    base = (diretorio or SKILLS_DIR).resolve()
    if not base.is_dir():
        return f"Skill '{nome_norm}' nao encontrada (pasta de skills indisponivel)."

    arquivo_alvo: Path | None = None
    candidato_direto = base / nome_norm / "SKILL.md"
    if _eh_arquivo_dentro_de(candidato_direto, base):
        arquivo_alvo = candidato_direto.resolve()
    else:
        for item, arq in _escanear_arquivos_skills(base):
            if item["name"] == nome_norm or item["slug"].lower() == nome_norm:
                arquivo_alvo = arq
                break

    if arquivo_alvo is None or not _eh_arquivo_dentro_de(arquivo_alvo, base):
        disponiveis = ", ".join(s["name"] for s in listar_skills(base)) or "nenhuma"
        return f"Skill '{nome_norm}' nao encontrada. Skills disponiveis: {disponiveis}."

    try:
        conteudo = arquivo_alvo.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Erro ao ler skill '{nome_norm}': {exc}"

    meta, corpo = _parse_frontmatter(conteudo)
    titulo = meta.get("name") or nome_norm
    corpo_final = corpo or conteudo.strip()
    if len(corpo_final) > _MAX_CORPO_SKILL_CHARS:
        corpo_final = corpo_final[:_MAX_CORPO_SKILL_CHARS].rstrip() + "\n... (skill truncada)"
    return f"[SKILL CARREGADA: {titulo}]\n\n{corpo_final}"
