import base64
import contextlib
import html
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from cache_ttl import cache_ttl
from eficiencia import compactar_saida_tool
from guardrails import sanitizar_conteudo_externo

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/tasks",
]

LIMITE_LIXEIRA_POR_CHAMADA = 50
LIMITE_LOTE_GMAIL = 100

FUSO_NOME = "America/Sao_Paulo"
MSG_CONECTAR = (
    "Conta Google ainda nao conectada (token.json nao encontrado ou expirado). "
    "Rode `uv run python conectar_google.py` no terminal para autorizar via OAuth no navegador."
)

MIME_AMIGAVEL = {
    "application/vnd.google-apps.document": "Google Docs",
    "application/vnd.google-apps.spreadsheet": "Google Sheets",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.folder": "Pasta",
    "application/vnd.google-apps.form": "Google Forms",
    "application/pdf": "PDF",
}


class GoogleNaoConectadoError(RuntimeError):
    """Indica que o token OAuth ainda nao foi gerado ou precisa ser renovado."""


def _fuso_sp():
    try:
        return ZoneInfo(FUSO_NOME)
    except Exception:
        return timezone(timedelta(hours=-3))


def conectar_conta_google() -> Credentials:
    """Executa o fluxo OAuth local (navegador) e salva/renova token.json."""
    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds and not creds.has_scopes(SCOPES):
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Arquivo '{CREDENTIALS_PATH.name}' nao encontrado em {BASE_DIR}. "
                    "Baixe as credenciais OAuth (Desktop App) no Google Cloud Console e salve como credentials.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds_nova = flow.run_local_server(port=0)
            if not isinstance(creds_nova, Credentials):
                raise RuntimeError("Tipo de credencial Google inesperado no fluxo OAuth.")
            creds = creds_nova

        assert creds is not None
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    assert creds is not None
    return creds


def _obter_credenciais() -> Credentials:
    """Carrega e renova silenciosamente token.json sem abrir navegador."""
    if not TOKEN_PATH.exists():
        raise GoogleNaoConectadoError(MSG_CONECTAR)

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except Exception as e:
        raise GoogleNaoConectadoError(f"{MSG_CONECTAR} (detalhe: {e})") from e

    if creds and not creds.has_scopes(SCOPES):
        raise GoogleNaoConectadoError(
            "O token.json atual nao possui as novas permissoes do Google Workspace. "
            "Rode `uv run python conectar_google.py` no terminal para reautorizar sua conta."
        )

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as e:
            raise GoogleNaoConectadoError(f"{MSG_CONECTAR} (falha ao renovar token: {e})") from e

    raise GoogleNaoConectadoError(MSG_CONECTAR)


def _servico(nome: str, versao: str):
    creds = _obter_credenciais()
    return build(nome, versao, credentials=creds, cache_discovery=False)


def _limpar_html(conteudo_html: str) -> str:
    sem_style = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", conteudo_html or "")
    com_quebras = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", sem_style)
    sem_tags = re.sub(r"(?s)<[^>]+>", " ", com_quebras)
    texto = html.unescape(sem_tags)
    linhas = [re.sub(r"[ \t]+", " ", ln).strip() for ln in texto.splitlines()]
    return "\n".join(ln for ln in linhas if ln).strip()


def _extrair_corpo_mime(payload: dict) -> str:
    planos: list[str] = []
    htmls: list[str] = []

    def _percorrer(parte: dict):
        mime = (parte.get("mimeType") or "").lower()
        body = parte.get("body") or {}
        data = body.get("data")
        if data and mime in ("text/plain", "text/html"):
            try:
                decodificado = base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")
            except Exception:
                decodificado = ""
            if mime == "text/plain":
                planos.append(decodificado)
            else:
                htmls.append(decodificado)
        for sub in parte.get("parts") or []:
            _percorrer(sub)

    _percorrer(payload or {})
    if planos:
        return "\n\n".join(p.strip() for p in planos if p.strip())
    if htmls:
        return _limpar_html("\n\n".join(htmls))
    return ""


def _normalizar_iso_sp(valor_iso: str) -> datetime:
    bruto = (valor_iso or "").strip()
    if not bruto:
        raise ValueError("Data/hora ISO vazia.")
    if bruto.endswith("Z"):
        bruto = bruto[:-1] + "+00:00"
    dt = datetime.fromisoformat(bruto)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_fuso_sp())
    return dt


