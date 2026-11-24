"""Pre-otimizacao automatica de imagens, PDFs e documentos Office/texto antes do envio para a API multimodal.

Recursos:
- `otimizar_imagem_para_llm`: corrige orientacao EXIF, compoe transparencia sobre `#111214`,
  redimensiona proporcionalmente (max_lado=1280px, LANCZOS) e exporta JPEG otimizado
  (preservando os bytes/MIME originais caso o JPEG fique maior).
- `processar_pdf_para_llm`: extrai texto via `pypdf.PdfReader`; PDFs digitais (>= 200 chars uteis)
  sao enviados como texto compactado (economizando milhares de tokens visuais), enquanto PDFs
  escaneados seguem como binario nativo `application/pdf`.
- `extrair_documento_para_markdown`: conversor universal com lazy import para `docling` (opcional,
  ativado por `ATHENA_USE_DOCLING=true`), `markitdown` (Office/PDF/HTML) e fallback automatico
  para `pypdf`/stdlib sem pesar no CI.
"""

from __future__ import annotations

import html as _html
import importlib
import io
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eficiencia import compactar_saida_tool

if TYPE_CHECKING:
    from PIL import Image as PILImage

# Carregados de forma lazy (e facilmente monkeypatchaveis em testes)
PdfReader: Any = None

MAX_LADO_IMAGEM_PADRAO = 1280
QUALIDADE_JPEG_PADRAO = 82
FUNDO_ALFA_DARK = (17, 18, 20)  # #111214
MIN_CHARS_PDF_DIGITAL = 200
MAX_CHARS_PDF_PADRAO = 18000

EXTENSOES_OFFICE: frozenset[str] = frozenset({".docx", ".pptx", ".xlsx"})
EXTENSOES_TEXTO_DIRETO: frozenset[str] = frozenset({".csv", ".html", ".htm", ".txt", ".md"})
EXTENSOES_DOCUMENTO_SUPORTADAS: frozenset[str] = frozenset(
    {".pdf"} | EXTENSOES_OFFICE | EXTENSOES_TEXTO_DIRETO
)

_EXT_PARA_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_FORMAT_PARA_MIME: dict[str, str] = {
    "JPEG": "image/jpeg",
    "JPG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def _inferir_mime_original(nome_arquivo: str, formato_pil: str | None = None) -> str:
    ext = Path(nome_arquivo or "").suffix.lower()
    if ext in _EXT_PARA_MIME:
        return _EXT_PARA_MIME[ext]
    if formato_pil:
        fmt = formato_pil.strip().upper()
        if fmt in _FORMAT_PARA_MIME:
            return _FORMAT_PARA_MIME[fmt]
    return "image/png"


def _converter_para_rgb(img: PILImage.Image) -> PILImage.Image:
    from PIL import Image

    tem_alfa = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if tem_alfa:
        rgba = img.convert("RGBA")
        fundo = Image.new("RGB", rgba.size, FUNDO_ALFA_DARK)
        fundo.paste(rgba, mask=rgba.getchannel("A"))
        return fundo
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def otimizar_imagem_para_llm(
    dados: bytes,
    nome_arquivo: str = "imagem.png",
    max_lado: int = MAX_LADO_IMAGEM_PADRAO,
    qualidade: int = QUALIDADE_JPEG_PADRAO,
) -> tuple[bytes, str, dict[str, Any]]:
    """Otimiza imagem (EXIF + RGB + redimensionamento proporcional <= `max_lado` + JPEG).

    Se o JPEG resultante ficar maior que o arquivo original, preserva os bytes e o MIME originais.
    """
    bytes_antes = len(dados or b"")
    mime_fallback = _inferir_mime_original(nome_arquivo)
    if not dados:
        return (
            b"",
            mime_fallback,
            {"bytes_antes": 0, "bytes_depois": 0, "reducao_pct": 0.0, "dimensoes": (0, 0)},
        )

    limite_lado = max(64, int(max_lado))
    qual = max(30, min(int(qualidade), 95))

    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(dados)) as img_raw:
            mime_orig = _inferir_mime_original(nome_arquivo, img_raw.format)
            img = ImageOps.exif_transpose(img_raw)
            dim_orig = (int(img.width), int(img.height))

            img_rgb = _converter_para_rgb(img)
            w, h = img_rgb.size
            maior_lado = max(w, h)
            if maior_lado > limite_lado:
                escala = limite_lado / float(maior_lado)
                novo_w = max(1, int(round(w * escala)))
                novo_h = max(1, int(round(h * escala)))
                img_rgb = img_rgb.resize((novo_w, novo_h), Image.Resampling.LANCZOS)

            dim_final = (int(img_rgb.width), int(img_rgb.height))
            buf = io.BytesIO()
            img_rgb.save(buf, format="JPEG", quality=qual, optimize=True)
            dados_jpeg = buf.getvalue()
    except Exception:
        return (
            dados,
            mime_fallback,
            {
                "bytes_antes": bytes_antes,
                "bytes_depois": bytes_antes,
                "reducao_pct": 0.0,
                "dimensoes": (0, 0),
            },
        )

    if len(dados_jpeg) > bytes_antes:
        return (
            dados,
            mime_orig,
            {
                "bytes_antes": bytes_antes,
                "bytes_depois": bytes_antes,
                "reducao_pct": 0.0,
                "dimensoes": dim_orig,
            },
        )

    bytes_depois = len(dados_jpeg)
    reducao_pct = round(((bytes_antes - bytes_depois) / bytes_antes) * 100.0, 1) if bytes_antes > 0 else 0.0
    return (
        dados_jpeg,
        "image/jpeg",
        {
            "bytes_antes": bytes_antes,
            "bytes_depois": bytes_depois,
            "reducao_pct": max(0.0, reducao_pct),
            "dimensoes": dim_final,
        },
    )


