import os
import shutil
import subprocess
from pathlib import Path

import httpx

CONTAINER = "sandbox-agentes"
RUNTIME = shutil.which("podman") or shutil.which("docker") or "podman"
_BASE_DIR = Path(__file__).resolve().parent
_PASTA_FACULDADE = Path(
    os.getenv("PASTA_FACULDADE", "").strip() or (Path.home() / "Documents" / "agente-faculdade")
)

MONTAGENS = {
    "/workspace": (_BASE_DIR / "sandbox_files", True),
    "/downloads": (Path.home() / "Downloads", True),
    "/faculdade": (_PASTA_FACULDADE, False),
}


def resolver_seguro(pasta_raiz: Path, caminho_rel: str, escrita: bool = False) -> tuple[Path, str]:
    bruto = (caminho_rel or ".").strip().replace("\\", "/")
    base_host, pode_escrever, prefixo = pasta_raiz.resolve(), True, "/workspace"
    rel = bruto

    for pref, (host_dir, rw) in MONTAGENS.items():
        if bruto == pref or bruto.startswith(pref + "/"):
            base_host, pode_escrever, prefixo = host_dir.resolve(), rw, pref
            rel = bruto[len(pref):].lstrip("/") or "."
            break

    if escrita and not pode_escrever:
        raise ValueError(f"A pasta {prefixo} esta em modo SOMENTE LEITURA por seguranca.")

    alvo = (base_host / rel.lstrip("/")).resolve()
    if base_host not in alvo.parents and alvo != base_host:
        raise ValueError(f"Acesso negado fora de {prefixo}.")
    return alvo, prefixo


def listar_pasta(pasta_raiz: Path, subpasta: str = "/workspace") -> str:
    alvo, pref = resolver_seguro(pasta_raiz, subpasta, escrita=False)
    if not alvo.exists():
        return f"Pasta nao encontrada: {subpasta}"
    itens = sorted(alvo.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:60]
    linhas = [f"[DIR]  {p.name}" if p.is_dir() else f"[ARQ]  {p.name} ({p.stat().st_size // 1024} KB)" for p in itens]
    return f"Conteudo de {subpasta} ({pref}):\n" + ("\n".join(linhas) if linhas else "(vazio)")


def ler_arquivo_texto(pasta_raiz: Path, caminho: str, limite: int = 8000) -> str:
    alvo, _ = resolver_seguro(pasta_raiz, caminho, escrita=False)
    if not alvo.is_file():
        return f"Arquivo nao encontrado: {caminho}"
    texto = alvo.read_text(encoding="utf-8-sig", errors="replace")
    return texto[:limite] + ("\n...[truncado]" if len(texto) > limite else "")


def gravar_arquivo_texto(pasta_raiz: Path, caminho: str, conteudo: str) -> str:
    alvo, pref = resolver_seguro(pasta_raiz, caminho, escrita=True)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    alvo.write_text(conteudo, encoding="utf-8")
    return f"Arquivo salvo em {pref}/{alvo.name} ({len(conteudo)} caracteres)"


def rodar_comando_seguro(pasta_raiz: Path, comando: str) -> str:
    res = subprocess.run(
        [RUNTIME, "exec", CONTAINER, "bash", "-lc", comando],
        capture_output=True,
        text=True,
        timeout=25,
    )
    saida = (res.stdout or "") + ("\n[STDERR]\n" + res.stderr if res.stderr else "")
    return f"[SANDBOX LINUX | exit={res.returncode}]\n{saida[:5000]}"


async def buscar_url(url: str) -> str:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url)
        return f"Status {r.status_code} ({url}):\n{r.text[:6000]}"