def buscar_emails(consulta: str, limite: int = 10) -> str:
    """Busca e-mails no Gmail e lista remetente, assunto, data, ID e trecho."""
    try:
        service = _servico("gmail", "v1")
        max_res = min(max(1, int(limite)), 30)
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=(consulta or "").strip(), maxResults=max_res)
            .execute()
        )
        mensagens = resp.get("messages") or []
        if not mensagens:
            return f"Nenhum e-mail encontrado para a consulta: '{consulta}'."

        linhas = [f"=== GMAIL: resultados para '{consulta}' ({len(mensagens)}) ==="]
        for item in mensagens:
            msg_id = item["id"]
            detalhe = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"].lower(): h["value"]
                for h in (detalhe.get("payload", {}).get("headers") or [])
            }
            remetente = headers.get("from", "(sem remetente)")
            assunto = headers.get("subject", "(sem assunto)")
            data_envio = headers.get("date", "(sem data)")
            trecho = html.unescape(detalhe.get("snippet", "")).strip()
            linhas.append(
                f"- ID: {msg_id}\n"
                f"  De: {remetente}\n"
                f"  Assunto: {assunto}\n"
                f"  Data: {data_envio}\n"
                f"  Trecho: {trecho}"
            )
        sanitizado, _susp, _pads = sanitizar_conteudo_externo("\n".join(linhas), origem="gmail:busca")
        return compactar_saida_tool(sanitizado)
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Gmail ao buscar e-mails: {e}"
    except Exception as e:
        return f"Erro ao buscar e-mails: {e}"


def ler_email(message_id: str) -> str:
    """Le os cabecalhos principais e o corpo completo de um e-mail pelo message_id."""
    mid = (message_id or "").strip()
    if not mid:
        return "Erro: informe um message_id valido."

    try:
        service = _servico("gmail", "v1")
        msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = msg.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in (payload.get("headers") or [])}

        remetente = headers.get("from", "(sem remetente)")
        destinatario = headers.get("to", "(sem destinatario)")
        assunto = headers.get("subject", "(sem assunto)")
        data_envio = headers.get("date", "(sem data)")
        corpo = _extrair_corpo_mime(payload) or html.unescape(msg.get("snippet", ""))
        limite_chars = 12000
        if len(corpo) > limite_chars:
            corpo = corpo[:limite_chars] + "\n...[truncado]"

        bruto = (
            f"ID: {mid}\n"
            f"De: {remetente}\n"
            f"Para: {destinatario}\n"
            f"Data: {data_envio}\n"
            f"Assunto: {assunto}\n\n"
            f"--- Corpo do E-mail ---\n"
            f"{corpo or '(sem conteudo textual)'}"
        )
        sanitizado, _susp, _pads = sanitizar_conteudo_externo(bruto, origem=f"gmail:{mid}")
        return compactar_saida_tool(sanitizado)
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Gmail ao ler e-mail ({mid}): {e}"
    except Exception as e:
        return f"Erro ao ler e-mail ({mid}): {e}"


def criar_rascunho_email(para: str, assunto: str, corpo: str) -> str:
    """Cria apenas um RASCUNHO no Gmail (nunca envia o e-mail diretamente)."""
    dest = (para or "").strip()
    subj = (assunto or "").strip()
    if not dest:
        return "Erro: informe o destinatario ('para')."
    if not subj:
        return "Erro: informe o assunto do e-mail."

    try:
        service = _servico("gmail", "v1")
        email_msg = EmailMessage()
        email_msg["To"] = dest
        email_msg["Subject"] = subj
        email_msg.set_content(corpo or "", charset="utf-8")

        raw = base64.urlsafe_b64encode(email_msg.as_bytes()).decode("ascii")
        rascunho = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        draft_id = rascunho.get("id", "")
        return (
            f"Rascunho criado com sucesso no Gmail (NAO foi enviado automaticamente).\n"
            f"- Draft ID: {draft_id}\n"
            f"- Para: {dest}\n"
            f"- Assunto: {subj}\n"
            f"Revise e envie manualmente na pasta Rascunhos do seu Gmail."
        )
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Gmail ao criar rascunho: {e}"
    except Exception as e:
        return f"Erro ao criar rascunho no Gmail: {e}"


def _limpar_message_ids(message_ids: list[str]) -> list[str]:
    limpos: list[str] = []
    vistos: set[str] = set()
    for mid in message_ids or []:
        m = str(mid or "").strip()
        if m and m not in vistos:
            vistos.add(m)
            limpos.append(m)
    return limpos


