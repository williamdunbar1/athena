"""Voz neural de alta qualidade para a Athena: STT local (`faster-whisper`) e TTS neural (`edge-tts` + `kokoro-onnx` offline).

As dependencias (`faster-whisper`, `edge-tts`, `kokoro-onnx`, `soundfile`) sao opcionais
(`[project.optional-dependencies] voice`) e carregadas via lazy import (`importlib.import_module`) para:
- Nao atrasar o boot da Athena nem baixar modelos de audio no GitHub Actions CI.
- Permitir fallback automatico de `edge-tts` para `kokoro-onnx` 100% local quando sem rede.
- Degradar com seguranca (`VozIndisponivelError`) permitindo fallback automatico no navegador.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import os
import re
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
VOZ_TTS_PADRAO = os.getenv("ATHENA_TTS_VOICE", "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
VOZ_KOKORO_PADRAO = "pf_dora"
KOKORO_MODEL_DIR_PADRAO = "sandbox_files/modelos_voz"
KOKORO_ARQUIVO_ONNX = "kokoro-v1.0.onnx"
KOKORO_ARQUIVO_VOZES = "voices-v1.0.bin"
MODELO_WHISPER_PADRAO = os.getenv("ATHENA_WHISPER_MODEL", "small").strip() or "small"

EXTENSOES_AUDIO_PERMITIDAS: frozenset[str] = frozenset(
    {"webm", "ogg", "wav", "mp3", "m4a", "mp4", "flac", "opus"}
)
MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_TTS_CHARS = 4000

_WHISPER_LOCK = threading.Lock()
_WHISPER_MODEL: Any = None
_KOKORO_LOCK = threading.Lock()
_KOKORO_MODEL: Any = None
_KOKORO_CACHE_KEY: tuple[str, str] | None = None

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


def _obter_tts_engine() -> str:
    motor = (os.getenv("ATHENA_TTS_ENGINE", "auto") or "auto").strip().lower()
    if motor in ("auto", "kokoro", "edge"):
        return motor
    return "auto"


def _obter_dir_kokoro() -> Path:
    bruto = (os.getenv("KOKORO_MODEL_DIR", KOKORO_MODEL_DIR_PADRAO) or KOKORO_MODEL_DIR_PADRAO).strip()
    p = Path(bruto).expanduser()
    if not p.is_absolute():
        p = BASE_DIR / p
    return p


def _resolver_voz_kokoro(voz: str | None) -> str:
    v = (voz or "").strip()
    if v and "_" in v and len(v) <= 32 and "Neural" not in v:
        return v
    return VOZ_KOKORO_PADRAO


def _preparar_texto_tts(texto: str) -> str:
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
    return texto_limpo


def _obter_modelo_kokoro() -> Any:
    """Lazy-loading thread-safe do modelo Kokoro ONNX a partir de `KOKORO_MODEL_DIR`."""
    global _KOKORO_MODEL, _KOKORO_CACHE_KEY

    pasta = _obter_dir_kokoro()
    arq_onnx = pasta / KOKORO_ARQUIVO_ONNX
    arq_vozes = pasta / KOKORO_ARQUIVO_VOZES
    if not arq_onnx.is_file() or not arq_vozes.is_file():
        raise VozIndisponivelError(
            f"Arquivos do Kokoro nao encontrados em '{pasta}' "
            f"(esperados: {KOKORO_ARQUIVO_ONNX} e {KOKORO_ARQUIVO_VOZES})."
        )

    chave = (str(arq_onnx.resolve()), str(arq_vozes.resolve()))
    if _KOKORO_MODEL is not None and chave == _KOKORO_CACHE_KEY:
        return _KOKORO_MODEL

    with _KOKORO_LOCK:
        if _KOKORO_MODEL is not None and chave == _KOKORO_CACHE_KEY:
            return _KOKORO_MODEL
        try:
            mod_kokoro = importlib.import_module("kokoro_onnx")
            kokoro_cls = mod_kokoro.Kokoro
        except (ModuleNotFoundError, AttributeError) as exc:
            raise VozIndisponivelError(
                "kokoro-onnx nao esta instalado. Instale com `uv sync --extra voice`."
            ) from exc

        _KOKORO_MODEL = kokoro_cls(chave[0], chave[1])
        _KOKORO_CACHE_KEY = chave
        return _KOKORO_MODEL


def sintetizar_fala_kokoro(texto: str, voz: str = VOZ_KOKORO_PADRAO) -> tuple[bytes, str]:
    """Sintetiza fala neural pt-BR 100% offline em WAV via `kokoro-onnx` + `soundfile`."""
    texto_limpo = _preparar_texto_tts(texto)
    modelo = _obter_modelo_kokoro()
    try:
        sf = importlib.import_module("soundfile")
    except ModuleNotFoundError as exc:
        raise VozIndisponivelError(
            "soundfile nao esta instalado. Instale com `uv sync --extra voice`."
        ) from exc

    voz_limpa = _resolver_voz_kokoro(voz)
    amostras, taxa_amostragem = modelo.create(
        texto_limpo,
        voice=voz_limpa,
        speed=1.0,
        lang="pt-br",
    )
    buf = io.BytesIO()
    sf.write(buf, amostras, int(taxa_amostragem), format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()
    if not wav_bytes:
        raise RuntimeError("Nenhum audio WAV gerado pelo motor kokoro-onnx.")
    return wav_bytes, "audio/wav"


async def _sintetizar_fala_edge(texto_limpo: str, voz: str = VOZ_TTS_PADRAO) -> tuple[bytes, str]:
    """Sintetiza fala neural pt-BR em MP3 via `edge-tts`."""
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
    return bytes(blocos), "audio/mpeg"


async def sintetizar_fala(texto: str, voz: str = VOZ_TTS_PADRAO) -> tuple[bytes, str]:
    """Sintetiza fala neural pt-BR retornando `(audio_bytes, mime_type)`.

    - `ATHENA_TTS_ENGINE=kokoro`: usa `kokoro-onnx` 100% offline (`audio/wav`).
    - `ATHENA_TTS_ENGINE=auto` (padrao): tenta `edge-tts` (`audio/mpeg`) e cai automaticamente
      para `kokoro-onnx` (`audio/wav`) se sem rede ou se `edge-tts` falhar.
    - `ATHENA_TTS_ENGINE=edge`: usa apenas `edge-tts` (`audio/mpeg`).
    """
    texto_limpo = _preparar_texto_tts(texto)
    engine = _obter_tts_engine()

    if engine == "kokoro":
        return await asyncio.to_thread(sintetizar_fala_kokoro, texto_limpo, _resolver_voz_kokoro(voz))

    if engine == "edge":
        return await _sintetizar_fala_edge(texto_limpo, voz=voz)

    # Modo auto: edge-tts -> fallback automatico para kokoro-onnx offline
    try:
        return await _sintetizar_fala_edge(texto_limpo, voz=voz)
    except Exception as exc_edge:
        try:
            return await asyncio.to_thread(sintetizar_fala_kokoro, texto_limpo, _resolver_voz_kokoro(voz))
        except VozIndisponivelError:
            raise exc_edge from None
        except Exception:
            raise exc_edge from None
