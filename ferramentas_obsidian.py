import contextlib
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

VAULT_PADRAO = Path.home() / "Documents" / "Athena-Vault"

SUBPASTAS_PADRAO: tuple[str, ...] = (
    "00-Inbox",
    "01-Faculdade",
    "02-Projetos",
    "03-Financas",
    "04-Diario",
)

PASTAS_OCULTAS_IGNORAR: frozenset[str] = frozenset({".obsidian", ".git", ".trash"})
PERFIL_INICIAL_NOME = "Perfil-William.md"
LIMITE_LEITURA_CHARS = 16000

CONTEUDO_PERFIL_INICIAL = """---
tags:
  - perfil
  - athena
---

# Perfil — William

Este é o **Segundo Cérebro** compartilhado da [[Athena]] no Obsidian.

## Estrutura do Vault
- [[00-Inbox]] — Capturas rápidas e ideias soltas
- [[01-Faculdade]] — Resumos de aulas, provas e planos de estudo
- [[02-Projetos]] — Projetos técnicos, arquitetura e anotações
- [[03-Financas]] — Análises de investimentos, metas e relatórios
- [[04-Diario]] — Notas diárias, logs e revisões

## Contexto & Metas
- **Curso / Faculdade:** UNISUL (portal Ulife)
- **Preferências:** Respostas objetivas em português do Brasil, organização clara em Markdown com `[[wikilinks]]` e `#tags`.
"""


def obter_pasta_vault() -> Path:
    bruto = (os.getenv("OBSIDIAN_VAULT") or "").strip().strip('"').strip("'")
    base = Path(bruto).expanduser() if bruto else VAULT_PADRAO
    return base.resolve()


def inicializar_vault() -> Path:
    """Garante que a pasta do Vault, subpastas padrao e Perfil-William.md existam."""
    vault = obter_pasta_vault()
    vault.mkdir(parents=True, exist_ok=True)
    for sub in SUBPASTAS_PADRAO:
        (vault / sub).mkdir(parents=True, exist_ok=True)

    perfil = vault / PERFIL_INICIAL_NOME
    if not perfil.exists():
        perfil.write_text(CONTEUDO_PERFIL_INICIAL, encoding="utf-8")
    return vault


def _normalizar(txt: str) -> str:
    sem_acento = (
        unicodedata.normalize("NFKD", (txt or "").strip())
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"\s+", " ", sem_acento).strip()


def _tem_segmento_oculto(caminho_rel: Path) -> bool:
    return any(
        parte.lower() in PASTAS_OCULTAS_IGNORAR or parte.startswith(".")
        for parte in caminho_rel.parts
        if parte not in ("", ".")
    )


def _resolver_no_vault(
    caminho_relativo: str,
    *,
    exigir_md: bool = True,
    permitir_raiz: bool = False,
) -> tuple[Path, Path]:
    """Valida e resolve um caminho estritamente dentro do Vault (apenas .md quando exigir_md=True)."""
    vault = inicializar_vault()
    bruto = (caminho_relativo or "").strip().replace("\\", "/")

    if not bruto or bruto in (".", "/"):
        if not permitir_raiz:
            raise ValueError("Informe o caminho relativo de uma nota .md dentro do Vault.")
        return vault, vault

    if re.match(r"^[A-Za-z]:", bruto) or bruto.startswith("/"):
        raise ValueError("Use caminho relativo dentro do Vault (ex.: '01-Faculdade/resumo.md').")

    rel = Path(bruto)
    if ".." in rel.parts:
        raise ValueError("Acesso bloqueado: navegacao fora do Vault ('..') nao e permitida.")
    if _tem_segmento_oculto(rel):
        raise ValueError("Acesso bloqueado a pastas/arquivos ocultos do Vault (ex.: .obsidian, .git).")

    if exigir_md:
        if not rel.suffix:
            rel = rel.with_suffix(".md")
        elif rel.suffix.lower() != ".md":
            raise ValueError(f"Apenas arquivos Markdown (.md) sao permitidos no Vault (recebido: '{rel.suffix}').")

    alvo = (vault / rel).resolve()
    if alvo != vault and vault not in alvo.parents:
        raise ValueError("Acesso negado: caminho fora da pasta do Obsidian Vault.")
    if not permitir_raiz and alvo == vault:
        raise ValueError("Informe o caminho de uma nota .md especifica dentro do Vault.")
    return vault, alvo


def _iterar_notas_md(pasta_base: Path, vault: Path):
    for item in sorted(pasta_base.rglob("*.md")):
        if item.is_symlink() or not item.is_file():
            continue
        try:
            rel = item.relative_to(vault)
            res = item.resolve()
        except (ValueError, OSError):
            continue
        if _tem_segmento_oculto(rel):
            continue
        if vault not in res.parents:
            continue
        yield res, rel