def mover_emails_para_lixeira(message_ids: list[str]) -> str:
    """Move e-mails para a Lixeira do Gmail via messages().trash() (NUNCA exclusao permanente; limite de 50 por chamada)."""
    ids = _limpar_message_ids(message_ids)
    if not ids:
        return "Nenhum message_id valido informado. 0 e-mail(s) afetado(s)."
    if len(ids) > LIMITE_LIXEIRA_POR_CHAMADA:
        return (
            f"Limite de seguranca excedido: no maximo {LIMITE_LIXEIRA_POR_CHAMADA} e-mails por chamada "
            f"(recebidos: {len(ids)}). Divida em lotes menores. 0 e-mail(s) afetado(s)."
        )

    try:
        service = _servico("gmail", "v1")
        movidos = 0
        falhas: list[str] = []
        for mid in ids:
            try:
                service.users().messages().trash(userId="me", id=mid).execute()
                movidos += 1
            except Exception as e:
                falhas.append(f"{mid} ({e})")

        msg = f"{movidos} e-mail(s) movido(s) para a Lixeira do Gmail (recuperaveis na Lixeira por 30 dias)."
        if falhas:
            msg += f"\nFalhas ({len(falhas)}): " + "; ".join(falhas[:10])
        return msg
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Gmail ao mover e-mails para a Lixeira: {e}"
    except Exception as e:
        return f"Erro ao mover e-mails para a Lixeira: {e}"


def arquivar_emails(message_ids: list[str]) -> str:
    """Arquiva e-mails removendo o label INBOX via batchModify."""
    ids = _limpar_message_ids(message_ids)
    if not ids:
        return "Nenhum message_id valido informado. 0 e-mail(s) afetado(s)."
    if len(ids) > LIMITE_LOTE_GMAIL:
        return (
            f"Limite de seguranca excedido: no maximo {LIMITE_LOTE_GMAIL} e-mails por chamada "
            f"(recebidos: {len(ids)}). 0 e-mail(s) afetado(s)."
        )

    try:
        service = _servico("gmail", "v1")
        service.users().messages().batchModify(
            userId="me",
            body={"ids": ids, "removeLabelIds": ["INBOX"]},
        ).execute()
        return f"{len(ids)} e-mail(s) arquivado(s) com sucesso (label INBOX removido)."
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Gmail ao arquivar e-mails: {e}"
    except Exception as e:
        return f"Erro ao arquivar e-mails: {e}"


def marcar_emails_como_lidos(message_ids: list[str]) -> str:
    """Marca e-mails como lidos removendo o label UNREAD via batchModify."""
    ids = _limpar_message_ids(message_ids)
    if not ids:
        return "Nenhum message_id valido informado. 0 e-mail(s) afetado(s)."
    if len(ids) > LIMITE_LOTE_GMAIL:
        return (
            f"Limite de seguranca excedido: no maximo {LIMITE_LOTE_GMAIL} e-mails por chamada "
            f"(recebidos: {len(ids)}). 0 e-mail(s) afetado(s)."
        )

    try:
        service = _servico("gmail", "v1")
        service.users().messages().batchModify(
            userId="me",
            body={"ids": ids, "removeLabelIds": ["UNREAD"]},
        ).execute()
        return f"{len(ids)} e-mail(s) marcado(s) como lido(s) com sucesso (label UNREAD removido)."
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Gmail ao marcar e-mails como lidos: {e}"
    except Exception as e:
        return f"Erro ao marcar e-mails como lidos: {e}"


@cache_ttl(
    300.0,
    chave_fn=lambda dias=7: max(1, min(int(dias), 90)),
    armazenar_se=lambda txt: isinstance(txt, str) and not txt.startswith("Erro") and "nao conectado" not in txt.lower(),
)
def listar_proximos_eventos(dias: int = 7, forcar: bool = False) -> str:
    """Lista os compromissos da agenda principal do Google nos proximos X dias (cache RAM 5 min; `forcar=True` atualiza)."""
    try:
        qtd_dias = max(1, min(int(dias), 90))
        service = _servico("calendar", "v3")
        agora = datetime.now(_fuso_sp())
        fim = agora + timedelta(days=qtd_dias)

        resp = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=agora.isoformat(),
                timeMax=fim.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )
        eventos = resp.get("items") or []
        if not eventos:
            return f"Nenhum compromisso encontrado nos proximos {qtd_dias} dia(s)."

        linhas = [f"=== GOOGLE AGENDA: proximos {qtd_dias} dia(s) ({len(eventos)} evento(s)) ==="]
        for ev in eventos:
            ev_id = ev.get("id") or ""
            titulo = ev.get("summary") or "(sem titulo)"
            inicio = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or "?"
            termino = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date") or "?"
            local = (ev.get("location") or "").strip()
            link = ev.get("htmlLink") or ""
            desc = (ev.get("description") or "").strip().replace("\n", " ")[:160]
            bloco = f"- {titulo} (ID: {ev_id})\n  Inicio: {inicio} | Fim: {termino}"
            if local:
                bloco += f"\n  Local: {local}"
            if desc:
                bloco += f"\n  Descricao: {desc}"
            if link:
                bloco += f"\n  Link: {link}"
            linhas.append(bloco)
        sanitizado, _susp, _pads = sanitizar_conteudo_externo("\n".join(linhas), origem="google_agenda")
        return compactar_saida_tool(sanitizado)
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Agenda ao listar eventos: {e}"
    except Exception as e:
        return f"Erro ao listar eventos da agenda: {e}"


