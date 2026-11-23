"""Voz neural de alta qualidade para a Athena: STT local (`faster-whisper`) e TTS neural (`edge-tts`).

As dependencias (`faster-whisper`, `edge-tts`) sao opcionais (`[project.optional-dependencies] voice`)
e carregadas via lazy import (`importlib.import_module`) para:
- Nao atrasar o boot da Athena nem baixar modelos de audio no GitHub Actions CI.
- Degradar com seguranca (`VozIndisponivelError`) permitindo fallback automatico no navegador.
"""

from __future__ import annotations

import importlib
import os
import re
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

VOZ_TTS_PADRAO = os.getenv("ATHENA_TTS_VOICE", "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
MODELO_WHISPER_PADRAO = os.getenv("ATHENA_WHISPER_MODEL", "small").strip() or "small"

EXTENSOES_AUDIO_PERMITIDAS: frozenset[str] = frozenset(
    {"webm", "ogg", "wav", "mp3", "m4a", "mp4", "flac", "opus"}
)
MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_TTS_CHARS = 4000

_WHISPER_LOCK = threading.Lock()
_WHISPER_MODEL: Any = None

_RE_BLOCO_CODIGO = re.compile(r"```[\s\S]*?```")
_RE_CODIGO_INLINE = re.compile(r"`([^`]+)`")
_RE_IMAGEM_MD = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_RE_LINK_MD = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_URL = re.compile(r"https?://\S+")
_RE_TAG_HTML = re.compile(r"<[^>]+>")
_RE_TITULO_MD = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_RE_SEPARADOR_MD = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)
_RE_TABELA_SEP = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*$", re.MULTILINE)
_RE_LISTA_MD = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s+", re.MULTILINE)
_RE_CITACAO_MD = re.compile(r"^\s*>\s?", re.MULTILINE)
_RE_EMOJI = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "\u2600-\u27bf"
    "\ufe0f"
    "\u200d"
    "]+",
    flags=re.UNICODE,
)


class VozIndisponivelError(RuntimeError):
    """Levantada quando os extras opcionais de voz (`faster-whisper` ou `edge-tts`) nao estao instalados."""


def limpar_texto_para_fala(texto: str) -> str:
    """Limpa Markdown, blocos de codigo, links, simbolos tecnicos e emojis para leitura natural em pt-BR."""
    s = str(texto or "")
    if not s.strip():
        return ""

    s = _RE_BLOCO_CODIGO.sub(" trecho de código omitido. ", s)
    s = _RE_IMAGEM_MD.sub(lambda m: f" {m.group(1).strip()} " if m.group(1).strip() else " ", s)
    s = _RE_LINK_MD.sub(r"\1", s)
    s = _RE_URL.sub(" ", s)
    s = _RE_CODIGO_INLINE.sub(r"\1", s)
    s = _RE_TAG_HTML.sub(" ", s)
    s = _RE_TABELA_SEP.sub(" ", s)
    s = _RE_SEPARADOR_MD.sub(". ", s)
    s = _RE_TITULO_MD.sub("", s)
    s = _RE_CITACAO_MD.sub("", s)
    s = _RE_LISTA_MD.sub("", s)
    s = _RE_EMOJI.sub(" ", s)

    # Remove formatacao tecnica remanescente mantendo pontuacao natural
    s = s.replace("|", ", ").replace("_", " ")
    s = re.sub(r"[*~`#]+", "", s)
    s = re.sub(r"\r\n|\r", "\n", s)
    s = re.sub(r"\n{2,}", ". ", s)
    s = s.replace("\n", " ")
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sanitizar_extensao_audio(extensao: str) -> str:
    ext = (extensao or "webm").strip().lower().lstrip(".")
    if "/" in ext:
        ext = ext.split("/")[-1]
    if ";" in ext:
        ext = ext.split(";", 1)[0].strip()
    if ext == "mpeg":
        ext = "mp3"
    if ext == "x-wav":
        ext = "wav"
    if ext not in EXTENSOES_AUDIO_PERMITIDAS:
        return "webm"
    return ext


def _obter_modelo_whisper() -> Any:
    """Lazy-loading thread-safe do WhisperModel (`small`, CPU `int8`) apenas na primeira transcricao."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL

    with _WHISPER_LOCK:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL
        try:
            modulo = importlib.import_module("faster_whisper")
            whisper_cls = modulo.WhisperModel
        except (ModuleNotFoundError, AttributeError) as exc:
            raise VozIndisponivelError(
                "faster-whisper nao esta instalado. Instale com `uv sync --extra voice`."
            ) from exc

        _WHISPER_MODEL = whisper_cls(
            MODELO_WHISPER_PADRAO,
            device="cpu",
            compute_type="int8",
        )
        return _WHISPER_MODEL


def transcrever_audio(audio_bytes: bytes, extensao: str = "webm") -> str:
    """Transcreve audio em portugues localmente via `faster-whisper` usando arquivo temporario seguro."""
    if not audio_bytes:
        return ""
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError(f"Audio excede o limite maximo permitido de {MAX_AUDIO_BYTES // (1024 * 1024)} MB.")

    modelo = _obter_modelo_whisper()
    ext = _sanitizar_extensao_audio(extensao)
    tmp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(prefix="athena_stt_", suffix=f".{ext}", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = Path(tmp.name)

        segmentos, _info = modelo.transcribe(
            str(tmp_path),
            language="pt",
            vad_filter=True,
        )
        partes = [str(getattr(seg, "text", "") or "").strip() for seg in segmentos]
        return " ".join(p for p in partes if p).strip()
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink(missing_ok=True)


async def sintetizar_fala(texto: str, voz: str = VOZ_TTS_PADRAO) -> bytes:
    """Sintetiza fala neural pt-BR em MP3 via `edge-tts` a partir do texto limpo."""
    texto_limpo = limpar_texto_para_fala(texto)
    if not texto_limpo:
        raise ValueError("Texto vazio apos limpeza para sintese de fala.")

    if len(texto_limpo) > MAX_TTS_CHARS:
        corte = texto_limpo[:MAX_TTS_CHARS]
        ultimo_ponto = max(corte.rfind(". "), corte.rfind("! "), corte.rfind("? "))
        if ultimo_ponto > MAX_TTS_CHARS // 2:
            texto_limpo = corte[: ultimo_ponto + 1].strip()
        else:
            texto_limpo = corte.rsplit(" ", 1)[0].strip() + "."

    try:
        edge_tts = importlib.import_module("edge_tts")
        communicate_cls = edge_tts.Communicate
    except (ModuleNotFoundError, AttributeError) as exc:
        raise VozIndisponivelError(
            "edge-tts nao esta instalado. Instale com `uv sync --extra voice`."
        ) from exc

    voz_limpa = (voz or VOZ_TTS_PADRAO).strip() or VOZ_TTS_PADRAO
    comunicador = communicate_cls(texto_limpo, voice=voz_limpa)
    blocos = bytearray()
    async for chunk in comunicador.stream():
        if isinstance(chunk, dict) and chunk.get("type") == "audio":
            dados = chunk.get("data")
            if isinstance(dados, (bytes, bytearray)):
                blocos.extend(dados)

    if not blocos:
        raise RuntimeError("Nenhum audio retornado pelo servico edge-tts.")
    return bytes(blocos)