def listar_notas_vault(subpasta: str = "") -> str:
    """Lista pastas e notas .md dentro do Obsidian Vault (ou de uma subpasta especifica)."""
    try:
        vault, alvo = _resolver_no_vault(subpasta, exigir_md=False, permitir_raiz=True)
    except ValueError as e:
        return f"Erro de seguranca/validacao no Vault: {e}"
    except OSError as e:
        return f"Erro ao acessar Obsidian Vault: {e}"

    if not alvo.exists() or not alvo.is_dir():
        return f"Subpasta nao encontrada no Vault: '{subpasta}'."

    subpastas = sorted(
        p.relative_to(vault).as_posix()
        for p in alvo.iterdir()
        if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
    )
    notas = list(_iterar_notas_md(alvo, vault))

    cabecalho = alvo.relative_to(vault).as_posix() if alvo != vault else "/"
    linhas = [f"=== OBSIDIAN VAULT ({vault}) · Pasta: {cabecalho} ==="]

    if subpastas:
        linhas.append("Pastas: " + ", ".join(subpastas))

    if not notas:
        linhas.append("Nenhuma nota .md encontrada nesta pasta.")
        return "\n".join(linhas)

    limite_exibicao = 120
    linhas.append(f"Notas .md encontradas ({len(notas)}):")
    for res, rel in notas[:limite_exibicao]:
        try:
            st = res.stat()
            kb = max(1, st.st_size // 1024) if st.st_size else 0
            dt = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            kb, dt = 0, "?"
        linhas.append(f"- {rel.as_posix()} ({kb} KB | {dt})")

    if len(notas) > limite_exibicao:
        linhas.append(f"... e mais {len(notas) - limite_exibicao} nota(s) omitida(s).")

    return "\n".join(linhas)


def _extrair_tags_nota(texto: str) -> list[str]:
    tags: list[str] = []
    vistos: set[str] = set()
    # Tags inline (#tag ou #sub/tag)
    for m in re.findall(r"(?<!\w)#([A-Za-z0-9_\-/À-ÿ]+)", texto or ""):
        t = "#" + m.lower()
        if t not in vistos:
            vistos.add(t)
            tags.append(t)
    # Tags em YAML frontmatter (tags: [a, b] ou lista - a)
    fm = re.match(r"^---\s*\n(.*?)\n---", texto or "", flags=re.S)
    if fm:
        bloco = fm.group(1)
        for m in re.findall(r"(?m)^\s*-\s*([A-Za-z0-9_\-/À-ÿ]+)\s*$", bloco):
            t = "#" + m.lower()
            if t not in vistos:
                vistos.add(t)
                tags.append(t)
        inline_fm = re.search(r"(?m)^tags\s*:\s*\[([^\]]+)\]", bloco)
        if inline_fm:
            for parte in inline_fm.group(1).split(","):
                limpa = parte.strip().strip('"').strip("'").lstrip("#").lower()
                if limpa and ("#" + limpa) not in vistos:
                    vistos.add("#" + limpa)
                    tags.append("#" + limpa)
    return tags


def buscar_no_vault(termo: str, limite: int = 10) -> str:
    """Pesquisa por titulo, conteudo e #tags em todas as notas .md do Vault e retorna trechos."""
    consulta = (termo or "").strip()
    if not consulta:
        return "Erro: informe um termo ou #tag para buscar no Obsidian Vault."

    try:
        vault = inicializar_vault()
    except OSError as e:
        return f"Erro ao acessar Obsidian Vault: {e}"

    max_res = max(1, min(int(limite), 30))
    eh_tag = consulta.startswith("#")
    consulta_norm = _normalizar(consulta.lstrip("#"))
    tokens = [t for t in consulta_norm.split() if len(t) >= 2] or [consulta_norm]

    ranqueados: list[tuple[int, str, list[str], str]] = []
    for res, rel in _iterar_notas_md(vault, vault):
        try:
            texto = res.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue

        rel_str = rel.as_posix()
        titulo_norm = _normalizar(rel.stem.replace("-", " ").replace("_", " "))
        tags = _extrair_tags_nota(texto)
        tags_norm = [_normalizar(t.lstrip("#")) for t in tags]
        texto_norm = _normalizar(texto)

        score = 0
        if eh_tag:
            if any(consulta_norm == tg or consulta_norm in tg for tg in tags_norm):
                score += 120
        else:
            if any(consulta_norm == tg for tg in tags_norm):
                score += 90
            elif any(consulta_norm in tg for tg in tags_norm):
                score += 50

        if consulta_norm and consulta_norm in titulo_norm:
            score += 80
        if consulta_norm and consulta_norm in texto_norm:
            score += 40

        hits_tokens = sum(1 for tk in tokens if tk in titulo_norm or tk in texto_norm or any(tk in tg for tg in tags_norm))
        if hits_tokens == len(tokens) and tokens:
            score += 20 + hits_tokens * 5
        elif hits_tokens > 0 and not eh_tag:
            score += hits_tokens * 3

        if score <= 0:
            continue

        # Extrai trecho relevante em volta da primeira linha que casar
        linhas_nota = texto.splitlines()
        trechos: list[str] = []
        for idx, ln in enumerate(linhas_nota):
            ln_norm = _normalizar(ln)
            if consulta_norm in ln_norm or any(tk in ln_norm for tk in tokens):
                ini = max(0, idx - 1)
                fim = min(len(linhas_nota), idx + 3)
                bloco = "\n".join(linhas_nota[ini:fim]).strip()
                if bloco:
                    trechos.append(bloco)
                if len(trechos) >= 2:
                    break

        preview = ("\n...\n".join(trechos) if trechos else texto[:320].strip())[:450]
        ranqueados.append((score, rel_str, tags[:8], preview))

    ranqueados.sort(key=lambda item: (-item[0], item[1].lower()))
    if not ranqueados:
        return f"Nenhuma nota encontrada no Obsidian Vault para '{consulta}'."

    saida = [f"=== BUSCA NO OBSIDIAN VAULT: '{consulta}' ({min(len(ranqueados), max_res)} de {len(ranqueados)}) ==="]
    for _score, rel_str, tags, preview in ranqueados[:max_res]:
        tag_txt = f" | tags: {', '.join(tags)}" if tags else ""
        saida.append(f"\n[{rel_str}{tag_txt}]\n{preview}")
    return "\n".join(saida)


def ler_nota_vault(caminho_relativo: str) -> str:
    """Le o conteudo completo de uma nota .md do Obsidian Vault."""
    try:
        vault, alvo = _resolver_no_vault(caminho_relativo, exigir_md=True, permitir_raiz=False)
    except ValueError as e:
        return f"Erro de seguranca/validacao no Vault: {e}"
    except OSError as e:
        return f"Erro ao acessar Obsidian Vault: {e}"

    if not alvo.is_file():
        return f"Nota nao encontrada no Vault: '{alvo.relative_to(vault).as_posix()}'."

    try:
        conteudo = alvo.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        return f"Erro ao ler nota no Vault: {e}"

    rel_str = alvo.relative_to(vault).as_posix()
    if len(conteudo) > LIMITE_LEITURA_CHARS:
        conteudo = conteudo[:LIMITE_LEITURA_CHARS] + "\n...[truncado]"
    return f"=== NOTA: {rel_str} ===\n{conteudo or '(nota vazia)'}"


def escrever_nota_vault(caminho_relativo: str, conteudo: str, anexar: bool = False) -> str:
    """Cria ou atualiza uma nota .md no Obsidian Vault (suporta [[wikilinks]] e #tags; anexar=True acrescenta ao final)."""
    try:
        vault, alvo = _resolver_no_vault(caminho_relativo, exigir_md=True, permitir_raiz=False)
    except ValueError as e:
        return f"Erro de seguranca/validacao no Vault: {e}"
    except OSError as e:
        return f"Erro ao acessar Obsidian Vault: {e}"

    texto = conteudo if conteudo is not None else ""
    rel_str = alvo.relative_to(vault).as_posix()

    try:
        alvo.parent.mkdir(parents=True, exist_ok=True)
        ja_existia = alvo.is_file()

        if anexar and ja_existia:
            atual = alvo.read_text(encoding="utf-8-sig", errors="replace")
            separador = "" if (not atual or atual.endswith("\n\n")) else ("\n" if atual.endswith("\n") else "\n\n")
            novo_conteudo = atual + separador + texto
            if novo_conteudo and not novo_conteudo.endswith("\n"):
                novo_conteudo += "\n"
            alvo.write_text(novo_conteudo, encoding="utf-8")
            acao = "anexada"
        else:
            final = texto if (not texto or texto.endswith("\n")) else (texto + "\n")
            alvo.write_text(final, encoding="utf-8")
            acao = "atualizada" if ja_existia else "criada"
    except OSError as e:
        return f"Erro ao salvar nota no Vault ({rel_str}): {e}"

    return f"Nota {acao} com sucesso no Obsidian Vault: {rel_str} ({len(texto)} caracteres)."


with contextlib.suppress(OSError):
    inicializar_vault()