def criar_evento_agenda(titulo: str, inicio_iso: str, fim_iso: str, descricao: str = "") -> str:
    """Cria um evento na agenda principal do Google usando fuso America/Sao_Paulo."""
    resumo = (titulo or "").strip()
    if not resumo:
        return "Erro: informe o titulo do evento."

    try:
        dt_inicio = _normalizar_iso_sp(inicio_iso)
        dt_fim = _normalizar_iso_sp(fim_iso)
        if dt_fim <= dt_inicio:
            return "Erro: fim_iso precisa ser posterior a inicio_iso."
    except ValueError as e:
        return f"Erro no formato de data/hora ISO (use ex.: 2026-11-25T14:00:00): {e}"

    try:
        service = _servico("calendar", "v3")
        corpo_evento = {
            "summary": resumo,
            "description": (descricao or "").strip(),
            "start": {
                "dateTime": dt_inicio.isoformat(),
                "timeZone": FUSO_NOME,
            },
            "end": {
                "dateTime": dt_fim.isoformat(),
                "timeZone": FUSO_NOME,
            },
        }
        criado = service.events().insert(calendarId="primary", body=corpo_evento).execute()
        listar_proximos_eventos.limpar_cache()
        return (
            f"Evento criado com sucesso no Google Agenda!\n"
            f"- ID: {criado.get('id', '')}\n"
            f"- Titulo: {resumo}\n"
            f"- Inicio: {dt_inicio.isoformat()} ({FUSO_NOME})\n"
            f"- Fim: {dt_fim.isoformat()} ({FUSO_NOME})\n"
            f"- Link: {criado.get('htmlLink', '')}"
        )
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Agenda ao criar evento: {e}"
    except Exception as e:
        return f"Erro ao criar evento na agenda: {e}"


def remover_evento_agenda(event_id: str) -> str:
    """Remove um evento especifico da agenda principal do Google pelo event_id."""
    eid = (event_id or "").strip()
    if not eid:
        return "Erro: informe um event_id valido."

    try:
        service = _servico("calendar", "v3")
        service.events().delete(calendarId="primary", eventId=eid).execute()
        listar_proximos_eventos.limpar_cache()
        return f"Evento removido com sucesso da agenda principal (ID: {eid})."
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Agenda ao remover evento ({eid}): {e}"
    except Exception as e:
        return f"Erro ao remover evento da agenda ({eid}): {e}"


def buscar_arquivos_drive(consulta: str, limite: int = 10) -> str:
    """Busca arquivos no Google Drive (somente leitura) retornando nome, tipo e link."""
    try:
        service = _servico("drive", "v3")
        max_res = min(max(1, int(limite)), 30)
        termo = (consulta or "").strip()
        if termo:
            escapado = termo.replace("\\", "\\\\").replace("'", "\\'")
            query = f"trashed = false and (name contains '{escapado}' or fullText contains '{escapado}')"
        else:
            query = "trashed = false"

        resp = (
            service.files()
            .list(
                q=query,
                pageSize=max_res,
                fields="files(id, name, mimeType, webViewLink, modifiedTime)",
                orderBy="modifiedTime desc",
            )
            .execute()
        )
        arquivos = resp.get("files") or []
        if not arquivos:
            return f"Nenhum arquivo encontrado no Google Drive para: '{termo or '(recentes)'}'."

        linhas = [f"=== GOOGLE DRIVE: resultados para '{termo or 'recentes'}' ({len(arquivos)}) ==="]
        for arq in arquivos:
            mime = arq.get("mimeType", "")
            tipo = MIME_AMIGAVEL.get(mime, mime or "desconhecido")
            link = arq.get("webViewLink") or f"https://drive.google.com/file/d/{arq.get('id', '')}/view"
            mod = arq.get("modifiedTime", "")
            linhas.append(
                f"- {arq.get('name', '(sem nome)')}\n"
                f"  Tipo: {tipo} | Modificado: {mod}\n"
                f"  Link: {link}"
            )
        sanitizado, _susp, _pads = sanitizar_conteudo_externo("\n".join(linhas), origem="google_drive")
        return compactar_saida_tool(sanitizado)
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Drive ao buscar arquivos: {e}"
    except Exception as e:
        return f"Erro ao buscar arquivos no Google Drive: {e}"