def processar_pdf_para_llm(
    dados: bytes,
    nome_arquivo: str = "documento.pdf",
    max_chars: int = MAX_CHARS_PDF_PADRAO,
) -> dict[str, Any]:
    """Classifica PDF em digital (`modo="texto"`) ou escaneado (`modo="binario"`).

    - Digital (>= 200 caracteres uteis extraidos): limpa espacos excessivos, aplica `max_chars`
      (preservando inicio/fim) e retorna texto puro para economizar tokens visuais.
    - Escaneado (< 200 caracteres uteis ou falha de leitura): preserva os bytes para leitura
      multimodal nativa do Gemini.
    """
    teto = max(500, int(max_chars))
    try:
        reader_cls = PdfReader
        if reader_cls is None:
            from pypdf import PdfReader as _PdfReader

            reader_cls = _PdfReader
        reader = reader_cls(io.BytesIO(dados))
        paginas = list(reader.pages or [])
        total_paginas = len(paginas)
        blocos: list[str] = []
        for idx, pag in enumerate(paginas, start=1):
            txt_pag = (pag.extract_text() or "").strip()
            if txt_pag:
                blocos.append(f"[Página {idx}]\n{txt_pag}")
    except Exception:
        return {
            "modo": "binario",
            "dados": dados,
            "mime_type": "application/pdf",
            "paginas": 0,
            "arquivo": nome_arquivo,
        }

    texto_bruto = "\n\n".join(blocos)
    chars_uteis = len(re.sub(r"\s+", "", texto_bruto))
    if chars_uteis >= MIN_CHARS_PDF_DIGITAL:
        texto_limpo = compactar_saida_tool(texto_bruto, max_chars=teto)
        return {
            "modo": "texto",
            "conteudo": texto_limpo,
            "paginas": total_paginas,
            "arquivo": nome_arquivo,
            "chars": len(texto_limpo),
        }

    return {
        "modo": "binario",
        "dados": dados,
        "mime_type": "application/pdf",
        "paginas": total_paginas,
        "arquivo": nome_arquivo,
    }


def docling_habilitado() -> bool:
    """Retorna True somente quando `ATHENA_USE_DOCLING=true` estiver ativo no ambiente."""
    return (os.getenv("ATHENA_USE_DOCLING", "false") or "false").strip().lower() in (
        "1",
        "true",
        "sim",
        "yes",
        "on",
    )


def _extrair_com_docling(dados: bytes, nome_arquivo: str) -> str | None:
    """Tenta converter PDF complexo via `docling` (lazy import). Retorna None se ausente/falhar."""
    if not docling_habilitado():
        return None
    ext = Path(nome_arquivo or "documento.pdf").suffix.lower() or ".pdf"
    try:
        mod = importlib.import_module("docling.document_converter")
        converter_cls = getattr(mod, "DocumentConverter", None)
        if converter_cls is None:
            return None
    except Exception:
        return None

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(dados)
            tmp_path = Path(tmp.name)
        converter = converter_cls()
        res = converter.convert(str(tmp_path))
        doc = getattr(res, "document", None)
        if doc is not None and hasattr(doc, "export_to_markdown"):
            txt = str(doc.export_to_markdown() or "").strip()
            return txt or None
        return None
    except Exception:
        return None
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()


def _extrair_com_markitdown(dados: bytes, nome_arquivo: str) -> str | None:
    """Tenta converter documento (.pdf/.docx/.pptx/.xlsx/.html/.csv) via `markitdown` (lazy import)."""
    ext = Path(nome_arquivo or "documento.bin").suffix.lower() or ".bin"
    try:
        mod = importlib.import_module("markitdown")
        md_cls = getattr(mod, "MarkItDown", None)
        if md_cls is None:
            return None
    except Exception:
        return None

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(dados)
            tmp_path = Path(tmp.name)
        md = md_cls()
        res = md.convert(str(tmp_path))
        txt = (
            getattr(res, "text_content", None)
            or getattr(res, "markdown", None)
            or (res if isinstance(res, str) else "")
        )
        limpo = str(txt or "").strip()
        return limpo or None
    except Exception:
        return None
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()


