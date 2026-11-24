import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import date, datetime
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from pydantic_ai import (
    AgentRunResultEvent,
    BinaryContent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.exceptions import ModelHTTPError, ToolFailedError
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from agentes import (
    MODELO_OLLAMA_ID,
    MODELO_PADRAO,
    OPCOES_MODELO,
    ROTULOS_MODELO,
    TIMEOUT_TENTATIVA_AUTO_SEGUNDOS,
    ContextoMissao,
    eh_erro_503,
    listar_agentes_diretos_publico,
    normalizar_agente_alvo,
    obter_agente_direto,
    obter_fila_modelos_auto,
    obter_modelo,
    orquestrador,
    registrar_cooldown_modelo,
    resolver_modelo_execucao,
    sanitizar_resposta_ollama_offline,
    verificar_ollama_disponivel,
)
from athena_lab import (
    STATUS_ACIONAVEIS_LAB,
    aprovar_proposta_lab,
    listar_propostas_lab,
    obter_status_cli_antigravity,
    obter_status_lab,
    rejeitar_proposta_lab,
    rodar_ciclo_rd,
    sentinel_ativo,
    verificar_erros_logfire_sentinel,
)
from auditoria import (
    aprovar_pasta,
    listar_atividades,
    listar_pastas_aprovadas,
    revogar_pasta,
)
from compactador import (
    agendar_extracao_memorias,
    limpar_resumos_conversa,
    preparar_historico_compactado,
)
from fast_path import MODELO_FAST_PATH, tentar_fast_path
from ferramentas_estudos import PASTA_PDFS, formatar_nome_materia
from ferramentas_estudos import _conn as conn_estudos
from ferramentas_faxina import (
    aprovar_todas_pendentes,
    definir_pasta_extra_autorizada,
    desfazer_movimentacao,
    executar_acao_aprovada,
    listar_acoes_faxina,
    recusar_acao,
)
from ferramentas_investimentos import (
    _conn as conn_invest,
)
from ferramentas_investimentos import (
    _normalizar_e_buscar_historico,
    importar_extrato_mercado_pago,
    obter_painel_carteira_real,
    responder_proposta,
    salvar_ativo_real_estruturado,
)
from ferramentas_navegador import (
    cancelar_controle_agente,
    retomar_controle_agente,
)
from ferramentas_windows import (
    PASTA_PRINTS,
    aprovar_acao_windows,
    capturar_tela_atual,
    obter_status_acoes_windows,
    recusar_acao_windows,
)
from ferramentas_windows import (
    listar_whitelist as listar_whitelist_windows,
)
from ferramentas_windows import (
    remover_whitelist as remover_whitelist_windows,
)
from grafo_obsidian import estatisticas_grafo, exportar_grafo_visual
from logging_config import configurar_logging, obter_logger
from mcp_manager import (
    encerrar_conexoes_mcp,
    iniciar_conexoes_mcp,
    listar_servidores_mcp,
)
from memoria import (
    apagar_fato,
    atualizar_titulo_conversa,
    criar_conversa,
    deletar_conversa,
    init_db,
    limpar_mensagens,
    listar_conversas,
    listar_fatos,
    listar_mensagens,
    montar_contexto_prompt,
    obter_conversa,
    obter_resumo_telemetria,
    registrar_telemetria,
    salvar_mensagem,
)
from memoria_temporal import resumo_memoria_temporal
from observabilidade import configurar_observabilidade
from otimizador_midia import (
    EXTENSOES_DOCUMENTO_SUPORTADAS,
    extrair_documento_para_markdown,
    otimizar_imagem_para_llm,
)
from rag_qdrant import obter_estatisticas_rag, sincronizar_indice_vetorial
from rotinas import (
    alterar_status_rotina,
    atualizar_rotina,
    criar_rotina_dinamica,
    encerrar_agendador,
    excluir_rotina_dinamica,
    executar_rotina_agora,
    executar_rotina_dinamica,
    iniciar_agendador,
    listar_rotinas,
    listar_rotinas_dinamicas,
)
from semantic_cache import (
    MODELO_CACHE_SEMANTICO,
    consultar_cache_semantico,
    estatisticas_cache_semantico,
    limpar_cache_semantico,
    salvar_cache_semantico,
)
from skills_manager import listar_skills
from teach_bot import (
    finalizar_gravacao_skill,
    iniciar_gravacao_skill,
    obter_status_gravacao,
)
from ulife_v2 import obter_dados_ulife_estruturados, sincronizar_ulife
from voz import (
    MAX_AUDIO_BYTES,
    VOZ_TTS_PADRAO,
    VozIndisponivelError,
    sintetizar_fala,
    transcrever_audio,
)

configurar_logging()
logger = obter_logger("servidor")
init_db()


class _FiltroStatusAtualAccessLog(logging.Filter):
    """Silencia o spam de `GET /api/status-atual` nos logs de acesso do Uvicorn."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "/api/status-atual" not in record.getMessage()
        except Exception:
            return True


def _silenciar_log_status_atual() -> None:
    logger_acesso = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _FiltroStatusAtualAccessLog) for f in logger_acesso.filters):
        logger_acesso.addFilter(_FiltroStatusAtualAccessLog())
    for handler in logger_acesso.handlers:
        if not any(isinstance(f, _FiltroStatusAtualAccessLog) for f in handler.filters):
            handler.addFilter(_FiltroStatusAtualAccessLog())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _silenciar_log_status_atual()
    iniciar_agendador()
    servidores_mcp = await iniciar_conexoes_mcp()
    logger.info(
        "athena_iniciada",
        mcp_ativos=sum(1 for s in servidores_mcp if s.get("status") == "ativo"),
        skills_ativas=len(listar_skills()),
        pin_ativo=_pin_ativo(),
    )
    try:
        yield
    finally:
        logger.info("athena_encerrando", tarefas_ativas=len(TAREFAS_ATIVAS))
        for reg in list(TAREFAS_ATIVAS.values()):
            t = reg.get("task")
            if t is not None and not t.done():
                t.cancel()
        await encerrar_conexoes_mcp()
        encerrar_agendador()


app = FastAPI(title="Athena", lifespan=lifespan)
configurar_observabilidade(app)
BASE_DIR = Path(__file__).resolve().parent

ATHENA_PIN = os.getenv("ATHENA_PIN", "").strip()
_ENV_SECRET = os.getenv("ATHENA_SECRET", "").strip()
_SESSION_SECRET: bytes = (
    _ENV_SECRET.encode("utf-8")
    if _ENV_SECRET
    else (
        hashlib.sha256(f"athena-pin-hmac:{ATHENA_PIN}:{BASE_DIR}".encode()).digest()
        if ATHENA_PIN
        else secrets.token_bytes(32)
    )
)
COOKIE_SESSAO = "athena_session"
SESSION_MAX_AGE = 604800  # 7 dias
MAX_TENTATIVAS_PIN = 5
BLOQUEIO_PIN_SEGUNDOS = 30
ROTAS_AUTH_PUBLICAS = frozenset({
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/biometria/login/opcoes",
    "/api/auth/biometria/login/verificar",
})
_TENTATIVAS_LOGIN: dict[str, dict[str, float | int]] = {}

WEBAUTHN_DB_PATH = BASE_DIR / "memoria.db"
WEBAUTHN_TTL_SEGUNDOS = 120
WEBAUTHN_USER_ID = b"athena-user-william"
WEBAUTHN_USER_NAME = "william@athena.local"
WEBAUTHN_RP_NAME = "Athena"
_WEBAUTHN_DESAFIOS: dict[str, dict] = {}


@contextmanager
def _conn_webauthn():
    c = sqlite3.connect(WEBAUTHN_DB_PATH, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS webauthn_credenciais (
                credential_id TEXT PRIMARY KEY,
                public_key BLOB NOT NULL,
                sign_count INTEGER NOT NULL,
                nome TEXT,
                criado_em TEXT
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


def _tem_credenciais_biometria() -> bool:
    with _conn_webauthn() as c:
        row = c.execute("SELECT 1 FROM webauthn_credenciais LIMIT 1").fetchone()
    return row is not None


def _listar_credenciais_biometria() -> list[tuple[str, bytes, int, str]]:
    with _conn_webauthn() as c:
        return c.execute(
            "SELECT credential_id, public_key, sign_count, COALESCE(nome, '') FROM webauthn_credenciais"
        ).fetchall()


def _limpar_desafios_expirados() -> None:
    agora = time.time()
    expirados = [cid for cid, info in _WEBAUTHN_DESAFIOS.items() if float(info.get("expira_em", 0.0)) <= agora]
    for cid in expirados:
        _WEBAUTHN_DESAFIOS.pop(cid, None)


def _extrair_rp_e_origin(request: Request) -> tuple[str, str]:
    host_hdr = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip().lower()
    rp_id = (host_hdr.split(":")[0] if host_hdr else (request.url.hostname or "localhost")).lower()
    if not host_hdr:
        porta = request.url.port
        host_hdr = f"{rp_id}:{porta}" if porta and porta not in (80, 443) else rp_id
    origin = f"{proto}://{host_hdr}"
    return rp_id, origin


def _gravar_cookie_sessao(response: Response) -> None:
    token = _gerar_cookie_sessao()
    response.set_cookie(
        key=COOKIE_SESSAO,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _pin_ativo() -> str:
    return ATHENA_PIN


def _chave_assinatura() -> bytes:
    return _SESSION_SECRET


def _gerar_cookie_sessao() -> str:
    nonce = secrets.token_hex(16)
    exp = int(time.time()) + SESSION_MAX_AGE
    payload = f"{nonce}.{exp}"
    sig = hmac.new(_chave_assinatura(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _sessao_valida(token: str | None) -> bool:
    if not _pin_ativo():
        return True
    if not token:
        return False
    partes = token.split(".")
    if len(partes) != 3:
        return False
    nonce, exp_str, sig = partes
    if not nonce or not exp_str.isdigit() or len(sig) != 64:
        return False
    if int(exp_str) <= int(time.time()):
        return False
    payload = f"{nonce}.{exp_str}"
    esperada = hmac.new(_chave_assinatura(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, esperada)


def _validar_pin_seguro(pin_recebido: str) -> bool:
    pin_esperado = _pin_ativo()
    if not pin_esperado:
        return True
    h_rec = hashlib.sha256((pin_recebido or "").strip().encode("utf-8")).digest()
    h_esp = hashlib.sha256(pin_esperado.encode("utf-8")).digest()
    return hmac.compare_digest(h_rec, h_esp)


def _obter_ip_cliente(request: Request) -> str:
    fwd = (request.headers.get("x-forwarded-for") or "").strip()
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "desconhecido"


@app.middleware("http")
async def middleware_autenticacao_pin(request: Request, call_next):
    path = request.url.path
    if _pin_ativo() and path.startswith("/api/") and path not in ROTAS_AUTH_PUBLICAS:
        cookie = request.cookies.get(COOKIE_SESSAO)
        if not _sessao_valida(cookie):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "erro": "Nao autenticado. Informe o PIN para acessar a Athena."},
            )
    return await call_next(request)

CONFIG = {
    "pasta_raiz": str((BASE_DIR / "sandbox_files").resolve()),
    "modelo": MODELO_PADRAO,
}

ESTADO_EXECUCAO: dict = {
    "ativo": False,
    "modelo": MODELO_PADRAO,
    "trilha": [],
}

PASTAS_CRITICAS_SISTEMA = (
    Path(r"C:\Windows").resolve(),
    Path(r"C:\Program Files").resolve(),
    Path(r"C:\Program Files (x86)").resolve(),
    Path(r"C:\ProgramData").resolve(),
    (Path.home() / "AppData").resolve(),
)


def _eh_pasta_critica_sistema(caminho: Path) -> bool:
    p = caminho.resolve()
    drive_sistema = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\").resolve()
    if p == drive_sistema:
        return True
    return any(p == bloq or bloq in p.parents for bloq in PASTAS_CRITICAS_SISTEMA)


def _aplicar_pasta_raiz(caminho: Path) -> dict:
    p = caminho.resolve()
    if not p.exists() or not p.is_dir():
        return {"ok": False, "erro": f"Pasta nao existe: {p}"}
    if _eh_pasta_critica_sistema(p):
        return {"ok": False, "erro": f"Pasta critica do sistema bloqueada por seguranca: {p}"}
    CONFIG["pasta_raiz"] = str(p)
    with suppress(ValueError):
        definir_pasta_extra_autorizada(p)
    return {"ok": True, "pasta_raiz": CONFIG["pasta_raiz"]}


class PedidoChat(BaseModel):
    mensagem: str
    modelo: str | None = None


class ConfigApp(BaseModel):
    pasta_raiz: str | None = None
    modelo: str | None = None


class ConfigRotina(BaseModel):
    hora: int
    minuto: int
    ativa: bool


class AtivoRealPayload(BaseModel):
    tipo: str
    nome: str
    valor_atual: float
    ticker: str = ""
    taxa_info: str = ""
    instituicao: str = "Mercado Pago"


class PedidoLoginPin(BaseModel):
    pin: str = ""


class PedidoWebAuthnVerificar(BaseModel):
    challenge_id: str
    credential: dict
    nome: str = ""


@app.get("/api/auth/status")
def auth_status(request: Request):
    protegido = bool(_pin_ativo())
    autenticado = (not protegido) or _sessao_valida(request.cookies.get(COOKIE_SESSAO))
    return {
        "protegido": protegido,
        "autenticado": autenticado,
        "biometria_disponivel": _tem_credenciais_biometria(),
    }


@app.post("/api/auth/login")
def auth_login(pedido: PedidoLoginPin, request: Request, response: Response):
    protegido = bool(_pin_ativo())
    if not protegido:
        return {"ok": True, "protegido": False, "autenticado": True}

    ip = _obter_ip_cliente(request)
    agora = time.time()
    reg = _TENTATIVAS_LOGIN.get(ip) or {"falhas": 0, "bloqueado_ate": 0.0}
    bloqueado_ate = float(reg.get("bloqueado_ate", 0.0))

    if agora < bloqueado_ate:
        restante = max(1, int(bloqueado_ate - agora + 0.999))
        return JSONResponse(
            status_code=429,
            content={
                "ok": False,
                "erro": f"Muitas tentativas incorretas. Aguarde {restante}s para tentar novamente.",
                "bloqueado_segundos": restante,
            },
        )

    if not _validar_pin_seguro(pedido.pin):
        falhas = int(reg.get("falhas", 0)) + 1
        if falhas >= MAX_TENTATIVAS_PIN:
            _TENTATIVAS_LOGIN[ip] = {"falhas": 0, "bloqueado_ate": agora + BLOQUEIO_PIN_SEGUNDOS}
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "erro": f"PIN incorreto ({MAX_TENTATIVAS_PIN} erros seguidos). Bloqueado por {BLOQUEIO_PIN_SEGUNDOS} segundos.",
                    "bloqueado_segundos": BLOQUEIO_PIN_SEGUNDOS,
                },
            )
        _TENTATIVAS_LOGIN[ip] = {"falhas": falhas, "bloqueado_ate": 0.0}
        restantes = MAX_TENTATIVAS_PIN - falhas
        return JSONResponse(
            status_code=401,
            content={
                "ok": False,
                "erro": f"PIN incorreto ({restantes} tentativa(s) restante(s)).",
            },
        )

    _TENTATIVAS_LOGIN.pop(ip, None)
    _gravar_cookie_sessao(response)
    return {"ok": True, "protegido": True, "autenticado": True}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(key=COOKIE_SESSAO, path="/", samesite="lax")
    return {"ok": True, "protegido": bool(_pin_ativo()), "autenticado": False}


@app.post("/api/auth/biometria/registrar/opcoes")
def biometria_registrar_opcoes(request: Request):
    _limpar_desafios_expirados()
    rp_id, origin = _extrair_rp_e_origin(request)
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", rp_id):
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "erro": "WebAuthn exige dominio valido (use http://localhost:8000 no PC ou https://...ts.net via tailscale serve no celular).",
            },
        )

    existentes = _listar_credenciais_biometria()
    excluir = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
        for cid, _pk, _sc, _nm in existentes
    ]

    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=WEBAUTHN_RP_NAME,
        user_id=WEBAUTHN_USER_ID,
        user_name=WEBAUTHN_USER_NAME,
        user_display_name="William",
        exclude_credentials=excluir,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    challenge_id = secrets.token_urlsafe(24)
    _WEBAUTHN_DESAFIOS[challenge_id] = {
        "tipo": "reg",
        "challenge": opts.challenge,
        "rp_id": rp_id,
        "origin": origin,
        "expira_em": time.time() + WEBAUTHN_TTL_SEGUNDOS,
    }
    return {
        "ok": True,
        "challenge_id": challenge_id,
        "options": json.loads(options_to_json(opts)),
    }


@app.post("/api/auth/biometria/registrar/verificar")
def biometria_registrar_verificar(pedido: PedidoWebAuthnVerificar, request: Request):
    _limpar_desafios_expirados()
    desafio = _WEBAUTHN_DESAFIOS.pop(pedido.challenge_id, None)
    if not desafio or desafio.get("tipo") != "reg":
        return JSONResponse(
            status_code=400,
            content={"ok": False, "erro": "Desafio expirado ou invalido. Tente ativar a biometria novamente."},
        )

    try:
        verificado = verify_registration_response(
            credential=pedido.credential,
            expected_challenge=desafio["challenge"],
            expected_rp_id=desafio["rp_id"],
            expected_origin=desafio["origin"],
            require_user_verification=False,
        )
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "erro": f"Falha na verificacao WebAuthn: {e}"})

    cred_id_b64 = bytes_to_base64url(verificado.credential_id)
    nome_disp = (pedido.nome or "").strip()[:80] or "Dispositivo"
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")

    with _conn_webauthn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO webauthn_credenciais
                (credential_id, public_key, sign_count, nome, criado_em)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cred_id_b64, bytes(verificado.credential_public_key), int(verificado.sign_count), nome_disp, agora),
        )

    return {
        "ok": True,
        "biometria_disponivel": True,
        "credencial": {"id": cred_id_b64[:12] + "...", "nome": nome_disp, "criado_em": agora},
    }


@app.post("/api/auth/biometria/login/opcoes")
def biometria_login_opcoes(request: Request):
    _limpar_desafios_expirados()
    credenciais = _listar_credenciais_biometria()
    if not credenciais:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "erro": "Nenhuma biometria cadastrada ainda."},
        )

    rp_id, origin = _extrair_rp_e_origin(request)
    permitidas = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
        for cid, _pk, _sc, _nm in credenciais
    ]
    opts = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=permitidas,
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    challenge_id = secrets.token_urlsafe(24)
    _WEBAUTHN_DESAFIOS[challenge_id] = {
        "tipo": "auth",
        "challenge": opts.challenge,
        "rp_id": rp_id,
        "origin": origin,
        "expira_em": time.time() + WEBAUTHN_TTL_SEGUNDOS,
    }
    return {
        "ok": True,
        "challenge_id": challenge_id,
        "options": json.loads(options_to_json(opts)),
    }


@app.post("/api/auth/biometria/login/verificar")
def biometria_login_verificar(pedido: PedidoWebAuthnVerificar, request: Request, response: Response):
    _limpar_desafios_expirados()
    desafio = _WEBAUTHN_DESAFIOS.pop(pedido.challenge_id, None)
    if not desafio or desafio.get("tipo") != "auth":
        return JSONResponse(
            status_code=400,
            content={"ok": False, "erro": "Desafio de biometria expirado ou invalido. Tente novamente."},
        )

    cred_id = str(pedido.credential.get("id") or pedido.credential.get("rawId") or "").strip()
    if not cred_id:
        return JSONResponse(status_code=400, content={"ok": False, "erro": "Credencial WebAuthn ausente."})

    with _conn_webauthn() as c:
        row = c.execute(
            "SELECT public_key, sign_count FROM webauthn_credenciais WHERE credential_id = ?",
            (cred_id,),
        ).fetchone()
    if row is None:
        return JSONResponse(status_code=401, content={"ok": False, "erro": "Biometria nao reconhecida neste servidor."})

    public_key, sign_count = bytes(row[0]), int(row[1])
    try:
        verificado = verify_authentication_response(
            credential=pedido.credential,
            expected_challenge=desafio["challenge"],
            expected_rp_id=desafio["rp_id"],
            expected_origin=desafio["origin"],
            credential_public_key=public_key,
            credential_current_sign_count=sign_count,
            require_user_verification=False,
        )
    except Exception as e:
        return JSONResponse(status_code=401, content={"ok": False, "erro": f"Falha ao validar biometria: {e}"})

    with _conn_webauthn() as c:
        c.execute(
            "UPDATE webauthn_credenciais SET sign_count = ? WHERE credential_id = ?",
            (int(verificado.new_sign_count), cred_id),
        )

    _TENTATIVAS_LOGIN.pop(_obter_ip_cliente(request), None)
    _gravar_cookie_sessao(response)
    return {"ok": True, "protegido": bool(_pin_ativo()), "autenticado": True}


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/athena-icon")
def athena_icon():
    png = BASE_DIR / "athena-icon.png"
    if png.is_file():
        return FileResponse(png, media_type="image/png")
    raise HTTPException(status_code=404, detail="Icone athena-icon.png nao encontrado.")


@app.get("/manifest.json")
def pwa_manifest():
    arq = BASE_DIR / "manifest.json"
    if arq.is_file():
        return FileResponse(arq, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="manifest.json nao encontrado.")


@app.get("/sw.js")
def pwa_service_worker():
    arq = BASE_DIR / "sw.js"
    if arq.is_file():
        return FileResponse(
            arq,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )
    raise HTTPException(status_code=404, detail="sw.js nao encontrado.")


@app.get("/api/telemetria")
def api_telemetria(limite: int = 15):
    return obter_resumo_telemetria(limite=limite)


@app.get("/api/config")
def ver_config():
    return {
        **CONFIG,
        "modelos_disponiveis": list(OPCOES_MODELO),
        "ollama": verificar_ollama_disponivel(),
    }


@app.post("/api/config")
def salvar_config(cfg: ConfigApp):
    if cfg.pasta_raiz is not None:
        res_pasta = _aplicar_pasta_raiz(Path(cfg.pasta_raiz))
        if not res_pasta["ok"]:
            return res_pasta
    aviso = ""
    ollama_status = verificar_ollama_disponivel(forcar=(cfg.modelo == MODELO_OLLAMA_ID))
    if cfg.modelo is not None:
        if cfg.modelo not in OPCOES_MODELO:
            return {"ok": False, "erro": f"Modelo invalido: {cfg.modelo}"}
        CONFIG["modelo"] = cfg.modelo
        if cfg.modelo == MODELO_OLLAMA_ID and not ollama_status.get("disponivel"):
            aviso = (
                "Ollama local esta desligado ou sem modelos instalados. "
                "Inicie o Ollama na sua maquina para conversar 100% offline."
            )
    return {
        "ok": True,
        "pasta_raiz": CONFIG["pasta_raiz"],
        "modelo": CONFIG["modelo"],
        "ollama": ollama_status,
        "aviso": aviso,
    }


@app.post("/api/escolher-pasta")
def escolher_pasta():
    script_tk = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "root.attributes('-topmost', True)\n"
        "root.update()\n"
        "pasta = filedialog.askdirectory(title='Escolha a pasta para os agentes', parent=root)\n"
        "root.destroy()\n"
        "print(pasta or '')\n"
    )
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script_tk],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelado": True, "erro": "Tempo limite esgotado na selecao de pasta."}
    except Exception as e:
        return {"ok": False, "erro": f"Nao foi possivel abrir o seletor de pastas: {e}"}

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"Codigo de saida {proc.returncode}"
        return {"ok": False, "erro": f"Erro ao abrir o Explorador de Pastas: {err}"}

    escolhida = (proc.stdout or "").strip()
    if not escolhida:
        return {"ok": False, "cancelado": True}

    return _aplicar_pasta_raiz(Path(escolhida))


def _enriquecer_mensagens_acoes_windows(mensagens: list[dict]) -> list[dict]:
    ids_win: list[int] = []
    for m in mensagens:
        meta = m.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("acoes_windows"), list):
            for ac in meta["acoes_windows"]:
                if isinstance(ac, dict) and ac.get("id") is not None:
                    with suppress(TypeError, ValueError):
                        ids_win.append(int(ac["id"]))
    if ids_win:
        status_map = obter_status_acoes_windows(ids_win)
        for m in mensagens:
            meta = m.get("meta")
            if isinstance(meta, dict) and isinstance(meta.get("acoes_windows"), list):
                atualizadas = []
                for ac in meta["acoes_windows"]:
                    if isinstance(ac, dict) and ac.get("id") in status_map:
                        novo = dict(status_map[int(ac["id"])])
                        if ac.get("auto_executada"):
                            novo["auto_executada"] = True
                        atualizadas.append(novo)
                    else:
                        atualizadas.append(ac)
                meta["acoes_windows"] = atualizadas
    return mensagens


class CriarConversaPayload(BaseModel):
    titulo: str = "Nova conversa"
    agente_alvo: str = "athena"
    id: str | None = None


class RenomearConversaPayload(BaseModel):
    titulo: str


@app.get("/api/agentes")
def api_listar_agentes():
    """Retorna todos os agentes disponiveis para chat direto (Athena + 7 especialistas)."""
    return {"ok": True, "agentes": listar_agentes_diretos_publico()}


@app.get("/api/conversas")
def api_listar_conversas(limite: int = 50):
    """Lista conversas recentes ordenadas por `atualizado_em DESC` (incluindo legadas)."""
    return {"ok": True, "conversas": listar_conversas(limite=limite)}


@app.post("/api/conversas")
def api_criar_conversa(payload: CriarConversaPayload | None = Body(default=None)):
    """Cria uma nova conversa vinculada a um `agente_alvo` (`athena` ou especialista direto)."""
    agente = normalizar_agente_alvo(payload.agente_alvo if payload else "athena")
    titulo = (payload.titulo if payload else "Nova conversa").strip() or "Nova conversa"
    cid_raw = (payload.id if payload and payload.id else None)
    cid = _normalizar_conversa_id(cid_raw) if cid_raw else None
    conv = criar_conversa(conversa_id=cid, titulo=titulo, agente_alvo=agente)
    return {"ok": True, "conversa": conv}


@app.get("/api/conversas/{conversa_id}")
def api_obter_conversa(conversa_id: str, limite: int = 80):
    """Retorna metadados + mensagens de uma conversa especifica."""
    cid = _normalizar_conversa_id(conversa_id)
    conv = obter_conversa(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail=f"Conversa '{cid}' nao encontrada.")
    mensagens = _enriquecer_mensagens_acoes_windows(listar_mensagens(limite=limite, conversa_id=cid))
    return {"ok": True, "conversa": conv, "mensagens": mensagens, "fatos": listar_fatos()}


@app.patch("/api/conversas/{conversa_id}")
def api_renomear_conversa(conversa_id: str, payload: RenomearConversaPayload):
    """Renomeia o titulo de uma conversa existente."""
    cid = _normalizar_conversa_id(conversa_id)
    titulo_limpo = (payload.titulo or "").strip()
    if not titulo_limpo:
        raise HTTPException(status_code=400, detail="Informe um titulo nao vazio.")
    conv = atualizar_titulo_conversa(cid, titulo_limpo)
    if conv is None:
        raise HTTPException(status_code=404, detail=f"Conversa '{cid}' nao encontrada.")
    return {"ok": True, "conversa": conv}


@app.delete("/api/conversas/{conversa_id}")
def api_deletar_conversa(conversa_id: str):
    """Cancela eventual tarefa ativa e apaga a conversa + mensagens + resumo compactado."""
    cid = _normalizar_conversa_id(conversa_id)
    reg = TAREFAS_ATIVAS.get(cid)
    if reg is not None and reg.get("status") == "running":
        t = reg.get("task")
        if t is not None and not t.done():
            reg["cancelada"] = True
            t.cancel()
    deletar_conversa(cid)
    return {"ok": True, "conversa_id": cid}


@app.get("/api/historico")
def ver_historico(conversa_id: str | None = None):
    cid = _normalizar_conversa_id(conversa_id) if conversa_id else None
    mensagens = _enriquecer_mensagens_acoes_windows(listar_mensagens(limite=40, conversa_id=cid))
    return {"mensagens": mensagens, "fatos": listar_fatos()}


@app.delete("/api/historico")
def resetar_chat(conversa_id: str | None = None):
    cid = _normalizar_conversa_id(conversa_id) if conversa_id else None
    limpar_mensagens(conversa_id=cid)
    limpar_resumos_conversa(cid)
    return {"ok": True}


@app.delete("/api/memorias/{chave}")
def remover_fato(chave: str):
    apagar_fato(chave)
    return {"ok": True}


@app.get("/api/memoria/temporal")
def api_memoria_temporal(limite: int = 50):
    """Retorna fatos vigentes na Memoria Temporal em Grafo e contagem de fatos historicos."""
    return resumo_memoria_temporal(limite_vigentes=limite)


@app.get("/api/painel/carteira")
def painel_carteira(forcar: bool = False):
    with conn_invest() as c:
        saldo = float(c.execute("SELECT saldo FROM carteira_caixa WHERE id = 1").fetchone()[0])
        pos_rows = c.execute("SELECT ticker, quantidade, preco_medio FROM carteira_posicoes ORDER BY ticker").fetchall()
        ops_rows = c.execute(
            "SELECT data_hora, tipo, ticker, quantidade, preco, total, motivo FROM carteira_operacoes ORDER BY id DESC LIMIT 20"
        ).fetchall()

    posicoes = []
    total_ativos = 0.0
    for sym, qtd, pm in pos_rows:
        _, hist = _normalizar_e_buscar_historico(sym, forcar=forcar)
        atual = float(hist["Close"].iloc[-1]) if (hist is not None and not hist.empty) else pm
        valor = atual * qtd
        total_ativos += valor
        lucro = (atual - pm) * qtd
        pct = ((atual / pm) - 1) * 100 if pm else 0.0
        posicoes.append({
            "ticker": sym,
            "quantidade": qtd,
            "preco_medio": round(pm, 2),
            "preco_atual": round(atual, 2),
            "total": round(valor, 2),
            "lucro": round(lucro, 2),
            "pct": round(pct, 2),
        })

    operacoes = [
        {"data_hora": r[0], "tipo": r[1], "ticker": r[2], "quantidade": r[3], "preco": round(r[4], 2), "total": round(r[5], 2), "motivo": r[6]}
        for r in ops_rows
    ]
    return {
        "caixa": round(saldo, 2),
        "em_ativos": round(total_ativos, 2),
        "patrimonio": round(saldo + total_ativos, 2),
        "posicoes": posicoes,
        "operacoes": operacoes,
    }


@app.get("/api/painel/carteira-real")
async def painel_carteira_real(forcar: bool = False):
    return await asyncio.to_thread(obter_painel_carteira_real, forcar=forcar)


@app.post("/api/painel/carteira-real/ativo")
def salvar_ativo_carteira_real(payload: AtivoRealPayload):
    return salvar_ativo_real_estruturado(
        tipo=payload.tipo,
        nome=payload.nome,
        valor_atual=payload.valor_atual,
        ticker=payload.ticker,
        taxa_info=payload.taxa_info,
        instituicao=payload.instituicao,
    )


@app.post("/api/painel/carteira-real/importar")
async def importar_extrato_carteira_real(arquivo: UploadFile = File(...)):
    nome = (arquivo.filename or "extrato.csv").strip()
    ext = Path(nome).suffix.lower()
    if ext not in (".csv", ".pdf", ".txt"):
        return {"ok": False, "erro": f"Formato nao permitido ({ext or 'desconhecido'}). Envie CSV ou PDF."}
    dados = await arquivo.read(MAX_UPLOAD_BYTES + 1)
    if len(dados) > MAX_UPLOAD_BYTES:
        return {"ok": False, "erro": "Arquivo excede o limite de 15 MB."}
    return await asyncio.to_thread(importar_extrato_mercado_pago, dados, nome)


@app.post("/api/painel/carteira-real/propostas/{proposta_id}/aprovar")
def aprovar_proposta_carteira_real(proposta_id: int):
    return responder_proposta(proposta_id, True)


@app.post("/api/painel/carteira-real/propostas/{proposta_id}/recusar")
def recusar_proposta_carteira_real(proposta_id: int):
    return responder_proposta(proposta_id, False)


@app.get("/api/painel/estudos")
def painel_estudos():
    hoje = date.today()
    with conn_estudos() as c:
        provas_rows = c.execute(
            "SELECT materia, titulo, data_iso, detalhes FROM provas ORDER BY data_iso ASC"
        ).fetchall()
        status_map = {
            (r[0], r[1]): r[2]
            for r in c.execute("SELECT materia, arquivo, status FROM progresso_aulas").fetchall()
        }

    provas = []
    for mat, tit, dt, det in provas_rows:
        dias = None
        with suppress(ValueError):
            dias = (date.fromisoformat(dt) - hoje).days
        provas.append({
            "materia": mat,
            "materia_nome": formatar_nome_materia(mat),
            "titulo": tit,
            "data_iso": dt,
            "dias": dias,
            "detalhes": det,
        })

    materias = []
    if PASTA_PDFS.exists():
        por_mat: dict[str, list[str]] = {}
        for pdf in sorted(PASTA_PDFS.rglob("*.pdf")):
            m = pdf.parent.name if pdf.parent != PASTA_PDFS else "geral"
            por_mat.setdefault(m, []).append(pdf.name)
        for m, arqs in por_mat.items():
            estudadas = sum(1 for a in arqs if status_map.get((m, a)) == "estudada")
            revisar = sum(1 for a in arqs if status_map.get((m, a)) == "revisar")
            pct = int((estudadas / len(arqs)) * 100) if arqs else 0
            materias.append({
                "materia": m,
                "nome": formatar_nome_materia(m),
                "total": len(arqs),
                "estudadas": estudadas,
                "revisar": revisar,
                "pct": pct,
            })

    return {"provas": provas, "materias": materias, "rag": obter_estatisticas_rag()}


@app.post("/api/rag/sincronizar")
async def sincronizar_rag():
    return await asyncio.to_thread(sincronizar_indice_vetorial)


@app.get("/api/mcp/servidores")
def ver_servidores_mcp():
    return {"servidores": listar_servidores_mcp()}


@app.get("/api/skills")
def ver_skills():
    return {"skills": listar_skills()}


@app.get("/api/obsidian/grafo")
async def ver_grafo_obsidian():
    """Retorna metricas do GraphRAG do Obsidian Vault (nos, arestas, top 5 hubs e notas orfas)."""
    return await asyncio.to_thread(estatisticas_grafo)


@app.get("/api/obsidian/grafo/visual")
async def ver_grafo_obsidian_visual(incluir_temporal: bool = True):
    """Exporta o grafo visual (Obsidian + #tags + Memoria Temporal) pronto para vis-network."""
    return await asyncio.to_thread(exportar_grafo_visual, incluir_temporal=incluir_temporal)


@app.get("/api/cache/semantico/stats")
def ver_stats_cache_semantico():
    """Retorna metricas do Cache Semantico local (`hits`, `misses`, `itens_ativos`)."""
    return estatisticas_cache_semantico()


@app.post("/api/cache/semantico/limpar")
def resetar_cache_semantico():
    """Limpa todas as entradas e contadores do Cache Semantico local."""
    removidos = limpar_cache_semantico()
    return {"ok": True, "removidos": removidos, "stats": estatisticas_cache_semantico()}


class PedidoLabExecutar(BaseModel):
    foco: str = "eficiencia e tokens"


_TAREFA_LAB_ATUAL: asyncio.Task | None = None


@app.get("/api/lab/propostas")
def api_lab_propostas():
    """Lista todas as propostas do Athena Lab, contagem de pendentes, status do ciclo/Sentinel e Antigravity CLI."""
    propostas = listar_propostas_lab()
    pendentes = sum(1 for p in propostas if p.get("status") in STATUS_ACIONAVEIS_LAB)
    return {
        "ok": True,
        "status": obter_status_lab(),
        "propostas": propostas,
        "pendentes": pendentes,
        "sentinel_ativo": sentinel_ativo(),
        "cli_antigravity": obter_status_cli_antigravity(),
    }


@app.post("/api/lab/sentinel/verificar-agora")
async def api_lab_sentinel_verificar_agora():
    """Aciona manualmente o Logfire Sentinel (0 tokens na consulta; dispara Lab em 2o plano se achar bug novo)."""
    return await verificar_erros_logfire_sentinel(forcar=True, background_lab=True)


@app.post("/api/lab/executar")
async def api_lab_executar(pedido: PedidoLabExecutar | None = Body(default=None)):
    """Dispara um ciclo R&D do Athena Lab em segundo plano sem travar a interface."""
    global _TAREFA_LAB_ATUAL
    if _TAREFA_LAB_ATUAL is not None and not _TAREFA_LAB_ATUAL.done():
        return {
            "ok": False,
            "erro": "Ja existe um ciclo do Athena Lab em andamento.",
            "status": obter_status_lab(),
        }
    foco = (pedido.foco if pedido is not None else "eficiencia e tokens").strip() or "eficiencia e tokens"
    _TAREFA_LAB_ATUAL = asyncio.create_task(rodar_ciclo_rd(foco))
    return {
        "ok": True,
        "iniciado": True,
        "foco": foco,
        "status": obter_status_lab(),
    }


@app.post("/api/lab/aprovar/{proposta_id}")
async def api_lab_aprovar(proposta_id: str):
    """Aprova e aplica localmente (merge limpo, sem git push) uma proposta validada do Athena Lab."""
    return await asyncio.to_thread(aprovar_proposta_lab, proposta_id)


@app.post("/api/lab/rejeitar/{proposta_id}")
async def api_lab_rejeitar(proposta_id: str):
    """Rejeita uma proposta do Athena Lab e remove sua branch/worktree temporario."""
    return await asyncio.to_thread(rejeitar_proposta_lab, proposta_id)


class PedidoVozFalar(BaseModel):
    texto: str
    voz: str = ""


@app.post("/api/voz/transcrever")
async def transcrever_voz(audio: UploadFile = File(...)):
    dados = await audio.read()
    if not dados:
        raise HTTPException(status_code=400, detail="Arquivo de audio vazio.")
    if len(dados) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio excede o limite de {MAX_AUDIO_BYTES // (1024 * 1024)} MB.",
        )

    ext = Path(audio.filename or "").suffix.lstrip(".")
    if not ext and audio.content_type:
        ext = audio.content_type

    try:
        texto = await asyncio.to_thread(transcrever_audio, dados, ext or "webm")
    except VozIndisponivelError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _logar_erro_execucao("voz/transcrever", "faster-whisper", exc)
        raise HTTPException(status_code=500, detail=f"Falha ao transcrever audio: {exc}") from exc

    return {"texto": texto}


@app.post("/api/voz/falar")
async def falar_voz(pedido: PedidoVozFalar):
    texto = (pedido.texto or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="Texto vazio.")

    voz_escolhida = (pedido.voz or "").strip() or VOZ_TTS_PADRAO
    try:
        res_tts = await sintetizar_fala(texto, voz=voz_escolhida)
    except VozIndisponivelError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _logar_erro_execucao("voz/falar", "tts", exc)
        raise HTTPException(status_code=502, detail=f"Falha ao sintetizar voz neural: {exc}") from exc

    if isinstance(res_tts, tuple):
        audio_bytes, media_type = res_tts
    else:
        audio_bytes, media_type = res_tts, "audio/mpeg"

    return Response(
        content=audio_bytes,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/painel/estudos/ulife")
def painel_estudos_ulife():
    return obter_dados_ulife_estruturados("tudo")


@app.post("/api/painel/estudos/ulife/sincronizar")
async def painel_estudos_ulife_sincronizar():
    return await sincronizar_ulife()


@app.get("/api/painel/faxina")
def painel_faxina():
    return listar_acoes_faxina()


@app.post("/api/painel/faxina/aprovar-todas")
async def aprovar_todas_faxina():
    return await asyncio.to_thread(aprovar_todas_pendentes)


@app.post("/api/painel/faxina/{acao_id}/aprovar")
def aprovar_acao_faxina(acao_id: int):
    return executar_acao_aprovada(acao_id)


@app.post("/api/painel/faxina/{acao_id}/recusar")
def recusar_acao_faxina(acao_id: int):
    return recusar_acao(acao_id)


@app.post("/api/painel/faxina/{acao_id}/desfazer")
def desfazer_acao_faxina(acao_id: int):
    return desfazer_movimentacao(acao_id)


class CriarRotinaDinamicaPayload(BaseModel):
    nome: str
    instrucao: str
    cron_expr: str
    agente_alvo: str = "athena"


class StatusRotinaDinamicaPayload(BaseModel):
    ativa: bool


class AprovarPastaPayload(BaseModel):
    caminho: str
    somente_leitura: bool = True


@app.get("/api/rotinas")
def ver_rotinas():
    return {"rotinas": listar_rotinas()}


@app.get("/api/rotinas/dinamicas")
def api_listar_rotinas_dinamicas():
    """Lista rotinas dinamicas criadas pelo chat/painel."""
    return {"ok": True, "rotinas": listar_rotinas_dinamicas()}


@app.post("/api/rotinas/dinamicas")
def api_criar_rotina_dinamica(payload: CriarRotinaDinamicaPayload):
    """Cria uma nova rotina dinamica agendada por cron de 5 campos."""
    try:
        rot = criar_rotina_dinamica(
            payload.nome,
            payload.instrucao,
            payload.cron_expr,
            agente_alvo=payload.agente_alvo,
        )
        return {"ok": True, "rotina": rot}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/rotinas/dinamicas/{rotina_id}")
def api_status_rotina_dinamica(rotina_id: str, payload: StatusRotinaDinamicaPayload):
    """Pausa ou reativa uma rotina dinamica."""
    res = alterar_status_rotina(rotina_id, payload.ativa)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=str(res.get("erro") or "Rotina nao encontrada."))
    return res


@app.delete("/api/rotinas/dinamicas/{rotina_id}")
def api_excluir_rotina_dinamica(rotina_id: str):
    """Exclui uma rotina dinamica e remove seu agendamento."""
    res = excluir_rotina_dinamica(rotina_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=str(res.get("erro") or "Rotina nao encontrada."))
    return res


@app.post("/api/rotinas/dinamicas/{rotina_id}/executar")
async def api_executar_rotina_dinamica(rotina_id: str):
    """Executa uma rotina dinamica imediatamente."""
    return await executar_rotina_dinamica(
        rotina_id,
        pasta_raiz=Path(CONFIG["pasta_raiz"]),
        modelo=CONFIG["modelo"],
    )


@app.post("/api/rotinas/{rotina_id}")
def editar_rotina(rotina_id: str, cfg: ConfigRotina):
    return atualizar_rotina(rotina_id, cfg.hora, cfg.minuto, cfg.ativa)


@app.post("/api/rotinas/{rotina_id}/executar")
async def rodar_rotina(rotina_id: str):
    return await executar_rotina_agora(
        rotina_id,
        pasta_raiz=Path(CONFIG["pasta_raiz"]),
        modelo=CONFIG["modelo"],
    )


@app.get("/api/auditoria/atividades")
def api_listar_atividades(agente: str | None = None, limite: int = 80):
    """Retorna a trilha unificada de auditoria (Activity) em ordem cronologica reversa."""
    return {"ok": True, "atividades": listar_atividades(limite=limite, agente=agente)}


@app.get("/api/permissoes/pastas")
def api_listar_pastas_aprovadas():
    """Lista pastas aprovadas na tabela `pastas_aprovadas`."""
    return {"ok": True, "pastas": listar_pastas_aprovadas()}


@app.post("/api/permissoes/pastas")
def api_aprovar_pasta(payload: AprovarPastaPayload):
    """Aprova uma pasta local (somente leitura por padrao)."""
    try:
        pasta = aprovar_pasta(payload.caminho, somente_leitura=payload.somente_leitura)
        return {"ok": True, "pasta": pasta}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/permissoes/pastas")
def api_revogar_pasta(
    caminho: str | None = None,
    payload: AprovarPastaPayload | None = Body(default=None),
):
    """Revoga a permissao de uma pasta (aceita query `?caminho=...` ou JSON body)."""
    alvo = (caminho or (payload.caminho if payload is not None else "") or "").strip()
    if not alvo:
        raise HTTPException(status_code=400, detail="Informe o caminho da pasta a revogar.")
    ok = revogar_pasta(alvo)
    return {"ok": ok, "caminho": alvo}


@app.get("/api/status-atual")
def status_execucao_atual():
    trilha_atual = list(ESTADO_EXECUCAO["trilha"])
    ultimo = trilha_atual[-1] if trilha_atual else None
    ultimo_passo = ""
    if ultimo:
        conteudo_curto = (ultimo.get("conteudo") or "").replace("\n", " ").strip()
        if len(conteudo_curto) > 95:
            conteudo_curto = conteudo_curto[:92] + "..."
        ultimo_passo = f"{ultimo.get('de', '')} -> {ultimo.get('para', '')}: {conteudo_curto}"
    return {
        "ativo": ESTADO_EXECUCAO["ativo"],
        "modelo": ESTADO_EXECUCAO["modelo"],
        "trilha": trilha_atual,
        "ultimo_passo": ultimo_passo,
    }


def _extrair_usage_tokens(resultado) -> tuple[int, int]:
    """Extrai (tokens_in, tokens_out) de um AgentRunResult/StreamedRunResult ou RunUsage.

    No Pydantic AI v2, `result.usage` e uma `@property` (enquanto em versoes v1 era metodo `result.usage()`),
    entao suportamos ambos com fallback seguro para `request_tokens`/`response_tokens` e `total_tokens`.
    """
    if resultado is None:
        return 0, 0
    try:
        usage_attr = getattr(resultado, "usage", resultado)
        u = (
            usage_attr
            if hasattr(usage_attr, "input_tokens") or hasattr(usage_attr, "request_tokens")
            else (usage_attr() if callable(usage_attr) else usage_attr)
        )
        if u is None:
            return 0, 0
        tokens_in = getattr(u, "input_tokens", None) or getattr(u, "request_tokens", 0) or 0
        tokens_out = getattr(u, "output_tokens", None) or getattr(u, "response_tokens", 0) or 0
        if not tokens_in and not tokens_out:
            total_tok = getattr(u, "total_tokens", 0) or 0
            if total_tok:
                tokens_out = total_tok
        return max(0, int(tokens_in)), max(0, int(tokens_out))
    except Exception:
        return 0, 0


def _extrair_cache_tokens(resultado) -> tuple[int, int]:
    """Extrai `(cache_read_tokens, cache_write_tokens)` do `RunUsage` para telemetria de Prompt Cache."""
    if resultado is None:
        return 0, 0
    try:
        usage_attr = getattr(resultado, "usage", resultado)
        u = (
            usage_attr
            if hasattr(usage_attr, "input_tokens") or hasattr(usage_attr, "cache_read_tokens")
            else (usage_attr() if callable(usage_attr) else usage_attr)
        )
        if u is None:
            return 0, 0
        details = getattr(u, "details", None)
        cache_read = getattr(u, "cache_read_tokens", None) or getattr(u, "cached_tokens", None) or 0
        if not cache_read and isinstance(details, dict):
            cache_read = details.get("cached_tokens") or details.get("cache_read_tokens") or 0
        cache_write = getattr(u, "cache_write_tokens", None) or 0
        if not cache_write and isinstance(details, dict):
            cache_write = details.get("cache_write_tokens") or 0
        return max(0, int(cache_read or 0)), max(0, int(cache_write or 0))
    except Exception:
        return 0, 0


def _extrair_modelo_real(resultado, fallback: str) -> str:
    """Le o `model_name` real da ultima ModelResponse em `result.all_messages()` (captura fallbacks do FallbackModel)."""
    if fallback == MODELO_OLLAMA_ID:
        return MODELO_OLLAMA_ID
    if resultado is None:
        return fallback
    try:
        all_msgs_fn = getattr(resultado, "all_messages", None)
        msgs = all_msgs_fn() if callable(all_msgs_fn) else None
        if isinstance(msgs, (list, tuple)):
            for msg in reversed(msgs):
                m_name = getattr(msg, "model_name", None)
                if m_name and isinstance(m_name, str) and m_name.strip():
                    limpo = m_name.strip()
                    if ":" in limpo:
                        limpo = limpo.split(":", 1)[1].strip()
                    if limpo:
                        return limpo
    except Exception:
        pass
    return fallback


def _extrair_agentes_trilha(trilha: list[dict]) -> str:
    """Resume quais agentes participaram da execucao a partir da trilha."""
    agentes: list[str] = ["Athena"]
    for item in trilha or []:
        for campo in ("de", "para"):
            nome = str(item.get(campo) or "").strip()
            if nome.startswith("Agente ") and nome not in agentes:
                agentes.append(nome)
    return ", ".join(agentes)


def _resolver_modelo_chat(
    modelo_req: str | None,
    mensagem: str,
    qtd_anexos: int = 0,
) -> tuple[str, list[str], str, bool]:
    """Resolve modelo pedido (`auto` ou manual) para `(modelo_config, fila_modelos, motivo, eh_auto)`.

    No modo Auto, `fila_modelos` ja vem filtrada pelo Circuit Breaker (`MODEL_COOLDOWN`).
    No modo manual, retorna apenas `[modelo_config]` sem cooldown nem fallback.
    """
    modelo_config = (modelo_req or CONFIG["modelo"] or MODELO_PADRAO).strip()
    if modelo_config not in OPCOES_MODELO:
        raise ValueError(f"Modelo invalido: {modelo_config}")
    if modelo_config == "auto":
        fila_modelos, motivo = obter_fila_modelos_auto(mensagem, qtd_anexos=qtd_anexos)
        return ("auto", fila_modelos, motivo, True)
    return (modelo_config, [modelo_config], "", False)


def _logar_erro_execucao(
    origem: str,
    modelo: str,
    exc: BaseException,
    *,
    conversa_id: str = "",
) -> None:
    """Registra erro estruturado com traceback real via structlog para nunca mascarar falhas de MCP/tool/schema."""
    logger.error(
        "erro_execucao",
        origem=origem,
        modelo=modelo,
        conversa_id=conversa_id or None,
        tipo_erro=type(exc).__name__,
        erro=str(exc),
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def _deve_tentar_fallback_run_apos_stream(exc: BaseException) -> bool:
    """So faz fallback `run_stream -> run` em incompatibilidade de streaming, nunca em erro HTTP/MCP/tool/schema."""
    if isinstance(exc, (ModelHTTPError, ToolFailedError, httpx.HTTPError)):
        return False
    if hasattr(exc, "status_code"):
        return False
    texto = f"{type(exc).__name__}: {exc}".lower()
    bloqueadores = (
        "mcp",
        "invalid_argument",
        "function_declarations",
        "schema",
        "unauthorized",
        "forbidden",
        "authentication",
        "permission_denied",
        "tool",
    )
    return not any(b in texto for b in bloqueadores)


def _formatar_erro_usuario(exc: BaseException) -> str:
    """Mantem a mensagem real e clara (incluindo tipo da excecao quando util)."""
    msg = str(exc).strip()
    tipo = type(exc).__name__
    if not msg:
        return tipo
    if tipo in ("RuntimeError", "ValueError") or msg.startswith(f"{tipo}:"):
        return msg
    return f"{tipo}: {msg}"


async def _executar_orquestrador_auto_ou_manual(
    entrada_modelo,
    ctx: ContextoMissao,
    fila_modelos: list[str],
    eh_auto: bool,
    agente_obj=orquestrador,
):
    """Executa `agente_obj.run(...)` com Circuit Breaker + timeout no modo Auto, ou forca o modelo direto no modo manual."""
    if not eh_auto:
        modelo_forcado = fila_modelos[0]
        ctx.modelo = modelo_forcado
        ESTADO_EXECUCAO["modelo"] = modelo_forcado
        res = await agente_obj.run(
            entrada_modelo,
            deps=ctx,
            model=obter_modelo(modelo_forcado, usar_fallback=False),
        )
        return res, _extrair_modelo_real(res, modelo_forcado)

    ultimo_erro: Exception | None = None
    erros_tentativas: list[str] = []
    for idx, mid in enumerate(fila_modelos):
        ctx.modelo = mid
        ESTADO_EXECUCAO["modelo"] = mid
        modelo_direto = resolver_modelo_execucao(mid, usar_fallback=False)
        rot_atual = ROTULOS_MODELO.get(mid, mid)

        try:
            async with asyncio.timeout(TIMEOUT_TENTATIVA_AUTO_SEGUNDOS):
                res = await agente_obj.run(
                    entrada_modelo,
                    deps=ctx,
                    model=modelo_direto,
                    retries=0,
                )
            return res, _extrair_modelo_real(res, mid)
        except Exception as exc:
            ultimo_erro = exc
            elegivel, _cd, motivo_err = registrar_cooldown_modelo(mid, exc)
            if not elegivel:
                _logar_erro_execucao("chat-auto", mid, exc)
                raise
            erros_tentativas.append(f"{rot_atual}: {motivo_err or exc}")
            if idx + 1 < len(fila_modelos):
                prox = fila_modelos[idx + 1]
                rot_prox = ROTULOS_MODELO.get(prox, prox)
                logger.warning(
                    "roteador_auto_fallback",
                    origem="chat",
                    modelo=mid,
                    proximo_modelo=prox,
                    motivo=motivo_err,
                    erro=str(exc),
                )
                ctx.registrar(
                    "Athena",
                    "Roteador",
                    "delegacao",
                    f"{rot_atual} {motivo_err}, alternando para {rot_prox}...",
                )
                continue
            break
    raise RuntimeError(
        f"Todos os modelos disponiveis no modo Auto falharam ({'; '.join(erros_tentativas)})."
    ) from ultimo_erro


async def _montar_prompt_com_compactacao(mensagem_atual: str, conversa_id: str = "principal") -> str:
    """Usa compactacao inteligente quando a conversa passa de 16 msgs, preservando as 8 recentes intactas."""
    historico_total = listar_mensagens(limite=80, conversa_id=conversa_id)
    resumo, recentes = await preparar_historico_compactado(conversa_id, historico_total)
    # Em conversas curtas (sem compactacao), mantem a janela enxuta das ultimas 5 mensagens
    msgs_para_prompt = recentes if resumo else recentes[-5:]
    return montar_contexto_prompt(
        mensagem_atual,
        resumo_compactado=resumo,
        historico_override=msgs_para_prompt,
    )


@app.post("/api/chat")
async def conversar(pedido: PedidoChat):
    try:
        modelo_config, fila_modelos, motivo_auto, eh_auto = _resolver_modelo_chat(
            pedido.modelo, pedido.mensagem, qtd_anexos=0
        )
    except ValueError as e:
        return {"ok": False, "erro": str(e), "trilha": [], "fatos": listar_fatos()}

    modelo_inicial = fila_modelos[0]
    CONFIG["modelo"] = modelo_config
    prompt_completo = await _montar_prompt_com_compactacao(pedido.mensagem, CONVERSA_ID_PADRAO)
    salvar_mensagem("user", pedido.mensagem)

    ctx = ContextoMissao(
        pasta_raiz=Path(CONFIG["pasta_raiz"]),
        modelo=modelo_inicial,
        usar_fallback=eh_auto,
        mensagem_atual=pedido.mensagem,
    )
    ESTADO_EXECUCAO["ativo"] = True
    ESTADO_EXECUCAO["modelo"] = modelo_inicial
    ESTADO_EXECUCAO["trilha"] = ctx.trilha

    ctx.registrar("Voce", f"Athena ({modelo_inicial})", "delegacao", pedido.mensagem)
    if eh_auto:
        ctx.registrar("Athena", "Roteador", "delegacao", f"Auto escolheu {modelo_inicial} ({motivo_auto})")
    t0 = time.perf_counter()
    try:
        res_fp = await asyncio.to_thread(tentar_fast_path, pedido.mensagem)
        if res_fp is not None:
            duracao_ms = max(1, int((time.perf_counter() - t0) * 1000))
            rota_fp = str(res_fp.get("rota") or "local")
            modelo_fp = str(res_fp.get("modelo") or MODELO_FAST_PATH)
            ESTADO_EXECUCAO["modelo"] = modelo_fp
            acao_win_fp = res_fp.get("acao_windows")
            if isinstance(acao_win_fp, dict):
                ctx.emitir_permissao_windows(acao_win_fp)
            ctx.registrar("Athena", modelo_fp, "ferramenta", f"{rota_fp} ({duracao_ms} ms · 0 tokens)")
            resposta_final = str(res_fp.get("resposta") or "").strip()
            registrar_telemetria(
                modelo=modelo_fp,
                duracao_ms=duracao_ms,
                tokens_in=0,
                tokens_out=0,
                agentes_usados=modelo_fp,
                sucesso=True,
            )
            meta_msg = {
                "modelo_usado": modelo_fp,
                "modo_auto": False,
                "motivo_modelo": f"Fast-Path: {rota_fp}",
                "duracao_ms": duracao_ms,
                "tokens_in": 0,
                "tokens_out": 0,
                "fast_path": True,
                "rota_fast_path": rota_fp,
                "imagens": [],
                "acoes_windows": list(ctx.acoes_windows),
            }
            salvar_mensagem("bot", resposta_final, ctx.trilha, meta=meta_msg)
            logger.info(
                "fast_path_hit",
                conversa_id=CONVERSA_ID_PADRAO,
                rota=rota_fp,
                latencia_ms=duracao_ms,
                tokens=0,
            )
            return {
                "ok": True,
                "resposta": resposta_final,
                "modelo": modelo_config,
                "modelo_usado": modelo_fp,
                "modo_auto": False,
                "motivo_modelo": f"Fast-Path: {rota_fp}",
                "fast_path": True,
                "rota_fast_path": rota_fp,
                "duracao_ms": duracao_ms,
                "tokens_in": 0,
                "tokens_out": 0,
                "imagens": [],
                "acoes_windows": list(ctx.acoes_windows),
                "trilha": ctx.trilha,
                "fatos": listar_fatos(),
                "telemetria": {"duracao_ms": duracao_ms, "tokens_in": 0, "tokens_out": 0},
            }

        res_cache = await asyncio.to_thread(consultar_cache_semantico, pedido.mensagem)
        if res_cache is not None:
            duracao_ms = max(1, int((time.perf_counter() - t0) * 1000))
            modelo_cache = str(res_cache.get("modelo") or MODELO_CACHE_SEMANTICO)
            similaridade = float(res_cache.get("similaridade") or 0.0)
            ESTADO_EXECUCAO["modelo"] = modelo_cache
            ctx.registrar("Athena", modelo_cache, "ferramenta", f"hit sim={similaridade:.3f} ({duracao_ms} ms · 0 tokens)")
            resposta_final = str(res_cache.get("resposta") or "").strip()
            registrar_telemetria(
                modelo=modelo_cache,
                duracao_ms=duracao_ms,
                tokens_in=0,
                tokens_out=0,
                agentes_usados=modelo_cache,
                sucesso=True,
            )
            meta_msg = {
                "modelo_usado": modelo_cache,
                "modo_auto": False,
                "motivo_modelo": f"Cache Semântico (sim={similaridade:.2f})",
                "duracao_ms": duracao_ms,
                "tokens_in": 0,
                "tokens_out": 0,
                "semantic_cache": True,
                "similaridade": similaridade,
                "imagens": [],
                "acoes_windows": [],
            }
            salvar_mensagem("bot", resposta_final, ctx.trilha, meta=meta_msg)
            logger.info(
                "semantic_cache_hit",
                conversa_id=CONVERSA_ID_PADRAO,
                similaridade=similaridade,
                latencia_ms=duracao_ms,
                tokens=0,
            )
            return {
                "ok": True,
                "resposta": resposta_final,
                "modelo": modelo_config,
                "modelo_usado": modelo_cache,
                "modo_auto": False,
                "motivo_modelo": f"Cache Semântico (sim={similaridade:.2f})",
                "semantic_cache": True,
                "similaridade": similaridade,
                "duracao_ms": duracao_ms,
                "tokens_in": 0,
                "tokens_out": 0,
                "imagens": [],
                "acoes_windows": [],
                "trilha": ctx.trilha,
                "fatos": listar_fatos(),
                "telemetria": {"duracao_ms": duracao_ms, "tokens_in": 0, "tokens_out": 0},
            }

        res, modelo_executado = await _executar_orquestrador_auto_ou_manual(
            prompt_completo, ctx, fila_modelos, eh_auto
        )
        duracao_ms = max(1, int((time.perf_counter() - t0) * 1000))
        tokens_in, tokens_out = _extrair_usage_tokens(res)
        cache_read_tokens, cache_write_tokens = _extrair_cache_tokens(res)
        cache_hit_pct = round((cache_read_tokens / tokens_in) * 100.0, 1) if tokens_in > 0 else 0.0
        imagens_geradas = _extrair_e_salvar_imagens_geradas(res)
        resposta_limpa = sanitizar_resposta_ollama_offline(res.output or "", modelo_executado)
        resposta_final = _anexar_imagens_no_markdown(resposta_limpa, imagens_geradas)
        logger.info(
            "chat_concluido",
            conversa_id=CONVERSA_ID_PADRAO,
            modelo=modelo_executado,
            modo_auto=eh_auto,
            latencia_ms=duracao_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_hit_pct=cache_hit_pct,
        )
        registrar_telemetria(
            modelo=modelo_executado,
            duracao_ms=duracao_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            agentes_usados=_extrair_agentes_trilha(ctx.trilha),
            sucesso=True,
        )
        meta_msg = {
            "modelo_usado": modelo_executado,
            "modo_auto": eh_auto,
            "motivo_modelo": motivo_auto,
            "duracao_ms": duracao_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "imagens": imagens_geradas,
            "acoes_windows": list(ctx.acoes_windows),
        }
        salvar_mensagem("bot", resposta_final, ctx.trilha, meta=meta_msg)
        agentes_turno = _extrair_agentes_trilha(ctx.trilha)
        if not ctx.acoes_windows and "Agente Faxineiro" not in agentes_turno and not imagens_geradas:
            with suppress(Exception):
                await asyncio.to_thread(salvar_cache_semantico, pedido.mensagem, resposta_final)
        agendar_extracao_memorias(pedido.mensagem, resposta_final)
        return {
            "ok": True,
            "resposta": resposta_final,
            "modelo": modelo_config,
            "modelo_usado": modelo_executado,
            "modo_auto": eh_auto,
            "motivo_modelo": motivo_auto,
            "duracao_ms": duracao_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "imagens": imagens_geradas,
            "acoes_windows": list(ctx.acoes_windows),
            "trilha": ctx.trilha,
            "fatos": listar_fatos(),
            "telemetria": {"duracao_ms": duracao_ms, "tokens_in": tokens_in, "tokens_out": tokens_out},
        }
    except Exception as e:
        _logar_erro_execucao("api/chat", ctx.modelo or modelo_inicial, e)
        duracao_ms = max(1, int((time.perf_counter() - t0) * 1000))
        with suppress(Exception):
            registrar_telemetria(
                modelo=ctx.modelo or modelo_inicial,
                duracao_ms=duracao_ms,
                agentes_usados=_extrair_agentes_trilha(ctx.trilha),
                sucesso=False,
            )
        return {"ok": False, "erro": _formatar_erro_usuario(e), "modelo": modelo_config, "modelo_usado": ctx.modelo or modelo_inicial, "trilha": ctx.trilha, "fatos": listar_fatos()}
    finally:
        ESTADO_EXECUCAO["ativo"] = False


MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB por arquivo individual (ex.: extratos)
MAX_UPLOAD_ARQUIVOS = 6
MAX_UPLOAD_SOMA_BYTES = 18 * 1024 * 1024  # 18 MB soma total inline (abaixo do teto de 20 MB do Gemini)
TIPOS_UPLOAD_PERMITIDOS = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "text/html",
    "text/plain",
    "text/markdown",
})
EXT_PARA_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
PASTA_UPLOADS = BASE_DIR / "sandbox_files" / "uploads"
PASTA_IMAGENS_GERADAS = BASE_DIR / "sandbox_files" / "imagens"
MIME_PARA_EXT_IMAGEM = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
EXT_PARA_MIME_IMAGEM = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _sanitizar_nome_upload(nome: str) -> str:
    base = Path(nome or "anexo").name
    limpo = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", base).strip(" .")
    return (limpo or "anexo")[:120]


def _extrair_e_salvar_imagens_geradas(resultado) -> list[dict]:
    """Extrai imagens geradas (`BinaryImage`/`BinaryContent` em ToolReturnPart, BuiltinToolReturnPart ou FilePart)
    das mensagens da execucao e salva em `sandbox_files/imagens/`, retornando metadados + URL `/api/arquivos/imagens/...`.
    """
    if resultado is None:
        return []
    try:
        all_msgs_fn = getattr(resultado, "all_messages", None)
        msgs = all_msgs_fn() if callable(all_msgs_fn) else None
    except Exception:
        msgs = None
    if not isinstance(msgs, (list, tuple)):
        return []

    itens_binarios: list[tuple[bytes, str]] = []

    def _coletar_obj(obj) -> None:
        if obj is None:
            return
        if isinstance(obj, BinaryContent):
            mime = (getattr(obj, "media_type", "") or "").split(";")[0].strip().lower()
            data = getattr(obj, "data", None)
            if mime.startswith("image/") and isinstance(data, (bytes, bytearray)) and len(data) > 0:
                itens_binarios.append((bytes(data), mime))
            return
        if isinstance(obj, (list, tuple)):
            for sub in obj:
                _coletar_obj(sub)
            return
        if isinstance(obj, dict):
            for sub in obj.values():
                _coletar_obj(sub)
            return
        # ToolReturn / FilePart / wrappers com .content ou .return_value
        for attr in ("content", "return_value"):
            sub = getattr(obj, attr, None)
            if sub is not None and sub is not obj:
                _coletar_obj(sub)

    for msg in msgs:
        partes = getattr(msg, "parts", None) or []
        for parte in partes:
            part_kind = str(getattr(parte, "part_kind", "") or "").lower()
            # Nunca confundir anexos enviados pelo usuario com imagens geradas pela Athena
            if part_kind == "user-prompt":
                continue
            if part_kind in ("tool-return", "builtin-tool-return", "file") or (
                hasattr(parte, "content") and isinstance(getattr(parte, "content", None), BinaryContent)
            ):
                _coletar_obj(getattr(parte, "content", None))

    if not itens_binarios:
        return []

    PASTA_IMAGENS_GERADAS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    vistos: set[str] = set()
    salvas: list[dict] = []

    for idx, (dados, mime) in enumerate(itens_binarios, start=1):
        digest = hashlib.sha256(dados).hexdigest()
        if digest in vistos:
            continue
        vistos.add(digest)
        ext = MIME_PARA_EXT_IMAGEM.get(mime, ".png")
        nome_arq = f"img_{ts}_{idx}_{digest[:8]}{ext}"
        destino = PASTA_IMAGENS_GERADAS / nome_arq
        destino.write_bytes(dados)
        salvas.append({
            "arquivo": nome_arq,
            "url": f"/api/arquivos/imagens/{nome_arq}",
            "caminho": f"sandbox_files/imagens/{nome_arq}",
            "media_type": mime,
        })

    return salvas


def _anexar_imagens_no_markdown(texto: str, imagens: list[dict]) -> str:
    """Garante que toda imagem gerada apareca inline em Markdown na bolha do chat (e persista no historico)."""
    if not imagens:
        return texto
    base = (texto or "").strip()
    blocos: list[str] = []
    for idx, img in enumerate(imagens, start=1):
        url = str(img.get("url") or "").strip()
        if not url or url in base:
            continue
        alt = f"Imagem gerada {idx}" if len(imagens) > 1 else "Imagem gerada"
        blocos.append(f"![{alt}]({url})")
    if not blocos:
        return base
    return (base + "\n\n" + "\n\n".join(blocos)).strip() if base else "\n\n".join(blocos)


@app.get("/api/arquivos/imagens/{nome_arquivo}")
def servir_imagem_gerada(nome_arquivo: str):
    """Serve imagens geradas pela capability `ImageGeneration` salvas em `sandbox_files/imagens/`."""
    nome_seguro = Path(nome_arquivo or "").name
    if not nome_seguro or nome_seguro != nome_arquivo or "/" in nome_arquivo or "\\" in nome_arquivo:
        raise HTTPException(status_code=400, detail="Nome de arquivo invalido.")
    base_res = PASTA_IMAGENS_GERADAS.resolve()
    alvo = (base_res / nome_seguro).resolve()
    if base_res not in alvo.parents or not alvo.is_file():
        raise HTTPException(status_code=404, detail="Imagem nao encontrada.")
    ext = alvo.suffix.lower()
    mime = EXT_PARA_MIME_IMAGEM.get(ext)
    if not mime:
        raise HTTPException(status_code=403, detail="Formato de arquivo nao permitido.")
    return FileResponse(alvo, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/arquivos/prints/{nome_arquivo}")
def servir_print_tela(nome_arquivo: str):
    """Serve capturas de tela autorizadas salvas em `sandbox_files/prints/`."""
    nome_seguro = Path(nome_arquivo or "").name
    if not nome_seguro or nome_seguro != nome_arquivo or "/" in nome_arquivo or "\\" in nome_arquivo:
        raise HTTPException(status_code=400, detail="Nome de arquivo invalido.")
    base_res = PASTA_PRINTS.resolve()
    alvo = (base_res / nome_seguro).resolve()
    if base_res not in alvo.parents or not alvo.is_file():
        raise HTTPException(status_code=404, detail="Captura de tela nao encontrada.")
    ext = alvo.suffix.lower()
    mime = EXT_PARA_MIME_IMAGEM.get(ext)
    if not mime:
        raise HTTPException(status_code=403, detail="Formato de arquivo nao permitido.")
    return FileResponse(alvo, media_type=mime, headers={"Cache-Control": "private, max-age=300"})


@app.post("/api/chat-multimodal")
async def conversar_multimodal(
    mensagem: str = Form(""),
    modelo: str | None = Form(None),
    conversa_id: str = Form("principal"),
    agente_alvo: str = Form("athena"),
    arquivos: list[UploadFile] = File(default=[]),
    arquivo: UploadFile | None = File(None),
):
    texto_usuario = (mensagem or "").strip()
    agente_norm = normalizar_agente_alvo(agente_alvo)
    lista_arquivos = [a for a in (arquivos or []) if a is not None and bool((a.filename or "").strip())]
    if arquivo is not None and bool((arquivo.filename or "").strip()):
        lista_arquivos.append(arquivo)

    try:
        modelo_config, fila_modelos, motivo_auto, eh_auto = _resolver_modelo_chat(
            modelo, texto_usuario, qtd_anexos=len(lista_arquivos)
        )
    except ValueError as e:
        return {"ok": False, "erro": str(e), "trilha": [], "fatos": listar_fatos()}

    modelo_real = fila_modelos[0]
    cid = _normalizar_conversa_id(conversa_id)

    if not texto_usuario and not lista_arquivos:
        return {
            "ok": False,
            "erro": "Envie uma mensagem ou anexe ate 6 fotos/PDFs.",
            "modelo": modelo_config,
            "modelo_usado": modelo_real,
            "trilha": [],
            "fatos": listar_fatos(),
        }

    erro_up, entrada_modelo, msg_historico, nomes_exibicao, caminhos_salvos_rel = (
        await _preparar_uploads_stream(texto_usuario, lista_arquivos, modelo_real, conversa_id=cid)
    )
    if erro_up is not None:
        return erro_up

    CONFIG["modelo"] = modelo_config
    existente = TAREFAS_ATIVAS.get(cid)
    if existente is not None and existente.get("status") == "running":
        t_exist = existente.get("task")
        if t_exist is not None and not t_exist.done():
            return {
                "ok": False,
                "erro": "Ja existe uma resposta em andamento nesta conversa. Aguarde concluir ou clique em Parar.",
                "modelo": modelo_config,
                "conversa_id": cid,
                "trilha": [],
                "fatos": listar_fatos(),
            }

    salvar_mensagem("user", msg_historico, conversa_id=cid, agente_alvo=agente_norm)

    registro, erro_conflito = _iniciar_tarefa_chat_background(
        conversa_id=cid,
        entrada_modelo=entrada_modelo,
        msg_historico=msg_historico,
        mensagem_atual=texto_usuario,
        modelo_config=modelo_config,
        fila_modelos=fila_modelos,
        motivo_auto=motivo_auto,
        eh_auto=eh_auto,
        nomes_exibicao=nomes_exibicao,
        caminhos_salvos_rel=caminhos_salvos_rel,
        usar_stream=False,
        agente_alvo=agente_norm,
    )
    if erro_conflito is not None or registro is None:
        return {
            "ok": False,
            "erro": erro_conflito or "Ja existe uma tarefa ativa nesta conversa.",
            "modelo": modelo_config,
            "conversa_id": cid,
            "trilha": [],
            "fatos": listar_fatos(),
        }

    # Aguarda via asyncio.shield para que desconexao HTTP NUNCA cancele a task em segundo plano
    await asyncio.shield(registro["task"])
    res_final = registro.get("resultado_final")
    if isinstance(res_final, dict):
        return res_final
    return {
        "ok": False,
        "erro": "Falha inesperada na tarefa em segundo plano.",
        "modelo": modelo_config,
        "conversa_id": cid,
        "trilha": [],
        "fatos": listar_fatos(),
    }


def _sse_evento(evento: str, dados: dict) -> str:
    return f"event: {evento}\ndata: {json.dumps(dados, ensure_ascii=False)}\n\n"


def _formatar_passo_trilha(item: dict) -> str:
    conteudo_curto = (item.get("conteudo") or "").replace("\n", " ").strip()
    if len(conteudo_curto) > 95:
        conteudo_curto = conteudo_curto[:92] + "..."
    return f"{item.get('de', '')} -> {item.get('para', '')}: {conteudo_curto}"


async def _preparar_uploads_stream(
    texto_usuario: str,
    lista_arquivos: list,
    modelo_escolhido: str,
    conversa_id: str = "principal",
) -> tuple[dict | None, str | list, str, list[str], list[str]]:
    if not lista_arquivos:
        prompt_comp = await _montar_prompt_com_compactacao(texto_usuario, conversa_id)
        return None, prompt_comp, texto_usuario, [], []

    if len(lista_arquivos) > MAX_UPLOAD_ARQUIVOS:
        return (
            {
                "ok": False,
                "erro": f"Limite maximo de {MAX_UPLOAD_ARQUIVOS} arquivos por mensagem excedido ({len(lista_arquivos)} enviados).",
                "modelo": modelo_escolhido,
                "trilha": [],
                "fatos": listar_fatos(),
            },
            "",
            texto_usuario,
            [],
            [],
        )

    soma_bytes = 0
    conteudos_binarios: list[BinaryContent] = []
    blocos_pdfs_texto: list[str] = []
    detalhes_prompt: list[str] = []
    caminhos_salvos_rel: list[str] = []
    nomes_exibicao: list[str] = []
    PASTA_UPLOADS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    for idx, arq in enumerate(lista_arquivos, start=1):
        nome_original = _sanitizar_nome_upload(getattr(arq, "filename", None) or f"anexo_{idx}")
        ext = Path(nome_original).suffix.lower()
        ctype = (getattr(arq, "content_type", None) or "").split(";")[0].strip().lower()
        if ctype == "image/jpg":
            ctype = "image/jpeg"
        if not ctype or ctype == "application/octet-stream":
            ctype = EXT_PARA_MIME.get(ext, "")

        if ctype not in TIPOS_UPLOAD_PERMITIDOS:
            return (
                {
                    "ok": False,
                    "erro": (
                        f"Tipo nao permitido em '{nome_original}' ({ctype or 'desconhecido'}). "
                        "Use JPG, PNG, WebP, PDF, DOCX, PPTX, XLSX, CSV, HTML, TXT ou MD."
                    ),
                    "modelo": modelo_escolhido,
                    "trilha": [],
                    "fatos": listar_fatos(),
                },
                "",
                texto_usuario,
                [],
                [],
            )

        restante = (MAX_UPLOAD_SOMA_BYTES - soma_bytes) + 1
        dados = await arq.read(max(1, restante))
        if not dados:
            return (
                {
                    "ok": False,
                    "erro": f"O arquivo '{nome_original}' esta vazio.",
                    "modelo": modelo_escolhido,
                    "trilha": [],
                    "fatos": listar_fatos(),
                },
                "",
                texto_usuario,
                [],
                [],
            )
        soma_bytes += len(dados)
        if soma_bytes > MAX_UPLOAD_SOMA_BYTES:
            return (
                {
                    "ok": False,
                    "erro": "A soma dos arquivos excede o limite maximo de 18 MB por mensagem.",
                    "modelo": modelo_escolhido,
                    "trilha": [],
                    "fatos": listar_fatos(),
                },
                "",
                texto_usuario,
                [],
                [],
            )

        nome_disco = f"{ts}_{idx}_{nome_original}"
        (PASTA_UPLOADS / nome_disco).write_bytes(dados)
        caminhos_salvos_rel.append(f"sandbox_files/uploads/{nome_disco}")
        nomes_exibicao.append(nome_original)

        if ctype.startswith("image/"):
            dados_envio, mime_envio, stats_img = await asyncio.to_thread(
                otimizar_imagem_para_llm,
                dados,
                nome_original,
            )
            logger.info(
                "midia_otimizada",
                tipo="imagem",
                arquivo=nome_original,
                bytes_antes=stats_img["bytes_antes"],
                bytes_depois=stats_img["bytes_depois"],
                reducao_pct=stats_img["reducao_pct"],
                dimensoes=list(stats_img["dimensoes"]),
            )
            detalhes_prompt.append(
                f"{nome_original} ({mime_envio}, {stats_img['dimensoes'][0]}x{stats_img['dimensoes'][1]}, "
                f"copia em /workspace/uploads/{nome_disco})"
            )
            conteudos_binarios.append(BinaryContent(data=dados_envio, media_type=mime_envio))
        elif ctype == "application/pdf" or ext in EXTENSOES_DOCUMENTO_SUPORTADAS:
            res_doc = await asyncio.to_thread(extrair_documento_para_markdown, dados, nome_original)
            if res_doc.get("modo") == "texto":
                conteudo_doc = str(res_doc.get("conteudo") or "")
                paginas_doc = int(res_doc.get("paginas") or 0)
                motor_doc = str(res_doc.get("motor") or "texto")
                bytes_depois_doc = len(conteudo_doc.encode("utf-8"))
                reducao_doc = (
                    round(((len(dados) - bytes_depois_doc) / len(dados)) * 100.0, 1)
                    if len(dados) > 0 and bytes_depois_doc <= len(dados)
                    else 0.0
                )
                logger.info(
                    "midia_otimizada",
                    tipo="pdf_texto" if ext == ".pdf" else "documento_markdown",
                    motor=motor_doc,
                    arquivo=nome_original,
                    paginas=paginas_doc,
                    bytes_antes=len(dados),
                    bytes_depois=bytes_depois_doc,
                    reducao_pct=reducao_doc,
                )
                if ext == ".pdf":
                    detalhes_prompt.append(
                        f"{nome_original} (PDF digital extraido via {motor_doc}, {paginas_doc} pagina(s), "
                        f"copia em /workspace/uploads/{nome_disco})"
                    )
                    blocos_pdfs_texto.append(
                        f"[TEXTO EXTRAIDO DO PDF '{nome_original}' ({paginas_doc} pagina(s), motor: {motor_doc})]\n{conteudo_doc}"
                    )
                else:
                    detalhes_prompt.append(
                        f"{nome_original} (documento convertido em Markdown via {motor_doc}, "
                        f"copia em /workspace/uploads/{nome_disco})"
                    )
                    blocos_pdfs_texto.append(
                        f"[DOCUMENTO CONVERTIDO PARA MARKDOWN '{nome_original}' (motor: {motor_doc})]\n{conteudo_doc}"
                    )
            elif res_doc.get("modo") == "binario":
                paginas_pdf = int(res_doc.get("paginas") or 0)
                logger.info(
                    "midia_otimizada",
                    tipo="pdf_binario",
                    arquivo=nome_original,
                    paginas=paginas_pdf,
                    bytes_antes=len(dados),
                    bytes_depois=len(dados),
                    reducao_pct=0.0,
                )
                detalhes_prompt.append(
                    f"{nome_original} (application/pdf escaneado, {paginas_pdf} pagina(s), "
                    f"copia em /workspace/uploads/{nome_disco})"
                )
                conteudos_binarios.append(BinaryContent(data=res_doc["dados"], media_type="application/pdf"))
            else:
                return (
                    {
                        "ok": False,
                        "erro": str(res_doc.get("erro") or f"Nao foi possivel processar '{nome_original}'."),
                        "modelo": modelo_escolhido,
                        "trilha": [],
                        "fatos": listar_fatos(),
                    },
                    "",
                    texto_usuario,
                    [],
                    [],
                )
        else:
            detalhes_prompt.append(f"{nome_original} ({ctype}, copia em /workspace/uploads/{nome_disco})")
            conteudos_binarios.append(BinaryContent(data=dados, media_type=ctype))

    if len(nomes_exibicao) == 1:
        prefixo_hist = f"[Anexo: {nomes_exibicao[0]}]"
    else:
        prefixo_hist = f"[Anexos ({len(nomes_exibicao)}): {', '.join(nomes_exibicao)}]"
    msg_historico = f"{prefixo_hist} {texto_usuario}".strip()

    resumo_anexos = "; ".join(detalhes_prompt)
    texto_prompt = (
        f"{texto_usuario}\n\n[Arquivos anexados pelo usuario ({len(nomes_exibicao)}): {resumo_anexos}]"
        if texto_usuario
        else f"Analise detalhadamente os {len(nomes_exibicao)} arquivo(s) anexado(s) ({resumo_anexos}) e responda em portugues do Brasil."
    )
    if blocos_pdfs_texto:
        texto_prompt = texto_prompt + "\n\n" + "\n\n".join(blocos_pdfs_texto)
    prompt_completo = await _montar_prompt_com_compactacao(texto_prompt, conversa_id)
    entrada_final: str | list = [prompt_completo, *conteudos_binarios] if conteudos_binarios else prompt_completo
    return None, entrada_final, msg_historico, nomes_exibicao, caminhos_salvos_rel


# ==============================================================================
# EXECUTOR EM SEGUNDO PLANO (INDEPENDENTE DA CONEXAO HTTP) + REATTACH / CANCELAR
# ==============================================================================

TAREFAS_ATIVAS: dict[str, dict] = {}
TAREFA_TTL_POS_CONCLUIDA_SEGUNDOS = 300  # 5 minutos em memoria apos finalizar
CONVERSA_ID_PADRAO = "principal"
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _normalizar_conversa_id(conversa_id: str | None) -> str:
    limpo = re.sub(r"[^A-Za-z0-9_-]+", "", str(conversa_id or "").strip())[:64]
    return limpo or CONVERSA_ID_PADRAO


def _limpar_tarefas_expiradas() -> None:
    agora = time.time()
    expiradas = [
        cid
        for cid, reg in list(TAREFAS_ATIVAS.items())
        if reg.get("status") != "running"
        and reg.get("finalizada_em", 0.0) > 0
        and (agora - float(reg["finalizada_em"])) >= TAREFA_TTL_POS_CONCLUIDA_SEGUNDOS
    ]
    for cid in expiradas:
        TAREFAS_ATIVAS.pop(cid, None)


def _publicar_evento_tarefa(registro: dict, tipo: str, payload: dict) -> None:
    if tipo == "chunk":
        delta = str(payload.get("delta") or "")
        if delta:
            payload = {"tipo": "chunk", **payload}
            registro["partes_texto"].append(delta)
            registro["texto_parcial"] = str(registro.get("texto_parcial") or "") + delta
    elif tipo == "reset_stream":
        payload = {"tipo": "reset_stream", **payload}
        registro["partes_texto"].clear()
        registro["texto_parcial"] = ""
    elif tipo == "status":
        registro["ultimo_status"] = payload
    registro["eventos"].append((tipo, payload))
    for q in list(registro["subscribers"]):
        with suppress(Exception):
            q.put_nowait((tipo, payload))


def _encerrar_subscribers_tarefa(registro: dict) -> None:
    for q in list(registro["subscribers"]):
        with suppress(Exception):
            q.put_nowait(None)


def _agendar_limpeza_tarefa(conversa_id: str, registro: dict) -> None:
    try:
        loop = asyncio.get_running_loop()

        def _remover_se_mesma() -> None:
            if TAREFAS_ATIVAS.get(conversa_id) is registro and registro.get("status") != "running":
                TAREFAS_ATIVAS.pop(conversa_id, None)

        loop.call_later(TAREFA_TTL_POS_CONCLUIDA_SEGUNDOS, _remover_se_mesma)
    except Exception:
        pass


def _iniciar_tarefa_chat_background(
    *,
    conversa_id: str,
    entrada_modelo,
    msg_historico: str,
    mensagem_atual: str = "",
    modelo_config: str,
    fila_modelos: list[str],
    motivo_auto: str,
    eh_auto: bool,
    nomes_exibicao: list[str],
    caminhos_salvos_rel: list[str],
    usar_stream: bool = True,
    agente_alvo: str = "athena",
) -> tuple[dict | None, str | None]:
    """Cria uma `asyncio.Task` independente da conexao HTTP e registra em `TAREFAS_ATIVAS[conversa_id]`."""
    _limpar_tarefas_expiradas()
    cid = _normalizar_conversa_id(conversa_id)
    existente = TAREFAS_ATIVAS.get(cid)
    if existente is not None and existente.get("status") == "running":
        t_exist = existente.get("task")
        if t_exist is not None and not t_exist.done():
            return None, "Ja existe uma resposta em andamento nesta conversa. Aguarde concluir ou clique em Parar."

    info_agente = obter_agente_direto(agente_alvo)
    agente_id = str(info_agente["id"])
    agente_nome = str(info_agente["nome"])
    agente_exec = info_agente["agente"]
    eh_agente_direto = agente_id != "athena"
    rotulo_agente_destino = f"Agente {agente_nome}" if eh_agente_direto else "Athena"

    modelo_real = fila_modelos[0]
    loop = asyncio.get_running_loop()
    modelo_atual_ref = [modelo_real]

    registro: dict = {
        "conversa_id": cid,
        "agente_alvo": agente_id,
        "agente_nome": agente_nome,
        "task": None,
        "status": "running",
        "eventos": [],
        "subscribers": [],
        "criado_em": time.time(),
        "finalizada_em": 0.0,
        "ultimo_status": None,
        "partes_texto": [],
        "texto_parcial": "",
        "ttft_ms": None,
        "resultado_final": None,
        "cancelada": False,
        "modelo_config": modelo_config,
        "modo_auto": eh_auto,
        "motivo_modelo": motivo_auto,
    }
    TAREFAS_ATIVAS[cid] = registro

    def _enfileirar_status(item: dict) -> None:
        payload = {
            "conversa_id": cid,
            "agente_alvo": agente_id,
            "agente_nome": agente_nome,
            "passo": _formatar_passo_trilha(item),
            "item": item,
            "trilha": list(ctx.trilha),
            "modelo": modelo_config,
            "modelo_usado": modelo_atual_ref[0],
            "modo_auto": eh_auto,
            "motivo_modelo": motivo_auto,
        }
        loop.call_soon_threadsafe(_publicar_evento_tarefa, registro, "status", payload)

    def _enfileirar_permissao_windows(acao: dict) -> None:
        payload = {
            "tipo": "permissao_windows",
            "conversa_id": cid,
            "acao": acao,
        }
        loop.call_soon_threadsafe(_publicar_evento_tarefa, registro, "permissao_windows", payload)

    def _enfileirar_take_the_wheel(dados: dict) -> None:
        payload = {
            "tipo": "take_the_wheel",
            "conversa_id": cid,
            **dados,
        }
        loop.call_soon_threadsafe(_publicar_evento_tarefa, registro, "take_the_wheel", payload)

    ctx = ContextoMissao(
        pasta_raiz=Path(CONFIG["pasta_raiz"]),
        modelo=modelo_real,
        usar_fallback=eh_auto,
        mensagem_atual=(mensagem_atual or msg_historico),
        on_evento=_enfileirar_status,
        on_permissao_windows=_enfileirar_permissao_windows,
        on_take_the_wheel=_enfileirar_take_the_wheel,
    )
    ESTADO_EXECUCAO["ativo"] = True
    ESTADO_EXECUCAO["modelo"] = modelo_real
    ESTADO_EXECUCAO["trilha"] = ctx.trilha

    ctx.registrar("Voce", f"{rotulo_agente_destino} ({modelo_real})", "delegacao", msg_historico)
    if eh_auto:
        ctx.registrar(rotulo_agente_destino, "Roteador", "delegacao", f"Auto escolheu {modelo_real} ({motivo_auto})")

    async def _executar_em_segundo_plano() -> None:
        partes_texto: list[str] = []
        texto_final = ""
        tokens_in = 0
        tokens_out = 0
        ttft_ms: int | None = None
        modelo_executado = modelo_real
        resultado_final_obj = None
        t0 = time.perf_counter()

        def _novo_estado_stream_ollama(modelo_id: str) -> dict:
            eh_ol = modelo_id == MODELO_OLLAMA_ID or "ollama" in (modelo_id or "").lower()
            return {"eh_ollama": eh_ol, "retendo": eh_ol, "buffer": ""}

        def _processar_delta_stream(delta: str, estado_st: dict) -> None:
            nonlocal ttft_ms
            if not delta:
                return
            if ttft_ms is None:
                ttft_ms = max(1, int((time.perf_counter() - t0) * 1000))
                registro["ttft_ms"] = ttft_ms
            partes_texto.append(delta)
            if not estado_st["retendo"]:
                _publicar_evento_tarefa(registro, "chunk", {"tipo": "chunk", "delta": delta})
                return
            estado_st["buffer"] += delta
            inicio_limpo = estado_st["buffer"].lstrip()
            if inicio_limpo and not inicio_limpo.startswith(("{", "[", "`")):
                estado_st["retendo"] = False
                _publicar_evento_tarefa(registro, "chunk", {"tipo": "chunk", "delta": estado_st["buffer"]})
                estado_st["buffer"] = ""

        def _finalizar_stream_ollama(modelo_exec: str, estado_st: dict, fallback_output: str = "") -> str:
            bruto = "".join(partes_texto).strip() or (fallback_output or "").strip()
            limpo = sanitizar_resposta_ollama_offline(bruto, modelo_exec)
            if estado_st["retendo"] and limpo:
                _publicar_evento_tarefa(registro, "chunk", {"tipo": "chunk", "delta": limpo})
                estado_st["buffer"] = ""
                estado_st["retendo"] = False
            elif not partes_texto and limpo:
                _publicar_evento_tarefa(registro, "chunk", {"tipo": "chunk", "delta": limpo})
            return limpo

        async def _consumir_stream_eventos(modelo_obj, estado_st: dict, *, retries_override: int | None = None):
            """Itera `agente_exec.run_stream_events(...)` emitindo cada TextPartDelta sem quebrar tool-calls."""
            kwargs_ev: dict = {"deps": ctx, "model": modelo_obj}
            if retries_override is not None:
                kwargs_ev["retries"] = retries_override
            resultado_ev = None
            async with agente_exec.run_stream_events(entrada_modelo, **kwargs_ev) as eventos:
                async for evento in eventos:
                    if isinstance(evento, PartStartEvent):
                        if isinstance(evento.part, TextPart) and evento.part.content:
                            _processar_delta_stream(evento.part.content, estado_st)
                    elif isinstance(evento, PartDeltaEvent):
                        if isinstance(evento.delta, TextPartDelta) and evento.delta.content_delta:
                            _processar_delta_stream(evento.delta.content_delta, estado_st)
                    elif isinstance(evento, AgentRunResultEvent):
                        resultado_ev = evento.result
            return resultado_ev

        try:
            # Fast-Path deterministico e Cache Semantico apenas na Orquestradora geral (`athena`) sem anexos
            if not nomes_exibicao and not eh_agente_direto:
                res_fp = await asyncio.to_thread(tentar_fast_path, ctx.mensagem_atual)
                if res_fp is not None:
                    rota_fp = str(res_fp.get("rota") or "local")
                    modelo_executado = str(res_fp.get("modelo") or MODELO_FAST_PATH)
                    modelo_atual_ref[0] = modelo_executado
                    ESTADO_EXECUCAO["modelo"] = modelo_executado
                    acao_win_fp = res_fp.get("acao_windows")
                    if isinstance(acao_win_fp, dict):
                        ctx.emitir_permissao_windows(acao_win_fp)
                    duracao_ms = max(1, int((time.perf_counter() - t0) * 1000))
                    ctx.registrar("Athena", modelo_executado, "ferramenta", f"{rota_fp} ({duracao_ms} ms · 0 tokens)")
                    texto_final = str(res_fp.get("resposta") or "").strip()
                    if texto_final:
                        _publicar_evento_tarefa(registro, "chunk", {"delta": texto_final})
                    with suppress(Exception):
                        registrar_telemetria(
                            modelo=modelo_executado,
                            duracao_ms=duracao_ms,
                            tokens_in=0,
                            tokens_out=0,
                            agentes_usados=modelo_executado,
                            sucesso=True,
                        )
                    meta_fp = {
                        "modelo_usado": modelo_executado,
                        "modo_auto": False,
                        "motivo_modelo": f"Fast-Path: {rota_fp}",
                        "duracao_ms": duracao_ms,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "fast_path": True,
                        "rota_fast_path": rota_fp,
                        "imagens": [],
                        "acoes_windows": list(ctx.acoes_windows),
                    }
                    salvar_mensagem("bot", texto_final, ctx.trilha, meta=meta_fp, conversa_id=cid, agente_alvo=agente_id)
                    conv_atual_fp = obter_conversa(cid)
                    payload_done_fp = {
                        "ok": True,
                        "conversa_id": cid,
                        "conversa_titulo": conv_atual_fp["titulo"] if conv_atual_fp else "",
                        "agente_alvo": agente_id,
                        "agente_nome": agente_nome,
                        "resposta": texto_final,
                        "modelo": modelo_config,
                        "modelo_usado": modelo_executado,
                        "modo_auto": False,
                        "motivo_modelo": f"Fast-Path: {rota_fp}",
                        "fast_path": True,
                        "rota_fast_path": rota_fp,
                        "duracao_ms": duracao_ms,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "imagens": [],
                        "acoes_windows": list(ctx.acoes_windows),
                        "anexo": "",
                        "anexos": [],
                        "caminho_upload": "",
                        "caminhos_upload": [],
                        "trilha": list(ctx.trilha),
                        "fatos": listar_fatos(),
                        "telemetria": {"duracao_ms": duracao_ms, "tokens_in": 0, "tokens_out": 0},
                    }
                    registro["resultado_final"] = payload_done_fp
                    registro["status"] = "done"
                    logger.info(
                        "fast_path_hit",
                        conversa_id=cid,
                        rota=rota_fp,
                        latencia_ms=duracao_ms,
                        tokens=0,
                    )
                    _publicar_evento_tarefa(registro, "done", payload_done_fp)
                    return

                res_cache = await asyncio.to_thread(consultar_cache_semantico, ctx.mensagem_atual)
                if res_cache is not None:
                    modelo_executado = str(res_cache.get("modelo") or MODELO_CACHE_SEMANTICO)
                    similaridade = float(res_cache.get("similaridade") or 0.0)
                    modelo_atual_ref[0] = modelo_executado
                    ESTADO_EXECUCAO["modelo"] = modelo_executado
                    duracao_ms = max(1, int((time.perf_counter() - t0) * 1000))
                    ctx.registrar(
                        "Athena",
                        modelo_executado,
                        "ferramenta",
                        f"hit sim={similaridade:.3f} ({duracao_ms} ms · 0 tokens)",
                    )
                    texto_final = str(res_cache.get("resposta") or "").strip()
                    if texto_final:
                        _publicar_evento_tarefa(registro, "chunk", {"delta": texto_final})
                    with suppress(Exception):
                        registrar_telemetria(
                            modelo=modelo_executado,
                            duracao_ms=duracao_ms,
                            tokens_in=0,
                            tokens_out=0,
                            agentes_usados=modelo_executado,
                            sucesso=True,
                        )
                    meta_sc = {
                        "modelo_usado": modelo_executado,
                        "modo_auto": False,
                        "motivo_modelo": f"Cache Semântico (sim={similaridade:.2f})",
                        "duracao_ms": duracao_ms,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "semantic_cache": True,
                        "similaridade": similaridade,
                        "imagens": [],
                        "acoes_windows": [],
                    }
                    salvar_mensagem("bot", texto_final, ctx.trilha, meta=meta_sc, conversa_id=cid, agente_alvo=agente_id)
                    conv_atual_sc = obter_conversa(cid)
                    payload_done_sc = {
                        "ok": True,
                        "conversa_id": cid,
                        "conversa_titulo": conv_atual_sc["titulo"] if conv_atual_sc else "",
                        "agente_alvo": agente_id,
                        "agente_nome": agente_nome,
                        "resposta": texto_final,
                        "modelo": modelo_config,
                        "modelo_usado": modelo_executado,
                        "modo_auto": False,
                        "motivo_modelo": f"Cache Semântico (sim={similaridade:.2f})",
                        "semantic_cache": True,
                        "similaridade": similaridade,
                        "duracao_ms": duracao_ms,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "imagens": [],
                        "acoes_windows": [],
                        "anexo": "",
                        "anexos": [],
                        "caminho_upload": "",
                        "caminhos_upload": [],
                        "trilha": list(ctx.trilha),
                        "fatos": listar_fatos(),
                        "telemetria": {"duracao_ms": duracao_ms, "tokens_in": 0, "tokens_out": 0},
                    }
                    registro["resultado_final"] = payload_done_sc
                    registro["status"] = "done"
                    logger.info(
                        "semantic_cache_hit",
                        conversa_id=cid,
                        similaridade=similaridade,
                        latencia_ms=duracao_ms,
                        tokens=0,
                    )
                    _publicar_evento_tarefa(registro, "done", payload_done_sc)
                    return

            if not usar_stream:
                res, modelo_executado = await _executar_orquestrador_auto_ou_manual(
                    entrada_modelo, ctx, fila_modelos, eh_auto, agente_obj=agente_exec
                )
                resultado_final_obj = res
                modelo_atual_ref[0] = modelo_executado
                tokens_in, tokens_out = _extrair_usage_tokens(res)
                texto_final = sanitizar_resposta_ollama_offline((res.output or "").strip(), modelo_executado)
                if texto_final:
                    _publicar_evento_tarefa(registro, "chunk", {"delta": texto_final})
            elif not eh_auto:
                # Modo manual forcado: streaming real via run_stream_events (suporta tools + deltas token-a-token)
                modelo_obj = obter_modelo(modelo_real, usar_fallback=False)
                estado_st = _novo_estado_stream_ollama(modelo_real)
                try:
                    stream_result = await _consumir_stream_eventos(modelo_obj, estado_st)
                    resultado_final_obj = stream_result
                    tokens_in, tokens_out = _extrair_usage_tokens(stream_result)
                    modelo_executado = _extrair_modelo_real(stream_result, modelo_real)
                    fallback_out = str(getattr(stream_result, "output", "") or "")
                    texto_final = _finalizar_stream_ollama(modelo_executado, estado_st, fallback_output=fallback_out)
                    if not texto_final:
                        res_fb = await agente_exec.run(
                            entrada_modelo,
                            deps=ctx,
                            model=modelo_obj,
                        )
                        resultado_final_obj = res_fb
                        fb_in, fb_out = _extrair_usage_tokens(res_fb)
                        tokens_in += fb_in
                        tokens_out += fb_out
                        modelo_executado = _extrair_modelo_real(res_fb, modelo_executado)
                        texto_final = sanitizar_resposta_ollama_offline((res_fb.output or "").strip(), modelo_executado)
                        if texto_final:
                            _publicar_evento_tarefa(registro, "chunk", {"tipo": "chunk", "delta": texto_final})
                except asyncio.CancelledError:
                    raise
                except Exception as exc_stream:
                    if not _deve_tentar_fallback_run_apos_stream(exc_stream):
                        raise exc_stream
                    if not partes_texto:
                        res_fb = await agente_exec.run(
                            entrada_modelo,
                            deps=ctx,
                            model=modelo_obj,
                        )
                        resultado_final_obj = res_fb
                        tokens_in, tokens_out = _extrair_usage_tokens(res_fb)
                        modelo_executado = _extrair_modelo_real(res_fb, modelo_real)
                        texto_final = sanitizar_resposta_ollama_offline((res_fb.output or "").strip(), modelo_executado)
                        if texto_final:
                            _publicar_evento_tarefa(registro, "chunk", {"tipo": "chunk", "delta": texto_final})
                    else:
                        raise exc_stream
            else:
                # Modo Auto: RetryOnModelErrors faz backoff 2s/4s/6s por request no mesmo modelo;
                # se o 503/429 persistir, emite reset_stream (se houve delta parcial) e alterna para o proximo modelo.
                erros_tentativas: list[str] = []
                ultimo_erro: Exception | None = None
                sucesso_auto = False

                for idx, mid in enumerate(fila_modelos):
                    modelo_atual_ref[0] = mid
                    ctx.modelo = mid
                    ESTADO_EXECUCAO["modelo"] = mid
                    modelo_executado = mid
                    modelo_direto = resolver_modelo_execucao(mid, usar_fallback=False)
                    rot_atual = ROTULOS_MODELO.get(mid, mid)

                    partes_texto.clear()
                    estado_st = _novo_estado_stream_ollama(mid)
                    stream_result = None
                    try:
                        async with asyncio.timeout(TIMEOUT_TENTATIVA_AUTO_SEGUNDOS):
                            stream_result = await _consumir_stream_eventos(
                                modelo_direto,
                                estado_st,
                                retries_override=0,
                            )
                        resultado_final_obj = stream_result
                        tokens_in, tokens_out = _extrair_usage_tokens(stream_result)
                        modelo_executado = _extrair_modelo_real(stream_result, mid)
                        fallback_out = str(getattr(stream_result, "output", "") or "")
                        texto_final = _finalizar_stream_ollama(modelo_executado, estado_st, fallback_output=fallback_out)
                        if not texto_final:
                            async with asyncio.timeout(TIMEOUT_TENTATIVA_AUTO_SEGUNDOS):
                                res_fb = await agente_exec.run(
                                    entrada_modelo,
                                    deps=ctx,
                                    model=modelo_direto,
                                    retries=0,
                                )
                            resultado_final_obj = res_fb
                            fb_in, fb_out = _extrair_usage_tokens(res_fb)
                            tokens_in += fb_in
                            tokens_out += fb_out
                            modelo_executado = _extrair_modelo_real(res_fb, mid)
                            texto_final = sanitizar_resposta_ollama_offline(
                                (res_fb.output or "").strip(), modelo_executado
                            )
                            if texto_final:
                                _publicar_evento_tarefa(registro, "chunk", {"tipo": "chunk", "delta": texto_final})
                        modelo_atual_ref[0] = modelo_executado
                        sucesso_auto = True
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc_tentativa:
                        texto_emitido = "".join(partes_texto).strip()
                        elegivel, _cd, motivo_err = registrar_cooldown_modelo(mid, exc_tentativa)

                        if not elegivel and not texto_emitido and _deve_tentar_fallback_run_apos_stream(exc_tentativa):
                            try:
                                async with asyncio.timeout(TIMEOUT_TENTATIVA_AUTO_SEGUNDOS):
                                    res_fb = await agente_exec.run(
                                        entrada_modelo,
                                        deps=ctx,
                                        model=modelo_direto,
                                        retries=0,
                                    )
                                resultado_final_obj = res_fb
                                tokens_in, tokens_out = _extrair_usage_tokens(res_fb)
                                modelo_executado = _extrair_modelo_real(res_fb, mid)
                                modelo_atual_ref[0] = modelo_executado
                                texto_final = sanitizar_resposta_ollama_offline(
                                    (res_fb.output or "").strip(), modelo_executado
                                )
                                if texto_final:
                                    _publicar_evento_tarefa(registro, "chunk", {"tipo": "chunk", "delta": texto_final})
                                sucesso_auto = True
                                break
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc_fb:
                                exc_tentativa = exc_fb
                                elegivel, _cd, motivo_err = registrar_cooldown_modelo(mid, exc_tentativa)

                        ultimo_erro = exc_tentativa
                        if not elegivel:
                            _logar_erro_execucao("chat-stream-auto", mid, exc_tentativa, conversa_id=cid)
                            raise exc_tentativa

                        erros_tentativas.append(f"{rot_atual}: {motivo_err or exc_tentativa}")

                        pode_alternar = (not texto_emitido) or (
                            eh_erro_503(exc_tentativa) and len(texto_emitido) < 300
                        )
                        if pode_alternar and idx + 1 < len(fila_modelos):
                            if texto_emitido or registro.get("texto_parcial"):
                                partes_texto.clear()
                                ttft_ms = None
                                registro["ttft_ms"] = None
                                _publicar_evento_tarefa(
                                    registro,
                                    "reset_stream",
                                    {"tipo": "reset_stream", "conversa_id": cid, "motivo": motivo_err},
                                )
                            prox = fila_modelos[idx + 1]
                            rot_prox = ROTULOS_MODELO.get(prox, prox)
                            modelo_atual_ref[0] = prox
                            logger.warning(
                                "roteador_auto_fallback",
                                origem="chat-stream",
                                conversa_id=cid,
                                modelo=mid,
                                proximo_modelo=prox,
                                motivo=motivo_err,
                                erro=str(exc_tentativa),
                            )
                            ctx.registrar(
                                "Athena",
                                "Roteador",
                                "delegacao",
                                f"{rot_atual} {motivo_err}, alternando para {rot_prox}...",
                            )
                            continue
                        if texto_emitido:
                            raise exc_tentativa
                        break

                if not sucesso_auto:
                    raise RuntimeError(
                        f"Todos os modelos disponiveis no modo Auto falharam ({'; '.join(erros_tentativas)})."
                    ) from ultimo_erro

            imagens_geradas = _extrair_e_salvar_imagens_geradas(resultado_final_obj)
            if imagens_geradas:
                texto_final = _anexar_imagens_no_markdown(texto_final, imagens_geradas)

            duracao_ms = max(1, int((time.perf_counter() - t0) * 1000))
            agentes_usados_tele = (
                f"Agente {agente_nome} (Direto)" if eh_agente_direto else _extrair_agentes_trilha(ctx.trilha)
            )
            with suppress(Exception):
                registrar_telemetria(
                    modelo=modelo_executado,
                    duracao_ms=duracao_ms,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    agentes_usados=agentes_usados_tele,
                    sucesso=True,
                )

            meta_msg = {
                "modelo_usado": modelo_executado,
                "modo_auto": eh_auto,
                "motivo_modelo": motivo_auto,
                "agente_alvo": agente_id,
                "agente_nome": agente_nome,
                "duracao_ms": duracao_ms,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "imagens": imagens_geradas,
                "acoes_windows": list(ctx.acoes_windows),
            }
            salvar_mensagem("bot", texto_final, ctx.trilha, meta=meta_msg, conversa_id=cid, agente_alvo=agente_id)
            conv_atual_done = obter_conversa(cid)

            payload_done = {
                "ok": True,
                "conversa_id": cid,
                "conversa_titulo": conv_atual_done["titulo"] if conv_atual_done else "",
                "agente_alvo": agente_id,
                "agente_nome": agente_nome,
                "resposta": texto_final,
                "modelo": modelo_config,
                "modelo_usado": modelo_executado,
                "modo_auto": eh_auto,
                "motivo_modelo": motivo_auto,
                "duracao_ms": duracao_ms,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "imagens": imagens_geradas,
                "acoes_windows": list(ctx.acoes_windows),
                "anexo": nomes_exibicao[0] if nomes_exibicao else "",
                "anexos": nomes_exibicao,
                "caminho_upload": caminhos_salvos_rel[0] if caminhos_salvos_rel else "",
                "caminhos_upload": caminhos_salvos_rel,
                "trilha": list(ctx.trilha),
                "fatos": listar_fatos(),
                "telemetria": {
                    "duracao_ms": duracao_ms,
                    "ttft_ms": ttft_ms,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                },
            }
            cache_read_tokens, cache_write_tokens = _extrair_cache_tokens(resultado_final_obj)
            cache_hit_pct = round((cache_read_tokens / tokens_in) * 100.0, 1) if tokens_in > 0 else 0.0
            registro["resultado_final"] = payload_done
            registro["status"] = "done"
            logger.info(
                "chat_concluido",
                conversa_id=cid,
                modelo=modelo_executado,
                modo_auto=eh_auto,
                latencia_ms=duracao_ms,
                ttft_ms=ttft_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cache_hit_pct=cache_hit_pct,
            )
            _publicar_evento_tarefa(registro, "done", payload_done)
            agentes_turno = _extrair_agentes_trilha(ctx.trilha)
            if (
                not eh_agente_direto
                and not nomes_exibicao
                and not ctx.acoes_windows
                and "Agente Faxineiro" not in agentes_turno
                and not imagens_geradas
            ):
                with suppress(Exception):
                    await asyncio.to_thread(salvar_cache_semantico, ctx.mensagem_atual, texto_final)
            agendar_extracao_memorias(msg_historico, texto_final)
        except asyncio.CancelledError:
            duracao_ms = max(1, int((time.perf_counter() - t0) * 1000))
            registro["cancelada"] = True
            registro["status"] = "error"
            logger.warning(
                "chat_cancelado",
                conversa_id=cid,
                modelo=modelo_executado,
                latencia_ms=duracao_ms,
            )
            with suppress(Exception):
                registrar_telemetria(
                    modelo=modelo_executado,
                    duracao_ms=duracao_ms,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    agentes_usados=_extrair_agentes_trilha(ctx.trilha),
                    sucesso=False,
                )
            payload_cancel = {
                "ok": False,
                "cancelada": True,
                "conversa_id": cid,
                "erro": "Geração interrompida pelo usuário.",
                "modelo": modelo_config,
                "modelo_usado": modelo_executado,
                "trilha": list(ctx.trilha),
                "fatos": listar_fatos(),
            }
            registro["resultado_final"] = payload_cancel
            _publicar_evento_tarefa(registro, "error", payload_cancel)
        except Exception as e:
            _logar_erro_execucao("chat-stream", modelo_executado, e)
            duracao_ms = max(1, int((time.perf_counter() - t0) * 1000))
            registro["status"] = "error"
            with suppress(Exception):
                registrar_telemetria(
                    modelo=modelo_executado,
                    duracao_ms=duracao_ms,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    agentes_usados=_extrair_agentes_trilha(ctx.trilha),
                    sucesso=False,
                )
            payload_err = {
                "ok": False,
                "conversa_id": cid,
                "erro": _formatar_erro_usuario(e),
                "modelo": modelo_config,
                "modelo_usado": modelo_executado,
                "trilha": list(ctx.trilha),
                "fatos": listar_fatos(),
            }
            registro["resultado_final"] = payload_err
            _publicar_evento_tarefa(registro, "error", payload_err)
        finally:
            ESTADO_EXECUCAO["ativo"] = any(
                r.get("status") == "running" for r in TAREFAS_ATIVAS.values() if r is not registro
            )
            registro["finalizada_em"] = time.time()
            _encerrar_subscribers_tarefa(registro)
            _agendar_limpeza_tarefa(cid, registro)

    registro["task"] = asyncio.create_task(_executar_em_segundo_plano())
    return registro, None


async def _assinar_tarefa_sse(registro: dict, *, replay_compacto: bool = False):
    """Streama eventos de uma tarefa em segundo plano para um cliente HTTP.

    Se o cliente desconectar (F5 / trocar de app), apenas remove sua fila de `subscribers` —
    NUNCA cancela `registro["task"]`!
    """
    fila_cliente: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
    ja_finalizada = registro.get("status") != "running"

    # Snapshot + inscricao sem yields no meio (atomico no event loop asyncio)
    eventos_iniciais: list[tuple[str, dict]] = []
    if replay_compacto:
        if registro.get("ultimo_status") is not None:
            eventos_iniciais.append(("status", registro["ultimo_status"]))
        for tipo_ev, payload_ev in registro.get("eventos") or []:
            if tipo_ev in ("permissao_windows", "take_the_wheel"):
                eventos_iniciais.append((tipo_ev, payload_ev))
        texto_acum = str(registro.get("texto_parcial") or "") or "".join(registro.get("partes_texto") or [])
        if texto_acum:
            eventos_iniciais.append(("snapshot", {"tipo": "snapshot", "texto": texto_acum}))
        if ja_finalizada:
            for tipo_ev, payload_ev in reversed(registro.get("eventos") or []):
                if tipo_ev in ("done", "error"):
                    eventos_iniciais.append((tipo_ev, payload_ev))
                    break
    else:
        eventos_iniciais = list(registro.get("eventos") or [])

    if not ja_finalizada:
        registro["subscribers"].append(fila_cliente)

    try:
        for tipo_ev, payload_ev in eventos_iniciais:
            yield _sse_evento(tipo_ev, payload_ev)

        if ja_finalizada:
            return

        while True:
            ev = await fila_cliente.get()
            if ev is None:
                break
            tipo_ev, payload_ev = ev
            yield _sse_evento(tipo_ev, payload_ev)
    finally:
        if fila_cliente in registro["subscribers"]:
            registro["subscribers"].remove(fila_cliente)


@app.post("/api/chat-stream")
async def conversar_stream(request: Request):
    """Streaming em tempo real via SSE com execucao desacoplada em segundo plano (`TAREFAS_ATIVAS`).

    Aceita tanto `application/json` (`{mensagem, modelo, conversa_id}`) quanto `multipart/form-data`
    com ate 6 anexos JPG/PNG/WebP/PDF (soma max. 18 MB).
    """
    ctype_req = (request.headers.get("content-type") or "").lower()
    texto_usuario = ""
    modelo_req: str | None = None
    conversa_id_req: str | None = None
    agente_alvo_req: str = "athena"
    lista_arquivos: list = []

    if "multipart/form-data" in ctype_req:
        form = await request.form()
        texto_usuario = str(form.get("mensagem") or "").strip()
        mod_val = form.get("modelo")
        modelo_req = str(mod_val).strip() if mod_val else None
        cid_val = form.get("conversa_id")
        conversa_id_req = str(cid_val).strip() if cid_val else None
        ag_val = form.get("agente_alvo")
        agente_alvo_req = str(ag_val).strip() if ag_val else "athena"
        for item in form.getlist("arquivos"):
            if hasattr(item, "read") and bool((getattr(item, "filename", "") or "").strip()):
                lista_arquivos.append(item)
        arq_single = form.get("arquivo")
        if hasattr(arq_single, "read") and bool((getattr(arq_single, "filename", "") or "").strip()):
            lista_arquivos.append(arq_single)
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            texto_usuario = str(body.get("mensagem") or "").strip()
            mod_val = body.get("modelo")
            modelo_req = str(mod_val).strip() if mod_val else None
            cid_val = body.get("conversa_id")
            conversa_id_req = str(cid_val).strip() if cid_val else None
            ag_val = body.get("agente_alvo")
            agente_alvo_req = str(ag_val).strip() if ag_val else "athena"

    cid = _normalizar_conversa_id(conversa_id_req)
    agente_norm = normalizar_agente_alvo(agente_alvo_req)

    def _stream_erro_imediato(payload_erro: dict) -> StreamingResponse:
        async def _gen_err():
            yield _sse_evento("error", payload_erro)

        return StreamingResponse(_gen_err(), media_type="text/event-stream", headers=SSE_HEADERS)

    try:
        modelo_config, fila_modelos, motivo_auto, eh_auto = _resolver_modelo_chat(
            modelo_req, texto_usuario, qtd_anexos=len(lista_arquivos)
        )
    except ValueError as e:
        return _stream_erro_imediato({
            "ok": False,
            "conversa_id": cid,
            "erro": str(e),
            "trilha": [],
            "fatos": listar_fatos(),
        })

    modelo_real = fila_modelos[0]

    if not texto_usuario and not lista_arquivos:
        return _stream_erro_imediato({
            "ok": False,
            "conversa_id": cid,
            "erro": "Envie uma mensagem ou anexe ate 6 fotos/PDFs.",
            "modelo": modelo_config,
            "modelo_usado": modelo_real,
            "trilha": [],
            "fatos": listar_fatos(),
        })

    erro_up, entrada_modelo, msg_historico, nomes_exibicao, caminhos_salvos_rel = (
        await _preparar_uploads_stream(texto_usuario, lista_arquivos, modelo_real, conversa_id=cid)
    )
    if erro_up is not None:
        return _stream_erro_imediato(erro_up)

    CONFIG["modelo"] = modelo_config
    existente = TAREFAS_ATIVAS.get(cid)
    if existente is not None and existente.get("status") == "running":
        t_exist = existente.get("task")
        if t_exist is not None and not t_exist.done():
            return _stream_erro_imediato({
                "ok": False,
                "erro": "Ja existe uma resposta em andamento nesta conversa. Aguarde concluir ou clique em Parar.",
                "modelo": modelo_config,
                "conversa_id": cid,
                "trilha": [],
                "fatos": listar_fatos(),
            })

    salvar_mensagem("user", msg_historico, conversa_id=cid, agente_alvo=agente_norm)

    registro, erro_conflito = _iniciar_tarefa_chat_background(
        conversa_id=cid,
        entrada_modelo=entrada_modelo,
        msg_historico=msg_historico,
        mensagem_atual=texto_usuario,
        modelo_config=modelo_config,
        fila_modelos=fila_modelos,
        motivo_auto=motivo_auto,
        eh_auto=eh_auto,
        nomes_exibicao=nomes_exibicao,
        caminhos_salvos_rel=caminhos_salvos_rel,
        usar_stream=True,
        agente_alvo=agente_norm,
    )
    if erro_conflito is not None or registro is None:
        return _stream_erro_imediato({
            "ok": False,
            "conversa_id": cid,
            "erro": erro_conflito or "Ja existe uma resposta em andamento nesta conversa.",
            "modelo": modelo_config,
            "trilha": [],
            "fatos": listar_fatos(),
        })

    return StreamingResponse(
        _assinar_tarefa_sse(registro, replay_compacto=False),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@app.get("/api/chat-reattach/{conversa_id}")
async def reconectar_chat_stream(conversa_id: str):
    """Reconecta via SSE a uma tarefa `running` apos F5 ou troca de app no celular."""
    _limpar_tarefas_expiradas()
    cid = _normalizar_conversa_id(conversa_id)
    registro = TAREFAS_ATIVAS.get(cid)
    t = registro.get("task") if registro else None

    if registro is None or registro.get("status") != "running" or t is None or t.done():
        async def _gen_idle():
            yield _sse_evento("idle", {"tipo": "idle", "conversa_id": cid})

        return StreamingResponse(_gen_idle(), media_type="text/event-stream", headers=SSE_HEADERS)

    return StreamingResponse(
        _assinar_tarefa_sse(registro, replay_compacto=True),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@app.post("/api/chat-cancelar/{conversa_id}")
async def cancelar_chat_em_andamento(conversa_id: str):
    """Cancela explicitamente uma tarefa em andamento quando o usuario clica em Parar."""
    _limpar_tarefas_expiradas()
    cid = _normalizar_conversa_id(conversa_id)
    registro = TAREFAS_ATIVAS.get(cid)
    if registro is None or registro.get("status") != "running":
        return {
            "ok": True,
            "cancelada": False,
            "conversa_id": cid,
            "mensagem": "Nenhuma tarefa ativa para cancelar.",
        }

    t = registro.get("task")
    if t is not None and not t.done():
        registro["cancelada"] = True
        t.cancel()
        return {
            "ok": True,
            "cancelada": True,
            "conversa_id": cid,
            "mensagem": "Cancelamento solicitado.",
        }

    return {
        "ok": True,
        "cancelada": False,
        "conversa_id": cid,
        "mensagem": "Tarefa ja finalizada.",
    }


class AprovarWindowsPayload(BaseModel):
    sempre_permitir: bool = False


@app.post("/api/windows/capturar-tela")
async def api_windows_capturar_tela():
    """Captura o monitor principal mediante clique explicito do usuario no botao de tela do chat."""
    try:
        dados = await asyncio.to_thread(capturar_tela_atual)
        return {"ok": True, **dados}
    except Exception as exc:
        _logar_erro_execucao("windows/capturar-tela", "mss", exc)
        return {"ok": False, "erro": f"Nao foi possivel capturar a tela: {exc}"}


@app.post("/api/windows/aprovar/{acao_id}")
async def api_windows_aprovar(
    acao_id: int,
    payload: AprovarWindowsPayload | None = Body(default=None),
):
    """Executa no Windows 11 uma acao previamente solicitada pela Athena (com opcao 'Sempre permitir' apenas para apps)."""
    sempre = bool(payload.sempre_permitir) if payload is not None else False
    return await asyncio.to_thread(aprovar_acao_windows, acao_id, sempre_permitir=sempre)


@app.post("/api/windows/recusar/{acao_id}")
def api_windows_recusar(acao_id: int):
    """Recusa uma solicitacao de abertura de app/URL/arquivo no Windows."""
    return recusar_acao_windows(acao_id)


@app.get("/api/windows/whitelist")
def api_windows_whitelist():
    """Lista os aplicativos liberados com 'Sempre permitir' no Windows."""
    return {"ok": True, "itens": listar_whitelist_windows()}


@app.delete("/api/windows/whitelist/{item}")
def api_windows_whitelist_remover(item: str):
    """Remove um aplicativo da whitelist 'Sempre permitir'."""
    return remover_whitelist_windows(item)


class IniciarGravacaoSkillPayload(BaseModel):
    nome_skill: str
    url_inicial: str
    descricao: str = ""


@app.post("/api/skills/gravar/iniciar")
async def api_teach_bot_iniciar(payload: IniciarGravacaoSkillPayload):
    """Inicia uma sessao controlada do 'Teach a Bot' para gravar passos e gerar SKILL.md."""
    try:
        return await iniciar_gravacao_skill(
            payload.nome_skill,
            payload.url_inicial,
            descricao=payload.descricao,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/skills/gravar/finalizar/{sessao_id}")
async def api_teach_bot_finalizar(sessao_id: str):
    """Finaliza a gravacao do 'Teach a Bot', gera skills/<slug>/SKILL.md e recarrega o catalogo."""
    try:
        return await finalizar_gravacao_skill(sessao_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/skills/gravar/status")
def api_teach_bot_status():
    """Retorna o status da gravacao ativa do 'Teach a Bot' (se houver)."""
    return obter_status_gravacao()


@app.post("/api/navegador/retomar/{sessao_id}")
def api_navegador_retomar(sessao_id: str):
    """Devolve o controle ao agente apos intervencao humana ('Take the wheel')."""
    return retomar_controle_agente(sessao_id)


@app.post("/api/navegador/cancelar/{sessao_id}")
def api_navegador_cancelar(sessao_id: str):
    """Cancela a espera de intervencao humana ('Take the wheel') e aborta a navegacao."""
    return cancelar_controle_agente(sessao_id)