# ==============================================================================
# GOOGLE DOCS, SHEETS, SLIDES E TASKS
# ==============================================================================


def _extrair_id_google(id_ou_url: str) -> str:
    bruto = (id_ou_url or "").strip()
    if not bruto:
        return ""
    m = re.search(r"/d/([A-Za-z0-9_-]{10,})", bruto)
    if m:
        return m.group(1)
    return bruto


def _utf16_len(texto: str) -> int:
    return len(texto.encode("utf-16-le")) // 2


def criar_google_doc(titulo: str, conteudo_texto: str) -> str:
    """Cria um Google Doc formatado (titulo, secoes e paragrafos/topicos) e retorna o link clicavel."""
    tit = (titulo or "").strip()
    if not tit:
        return "Erro: informe um titulo para o Google Doc."

    try:
        service = _servico("docs", "v1")
        doc = service.documents().create(body={"title": tit}).execute()
        doc_id = doc.get("documentId", "")
        link = f"https://docs.google.com/document/d/{doc_id}/edit"

        # Monta blocos estruturados a partir do texto/Markdown
        linhas_raw = (conteudo_texto or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
        blocos: list[tuple[str, str]] = [("TITLE", tit)]

        for ln in linhas_raw:
            s = ln.strip()
            if not s:
                continue
            if s.startswith("### "):
                blocos.append(("HEADING_3", s[4:].strip()))
            elif s.startswith("## "):
                blocos.append(("HEADING_2", s[3:].strip()))
            elif s.startswith("# "):
                txt_h1 = s[2:].strip()
                if txt_h1.lower() != tit.lower():
                    blocos.append(("HEADING_1", txt_h1))
            elif re.match(r"^[-*•]\s+", s):
                blocos.append(("BULLET", re.sub(r"^[-*•]\s+", "", s).strip()))
            else:
                blocos.append(("NORMAL_TEXT", s))

        # Remove marcadores simples de negrito Markdown (**) mantendo texto limpo
        texto_total = ""
        intervalos: list[tuple[int, int, str]] = []
        cursor = 1  # Google Docs body comeca no indice UTF-16 1

        for estilo, texto_bloco in blocos:
            limpo = re.sub(r"\*\*(.+?)\*\*", r"\1", texto_bloco).strip()
            if not limpo:
                continue
            linha_final = limpo + "\n"
            comp = _utf16_len(linha_final)
            intervalos.append((cursor, cursor + comp, estilo))
            texto_total += linha_final
            cursor += comp

        requests: list[dict] = []
        if texto_total:
            requests.append({"insertText": {"location": {"index": 1}, "text": texto_total}})
            for ini, fim, estilo in intervalos:
                if estilo == "BULLET":
                    requests.append({
                        "createParagraphBullets": {
                            "range": {"startIndex": ini, "endIndex": fim},
                            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                        }
                    })
                elif estilo in ("TITLE", "HEADING_1", "HEADING_2", "HEADING_3"):
                    requests.append({
                        "updateParagraphStyle": {
                            "range": {"startIndex": ini, "endIndex": fim},
                            "paragraphStyle": {"namedStyleType": estilo},
                            "fields": "namedStyleType",
                        }
                    })

        if requests:
            service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()

        return (
            "Google Doc criado com sucesso!\n"
            f"- Titulo: {tit}\n"
            f"- ID: {doc_id}\n"
            f"- Link: {link}"
        )
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Docs ao criar documento: {e}"
    except Exception as e:
        return f"Erro ao criar Google Doc: {e}"


def _extrair_texto_elementos_doc(elementos: list[dict]) -> list[str]:
    partes: list[str] = []
    for el in elementos or []:
        if "paragraph" in el:
            runs = []
            for pe in el["paragraph"].get("elements") or []:
                tr = pe.get("textRun")
                if tr and tr.get("content"):
                    runs.append(tr["content"])
            linha = "".join(runs).rstrip("\n")
            if linha.strip():
                partes.append(linha)
        elif "table" in el:
            for row in el["table"].get("tableRows") or []:
                celulas = []
                for cell in row.get("tableCells") or []:
                    txt_c = " ".join(_extrair_texto_elementos_doc(cell.get("content") or [])).strip()
                    celulas.append(txt_c)
                if any(celulas):
                    partes.append(" | ".join(celulas))
    return partes


def ler_google_doc(document_id: str) -> str:
    """Le o titulo e o conteudo completo de um Google Doc pelo document_id (ou URL)."""
    doc_id = _extrair_id_google(document_id)
    if not doc_id:
        return "Erro: informe um document_id valido."

    try:
        service = _servico("docs", "v1")
        doc = service.documents().get(documentId=doc_id).execute()
        titulo = doc.get("title") or "(Sem titulo)"
        conteudo = "\n".join(_extrair_texto_elementos_doc((doc.get("body") or {}).get("content") or [])).strip()
        if len(conteudo) > 16000:
            conteudo = conteudo[:16000] + "\n...[conteudo truncado]"
        link = f"https://docs.google.com/document/d/{doc_id}/edit"
        bruto_doc = (
            f"=== GOOGLE DOC: {titulo} ===\n"
            f"ID: {doc_id}\n"
            f"Link: {link}\n\n"
            f"{conteudo or '(documento vazio)'}"
        )
        sanitizado_doc, _susp, _pads = sanitizar_conteudo_externo(bruto_doc, origem=f"google_docs:{doc_id}")
        return compactar_saida_tool(sanitizado_doc)
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Docs ao ler documento ({doc_id}): {e}"
    except Exception as e:
        return f"Erro ao ler Google Doc ({doc_id}): {e}"


def criar_google_sheet(
    titulo: str,
    aba_nome: str = "Dados",
    linhas: list[list[str]] | None = None,
) -> str:
    """Cria uma planilha no Google Sheets ja preenchida com cabecalho, dados/formulas e retorna o link."""
    tit = (titulo or "").strip()
    if not tit:
        return "Erro: informe um titulo para a planilha."
    aba = re.sub(r"[:\\/?*\[\]]+", " ", (aba_nome or "Dados").strip()).strip()[:80] or "Dados"

    try:
        service = _servico("sheets", "v4")
        criada = (
            service.spreadsheets()
            .create(
                body={
                    "properties": {"title": tit},
                    "sheets": [{"properties": {"title": aba}}],
                },
                fields="spreadsheetId,spreadsheetUrl,sheets(properties(sheetId,title))",
            )
            .execute()
        )
        sid = criada.get("spreadsheetId", "")
        link = criada.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sid}/edit"
        sheet_id = 0
        sheets_meta = criada.get("sheets") or []
        if sheets_meta and isinstance(sheets_meta[0].get("properties"), dict):
            sheet_id = int(sheets_meta[0]["properties"].get("sheetId", 0))

        linhas_limpas = [
            ["" if cel is None else str(cel) for cel in row]
            for row in (linhas or [])
            if isinstance(row, (list, tuple))
        ]
        if linhas_limpas:
            aba_esc = aba.replace("'", "''")
            service.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"'{aba_esc}'!A1",
                valueInputOption="USER_ENTERED",
                body={"values": linhas_limpas},
            ).execute()

            # Congela a primeira linha (cabecalho) e destaca em negrito
            with contextlib.suppress(Exception):
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sid,
                    body={
                        "requests": [
                            {
                                "updateSheetProperties": {
                                    "properties": {
                                        "sheetId": sheet_id,
                                        "gridProperties": {"frozenRowCount": 1},
                                    },
                                    "fields": "gridProperties.frozenRowCount",
                                }
                            },
                            {
                                "repeatCell": {
                                    "range": {
                                        "sheetId": sheet_id,
                                        "startRowIndex": 0,
                                        "endRowIndex": 1,
                                    },
                                    "cell": {
                                        "userEnteredFormat": {
                                            "textFormat": {"bold": True},
                                        }
                                    },
                                    "fields": "userEnteredFormat.textFormat.bold",
                                }
                            },
                        ]
                    },
                ).execute()

        return (
            "Google Sheet criada com sucesso!\n"
            f"- Titulo: {tit}\n"
            f"- Aba: {aba} ({len(linhas_limpas)} linha(s) inserida(s))\n"
            f"- ID: {sid}\n"
            f"- Link: {link}"
        )
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Sheets ao criar planilha: {e}"
    except Exception as e:
        return f"Erro ao criar Google Sheet: {e}"