def _extrair_texto_stdlib(dados: bytes, ext: str) -> str:
    """Fallback leve em stdlib pura para `.txt`, `.md`, `.csv` e `.html`."""
    try:
        bruto = dados.decode("utf-8-sig")
    except UnicodeDecodeError:
        bruto = dados.decode("latin-1", errors="replace")

    if ext in (".html", ".htm"):
        sem_script = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", bruto)
        sem_tags = re.sub(r"(?s)<[^>]+>", "\n", sem_script)
        return _html.unescape(sem_tags).strip()

    return bruto.strip()


def extrair_documento_para_markdown(
    dados: bytes,
    nome_arquivo: str,
    max_chars: int = MAX_CHARS_PDF_PADRAO,
) -> dict[str, Any]:
    """Conversor universal de documentos para Markdown enxuto otimizado para LLM/RAG.

    Suporta: `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.csv`, `.html`, `.htm`, `.txt`, `.md`.
    - PDF: tenta `docling` (se `ATHENA_USE_DOCLING=true`), depois `markitdown`, e faz fallback
      automatico para `pypdf` (`processar_pdf_para_llm`, preservando binario se escaneado).
    - Office (`.docx`, `.pptx`, `.xlsx`): usa `markitdown` via lazy import com erro amigavel se ausente.
    - Texto/CSV/HTML/MD: tenta `markitdown` (HTML/CSV) com fallback automatico em stdlib pura.
    """
    teto = max(500, int(max_chars))
    nome = (nome_arquivo or "documento").strip() or "documento"
    ext = Path(nome).suffix.lower()

    if ext not in EXTENSOES_DOCUMENTO_SUPORTADAS:
        return {
            "ok": False,
            "modo": "erro",
            "motor": "nenhum",
            "arquivo": nome,
            "erro": (
                f"Extensao '{ext or 'desconhecida'}' nao suportada pelo conversor de documentos. "
                "Use .pdf, .docx, .pptx, .xlsx, .csv, .html, .txt ou .md."
            ),
        }

    if not dados:
        return {
            "ok": False,
            "modo": "erro",
            "motor": "nenhum",
            "arquivo": nome,
            "erro": f"O arquivo '{nome}' esta vazio.",
        }

    if ext == ".pdf":
        # 1) Docling (opcional, apenas quando ATHENA_USE_DOCLING=true e instalado)
        txt_docling = _extrair_com_docling(dados, nome)
        if txt_docling and len(re.sub(r"\s+", "", txt_docling)) >= MIN_CHARS_PDF_DIGITAL:
            comp = compactar_saida_tool(txt_docling, max_chars=teto)
            return {
                "ok": True,
                "modo": "texto",
                "motor": "docling",
                "conteudo": comp,
                "paginas": 0,
                "arquivo": nome,
                "chars": len(comp),
            }

        # 2) MarkItDown (opcional)
        txt_md = _extrair_com_markitdown(dados, nome)
        if txt_md and len(re.sub(r"\s+", "", txt_md)) >= MIN_CHARS_PDF_DIGITAL:
            comp = compactar_saida_tool(txt_md, max_chars=teto)
            return {
                "ok": True,
                "modo": "texto",
                "motor": "markitdown",
                "conteudo": comp,
                "paginas": 0,
                "arquivo": nome,
                "chars": len(comp),
            }

        # 3) Fallback padrao: pypdf (digital -> texto; escaneado -> binario nativo)
        res_pdf = processar_pdf_para_llm(dados, nome_arquivo=nome, max_chars=teto)
        return {
            "ok": True,
            "motor": "pypdf" if res_pdf.get("modo") == "texto" else "binario_pdf",
            **res_pdf,
        }

    if ext in EXTENSOES_OFFICE:
        txt_office = _extrair_com_markitdown(dados, nome)
        if txt_office:
            comp = compactar_saida_tool(txt_office, max_chars=teto)
            return {
                "ok": True,
                "modo": "texto",
                "motor": "markitdown",
                "conteudo": comp,
                "arquivo": nome,
                "chars": len(comp),
            }
        return {
            "ok": False,
            "modo": "indisponivel",
            "motor": "nenhum",
            "arquivo": nome,
            "erro": (
                f"Nao foi possivel converter '{nome}' ({ext}): extra opcional 'docs' (markitdown) "
                "nao esta instalado. Instale com `uv sync --extra docs`."
            ),
        }

    # .csv, .html, .htm, .txt, .md
    if ext in (".html", ".htm", ".csv"):
        txt_mk = _extrair_com_markitdown(dados, nome)
        if txt_mk:
            comp = compactar_saida_tool(txt_mk, max_chars=teto)
            return {
                "ok": True,
                "modo": "texto",
                "motor": "markitdown",
                "conteudo": comp,
                "arquivo": nome,
                "chars": len(comp),
            }

    txt_plain = _extrair_texto_stdlib(dados, ext)
    comp_plain = compactar_saida_tool(txt_plain, max_chars=teto)
    return {
        "ok": True,
        "modo": "texto",
        "motor": "stdlib",
        "conteudo": comp_plain,
        "arquivo": nome,
        "chars": len(comp_plain),
    }