def ler_google_sheet(spreadsheet_id: str, intervalo: str = "A1:Z100") -> str:
    """Le dados de uma planilha do Google Sheets pelo spreadsheet_id (ou URL) e intervalo."""
    sid = _extrair_id_google(spreadsheet_id)
    if not sid:
        return "Erro: informe um spreadsheet_id valido."
    rng = (intervalo or "A1:Z100").strip()

    try:
        service = _servico("sheets", "v4")
        meta = (
            service.spreadsheets()
            .get(spreadsheetId=sid, fields="properties(title),sheets(properties(title))")
            .execute()
        )
        titulo = (meta.get("properties") or {}).get("title") or "(Sem titulo)"
        abas = [
            (s.get("properties") or {}).get("title", "")
            for s in (meta.get("sheets") or [])
        ]

        resp = service.spreadsheets().values().get(spreadsheetId=sid, range=rng).execute()
        valores = resp.get("values") or []
        link = f"https://docs.google.com/spreadsheets/d/{sid}/edit"

        linhas = [
            f"=== GOOGLE SHEET: {titulo} ===",
            f"ID: {sid} | Abas: {', '.join(a for a in abas if a) or '-'}",
            f"Intervalo: {resp.get('range', rng)} | Link: {link}",
            "",
        ]
        if not valores:
            linhas.append("(Nenhuma celula preenchida neste intervalo.)")
        else:
            for idx, row in enumerate(valores[:120], start=1):
                linhas.append(f"{idx:>3}: " + " | ".join(str(c) for c in row))
            if len(valores) > 120:
                linhas.append(f"... e mais {len(valores) - 120} linha(s) omitida(s).")
        sanitizado, _susp, _pads = sanitizar_conteudo_externo("\n".join(linhas), origem=f"google_sheets:{sid}")
        return compactar_saida_tool(sanitizado)
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Sheets ao ler planilha ({sid}): {e}"
    except Exception as e:
        return f"Erro ao ler Google Sheet ({sid}): {e}"


def criar_google_slides(titulo: str, slides: list[dict]) -> str:
    """Cria uma apresentacao no Google Slides com capa + slides de conteudo (titulo + topicos) e retorna o link."""
    tit = (titulo or "").strip()
    if not tit:
        return "Erro: informe um titulo para a apresentacao."

    try:
        service = _servico("slides", "v1")
        pres = service.presentations().create(body={"title": tit}).execute()
        pres_id = pres.get("presentationId", "")
        link = f"https://docs.google.com/presentation/d/{pres_id}/edit"

        requests: list[dict] = []

        # Preenche a capa inicial (slide 0 criado por padrao)
        slides_existentes = pres.get("slides") or []
        if slides_existentes:
            capa = slides_existentes[0]
            for pe in capa.get("pageElements") or []:
                shape = pe.get("shape") or {}
                ph_type = (shape.get("placeholder") or {}).get("type", "")
                oid = pe.get("objectId")
                if not oid:
                    continue
                if ph_type in ("CENTERED_TITLE", "TITLE"):
                    requests.append({"insertText": {"objectId": oid, "insertionIndex": 0, "text": tit}})
                elif ph_type == "SUBTITLE":
                    sub = f"Gerado pela Athena · {datetime.now(_fuso_sp()).strftime('%d/%m/%Y')}"
                    requests.append({"insertText": {"objectId": oid, "insertionIndex": 0, "text": sub}})

        # Cria cada slide de conteudo (TITLE_AND_BODY) com titulo e topicos em lista
        qtd_criados = 0
        for idx, item in enumerate(slides or [], start=1):
            if not isinstance(item, dict):
                continue
            tit_slide = str(item.get("titulo") or f"Slide {idx}").strip()
            topicos_raw = item.get("topicos") or []
            if isinstance(topicos_raw, str):
                topicos = [t.strip() for t in topicos_raw.splitlines() if t.strip()]
            else:
                topicos = [str(t).strip() for t in topicos_raw if str(t).strip()]

            sid_obj = f"athena_slide_{idx}"
            title_obj = f"athena_title_{idx}"
            body_obj = f"athena_body_{idx}"

            requests.append({
                "createSlide": {
                    "objectId": sid_obj,
                    "insertionIndex": idx,
                    "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
                    "placeholderIdMappings": [
                        {
                            "layoutPlaceholder": {"type": "TITLE", "index": 0},
                            "objectId": title_obj,
                        },
                        {
                            "layoutPlaceholder": {"type": "BODY", "index": 0},
                            "objectId": body_obj,
                        },
                    ],
                }
            })
            requests.append({
                "insertText": {
                    "objectId": title_obj,
                    "insertionIndex": 0,
                    "text": tit_slide,
                }
            })
            if topicos:
                corpo_txt = "\n".join(topicos)
                requests.append({
                    "insertText": {
                        "objectId": body_obj,
                        "insertionIndex": 0,
                        "text": corpo_txt,
                    }
                })
                requests.append({
                    "createParagraphBullets": {
                        "objectId": body_obj,
                        "textRange": {"type": "ALL"},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                })
            qtd_criados += 1

        if requests:
            service.presentations().batchUpdate(presentationId=pres_id, body={"requests": requests}).execute()

        return (
            "Google Slides criado com sucesso!\n"
            f"- Titulo: {tit}\n"
            f"- Total de slides: {qtd_criados + 1} (capa + {qtd_criados} slide(s) de conteudo)\n"
            f"- ID: {pres_id}\n"
            f"- Link: {link}"
        )
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Slides ao criar apresentacao: {e}"
    except Exception as e:
        return f"Erro ao criar Google Slides: {e}"


def listar_google_tasks(limite: int = 30) -> str:
    """Lista tarefas pendentes do Google Tasks (lista padrao do usuario)."""
    try:
        service = _servico("tasks", "v1")
        max_res = min(max(1, int(limite)), 100)
        resp = (
            service.tasks()
            .list(
                tasklist="@default",
                showCompleted=False,
                showHidden=False,
                maxResults=max_res,
            )
            .execute()
        )
        itens = resp.get("items") or []
        if not itens:
            return "Nenhuma tarefa pendente no Google Tasks. Link: https://tasks.google.com/"

        linhas = [f"=== GOOGLE TASKS ({len(itens)} pendente(s)) ==="]
        for t in itens:
            titulo = (t.get("title") or "(Sem titulo)").strip()
            due = (t.get("due") or "")[:10]
            notas = (t.get("notes") or "").strip()
            tid = t.get("id", "")
            due_txt = f" | Vencimento: {due}" if due else ""
            notas_txt = f"\n  Notas: {notas[:220]}" if notas else ""
            linhas.append(f"- {titulo}{due_txt} [ID: {tid}]{notas_txt}")
        linhas.append("\nLink: https://tasks.google.com/")
        sanitizado, _susp, _pads = sanitizar_conteudo_externo("\n".join(linhas), origem="google_tasks")
        return compactar_saida_tool(sanitizado)
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Tasks ao listar tarefas: {e}"
    except Exception as e:
        return f"Erro ao listar Google Tasks: {e}"


def criar_google_task(titulo: str, notas: str = "", vencimento_iso: str = "") -> str:
    """Cria uma tarefa na lista padrao do Google Tasks (vencimento_iso opcional: AAAA-MM-DD)."""
    tit = (titulo or "").strip()
    if not tit:
        return "Erro: informe um titulo para a tarefa."

    corpo: dict = {"title": tit}
    if (notas or "").strip():
        corpo["notes"] = notas.strip()

    venc = (vencimento_iso or "").strip()
    if venc:
        m_dt = re.match(r"^(\d{4}-\d{2}-\d{2})", venc)
        if not m_dt:
            return f"Erro: vencimento_iso invalido ('{venc}'). Use o formato AAAA-MM-DD."
        corpo["due"] = f"{m_dt.group(1)}T00:00:00.000Z"

    try:
        service = _servico("tasks", "v1")
        criada = service.tasks().insert(tasklist="@default", body=corpo).execute()
        return (
            "Tarefa criada com sucesso no Google Tasks!\n"
            f"- ID: {criada.get('id', '')}\n"
            f"- Titulo: {criada.get('title', tit)}\n"
            f"- Vencimento: {(criada.get('due') or 'Sem prazo')[:10]}\n"
            "- Link: https://tasks.google.com/"
        )
    except GoogleNaoConectadoError as e:
        return str(e)
    except HttpError as e:
        return f"Erro da API do Google Tasks ao criar tarefa: {e}"
    except Exception as e:
        return f"Erro ao criar tarefa no Google Tasks: {e}"
