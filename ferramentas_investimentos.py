import csv
import html
import io
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import yfinance as yf
from pypdf import PdfReader

from cache_ttl import cache_ttl
from ferramentas_obsidian import escrever_nota_vault

DB_PATH = Path(__file__).resolve().parent / "memoria.db"
SALDO_PADRAO = 10000.0

TIPOS_CARTEIRA_REAL: tuple[str, ...] = (
    "conta_cdi",
    "renda_fixa",
    "cripto",
    "acoes_fiis",
    "etf",
)

ROTULOS_TIPO_REAL: dict[str, str] = {
    "conta_cdi": "Conta CDI",
    "renda_fixa": "Renda Fixa",
    "cripto": "Cripto",
    "acoes_fiis": "Ações / FIIs",
    "etf": "ETF",
}

BCB_SELIC_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json"
_SELIC_FALLBACK_ANUAL = 14.25
TTL_CACHE_MERCADO_SEGUNDOS = 300  # 5 minutos em RAM
NOTA_OBSIDIAN_APORTES = "03-Financas/Aportes.md"


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS carteira_caixa (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                saldo REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS carteira_posicoes (
                ticker TEXT PRIMARY KEY,
                quantidade REAL NOT NULL,
                preco_medio REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS carteira_operacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_hora TEXT NOT NULL,
                tipo TEXT NOT NULL,
                ticker TEXT NOT NULL,
                quantidade REAL NOT NULL,
                preco REAL NOT NULL,
                total REAL NOT NULL,
                motivo TEXT DEFAULT ''
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS carteira_real (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instituicao TEXT DEFAULT 'Mercado Pago',
                tipo TEXT NOT NULL,
                nome TEXT NOT NULL,
                ticker TEXT DEFAULT '',
                valor_atual REAL NOT NULL,
                taxa_info TEXT DEFAULT '',
                atualizado_em TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS extrato_movimentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                categoria TEXT NOT NULL,
                origem TEXT DEFAULT 'Mercado Pago'
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS propostas_investimento (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ativo TEXT NOT NULL,
                tipo TEXT NOT NULL,
                valor_sugerido REAL NOT NULL,
                risco TEXT NOT NULL,
                motivo TEXT NOT NULL,
                comparacao_cdi TEXT NOT NULL,
                status TEXT DEFAULT 'pendente',
                criado_em TEXT NOT NULL
            )
            """
        )
        row = c.execute("SELECT saldo FROM carteira_caixa WHERE id = 1").fetchone()
        if row is None:
            c.execute("INSERT INTO carteira_caixa (id, saldo) VALUES (1, ?)", (SALDO_PADRAO,))
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@cache_ttl(
    TTL_CACHE_MERCADO_SEGUNDOS,
    chave_fn=lambda ticker: (ticker or "").strip().upper(),
    armazenar_se=lambda res: res is not None and res[1] is not None and not res[1].empty,
)
def _normalizar_e_buscar_historico(ticker: str, forcar: bool = False):
    simbolo = ticker.strip().upper()
    tentativas = [simbolo]
    if "." not in simbolo and "-" not in simbolo and len(simbolo) in (5, 6) and simbolo[-1].isdigit():
        tentativas.insert(0, simbolo + ".SA")
    for sym in tentativas:
        hist = yf.Ticker(sym).history(period="3mo")
        if hist is not None and not hist.empty:
            return sym, hist
    return simbolo, None


@cache_ttl(
    TTL_CACHE_MERCADO_SEGUNDOS,
    chave_fn=lambda ticker: (ticker or "").strip().upper(),
)
def consultar_ativo(ticker: str, forcar: bool = False) -> str:
    sym, hist = _normalizar_e_buscar_historico(ticker, forcar=forcar)
    if hist is None or hist.empty:
        return f"Nao encontrei cotacao para '{ticker}'. Tente no formato PETR4.SA, MXRF11.SA, AAPL ou BTC-USD."

    closes = hist["Close"]
    preco = float(closes.iloc[-1])
    var_5d = ((preco / float(closes.iloc[-5])) - 1) * 100 if len(closes) >= 5 else 0.0
    var_30d = ((preco / float(closes.iloc[-21])) - 1) * 100 if len(closes) >= 21 else 0.0
    mm20 = float(closes.tail(20).mean())
    mm50 = float(closes.tail(50).mean()) if len(closes) >= 50 else mm20
    tendencia = "ALTA (preco acima da MM20)" if preco >= mm20 else "BAIXA (preco abaixo da MM20)"

    return (
        f"Ativo: {sym}\n"
        f"- Preco atual (ultimo fechamento): {preco:.2f}\n"
        f"- Variacao ~5 pregoes: {var_5d:+.2f}%\n"
        f"- Variacao ~1 mes: {var_30d:+.2f}%\n"
        f"- Media movel 20d: {mm20:.2f} | Media movel 50d: {mm50:.2f}\n"
        f"- Sinal tecnico simples: {tendencia}"
    )


def definir_caixa(valor: float) -> str:
    if valor < 0:
        return "Valor invalido."
    with _conn() as c:
        c.execute("UPDATE carteira_caixa SET saldo = ? WHERE id = 1", (float(valor),))
    return f"Saldo simulado atualizado para {valor:.2f}."


def comprar_simulado(ticker: str, quantidade: float, motivo: str = "") -> str:
    if quantidade <= 0:
        return "Quantidade precisa ser maior que zero."
    sym, hist = _normalizar_e_buscar_historico(ticker)
    if hist is None or hist.empty:
        return f"Nao encontrei preco real para '{ticker}'."

    preco = float(hist["Close"].iloc[-1])
    total = preco * quantidade

    with _conn() as c:
        saldo = float(c.execute("SELECT saldo FROM carteira_caixa WHERE id = 1").fetchone()[0])
        if total > saldo:
            return f"Saldo simulado insuficiente! Custo: {total:.2f} | Caixa disponivel: {saldo:.2f}."

        pos = c.execute("SELECT quantidade, preco_medio FROM carteira_posicoes WHERE ticker = ?", (sym,)).fetchone()
        if pos:
            nova_qtd = float(pos[0]) + quantidade
            novo_pm = ((float(pos[0]) * float(pos[1])) + total) / nova_qtd
            c.execute("UPDATE carteira_posicoes SET quantidade=?, preco_medio=? WHERE ticker=?", (nova_qtd, novo_pm, sym))
        else:
            c.execute("INSERT INTO carteira_posicoes (ticker, quantidade, preco_medio) VALUES (?, ?, ?)", (sym, quantidade, preco))

        novo_saldo = saldo - total
        c.execute("UPDATE carteira_caixa SET saldo = ? WHERE id = 1", (novo_saldo,))
        c.execute(
            "INSERT INTO carteira_operacoes (data_hora, tipo, ticker, quantidade, preco, total, motivo) VALUES (?, 'COMPRA', ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), sym, quantidade, preco, total, motivo),
        )

    return f"[SIMULADO] COMPRA executada: {quantidade}x {sym} a {preco:.2f} (Total: {total:.2f}). Caixa restante: {novo_saldo:.2f}."


def vender_simulado(ticker: str, quantidade: float, motivo: str = "") -> str:
    if quantidade <= 0:
        return "Quantidade precisa ser maior que zero."
    sym, hist = _normalizar_e_buscar_historico(ticker)
    if hist is None or hist.empty:
        return f"Nao encontrei preco real para '{ticker}'."

    preco = float(hist["Close"].iloc[-1])
    total = preco * quantidade

    with _conn() as c:
        pos = c.execute("SELECT quantidade, preco_medio FROM carteira_posicoes WHERE ticker = ?", (sym,)).fetchone()
        if not pos or float(pos[0]) < quantidade:
            qtd_atual = float(pos[0]) if pos else 0.0
            return f"Posicao insuficiente em {sym}. Voce tem {qtd_atual}."

        pm = float(pos[1])
        lucro = (preco - pm) * quantidade
        restante = float(pos[0]) - quantidade
        if restante <= 1e-8:
            c.execute("DELETE FROM carteira_posicoes WHERE ticker = ?", (sym,))
        else:
            c.execute("UPDATE carteira_posicoes SET quantidade = ? WHERE ticker = ?", (restante, sym))

        saldo = float(c.execute("SELECT saldo FROM carteira_caixa WHERE id = 1").fetchone()[0])
        novo_saldo = saldo + total
        c.execute("UPDATE carteira_caixa SET saldo = ? WHERE id = 1", (novo_saldo,))
        c.execute(
            "INSERT INTO carteira_operacoes (data_hora, tipo, ticker, quantidade, preco, total, motivo) VALUES (?, 'VENDA', ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), sym, quantidade, preco, total, motivo),
        )

    return f"[SIMULADO] VENDA executada: {quantidade}x {sym} a {preco:.2f} (Total: {total:.2f} | Lucro/Prejuizo: {lucro:+.2f}). Novo caixa: {novo_saldo:.2f}."


def ver_carteira() -> str:
    with _conn() as c:
        saldo = float(c.execute("SELECT saldo FROM carteira_caixa WHERE id = 1").fetchone()[0])
        posicoes = c.execute("SELECT ticker, quantidade, preco_medio FROM carteira_posicoes ORDER BY ticker").fetchall()

    linhas = ["=== CARTEIRA SIMULADA (PAPER TRADING) ===", f"Caixa disponivel: {saldo:.2f}"]
    total_ativos = 0.0
    if not posicoes:
        linhas.append("Posicoes abertas: (nenhuma ainda)")
    else:
        linhas.append("Posicoes abertas:")
        for sym, qtd, pm in posicoes:
            _, hist = _normalizar_e_buscar_historico(sym)
            preco_atual = float(hist["Close"].iloc[-1]) if (hist is not None and not hist.empty) else pm
            valor_atual = preco_atual * qtd
            total_ativos += valor_atual
            res = (preco_atual - pm) * qtd
            pct = ((preco_atual / pm) - 1) * 100 if pm else 0.0
            linhas.append(f"  - {sym}: {qtd:g} un. | PM {pm:.2f} | Atual {preco_atual:.2f} | Total {valor_atual:.2f} ({res:+.2f} / {pct:+.2f}%)")

    linhas.append(f"Patrimonio total simulado: {saldo + total_ativos:.2f}")
    return "\n".join(linhas)


def ver_operacoes(limite: int = 12) -> str:
    with _conn() as c:
        rows = c.execute(
            "SELECT data_hora, tipo, ticker, quantidade, preco, total, motivo FROM carteira_operacoes ORDER BY id DESC LIMIT ?",
            (limite,),
        ).fetchall()
    if not rows:
        return "Nenhuma operacao simulada registrada ainda."
    return "\n".join(
        f"- [{r[0]}] {r[1]} {r[3]:g}x {r[2]} @ {r[4]:.2f} (Total {r[5]:.2f}) | {r[6]}"
        for r in rows
    )


# ==============================================================================
# CARTEIRA REAL (MERCADO PAGO), BCB (SELIC/CDI), NOTICIAS RSS E PROPOSTAS
# ==============================================================================


def _norm_ascii(txt: str) -> str:
    return (
        unicodedata.normalize("NFKD", (txt or "").strip())
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def _normalizar_tipo_real(tipo: str) -> str:
    t = _norm_ascii(tipo).replace("-", "_").replace(" ", "_")
    if t in TIPOS_CARTEIRA_REAL:
        return t
    if any(k in t for k in ("cdi", "conta", "cofrinho", "reserva")):
        return "conta_cdi"
    if any(k in t for k in ("renda", "cdb", "lci", "lca", "tesouro", "fixa", "debenture")):
        return "renda_fixa"
    if any(k in t for k in ("cripto", "crypto", "btc", "eth", "bitcoin", "solana")):
        return "cripto"
    if "etf" in t:
        return "etf"
    if any(k in t for k in ("acao", "acoes", "fii", "fiis", "bdr", "bolsa", "stock")):
        return "acoes_fiis"
    return "renda_fixa"


def _parse_valor_br(texto: str) -> float | None:
    s = (texto or "").strip()
    if not s:
        return None
    s = s.replace("R$", "").replace("\xa0", "").replace(" ", "")
    s = re.sub(r"[^0-9,.\-+]", "", s)
    if not s or s in ("+", "-", ".", ","):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _categorizar_movimento(descricao: str, valor: float) -> str:
    d = _norm_ascii(descricao)
    if any(k in d for k in ("rendimento", "cdi", "juros", "dividendo", "provento", "cashback")):
        return "rendimento_cdi"
    if any(k in d for k in ("cdb", "tesouro", "cripto", "bitcoin", "aplicacao", "resgate", "invest", "reserva")):
        return "investimento"
    if any(k in d for k in ("pix", "transferencia", "ted", "doc")):
        return "pix_transferencia"
    if any(k in d for k in ("deposito", "salario", "recebimento", "recarga")):
        return "deposito"
    if valor < 0:
        return "pagamento_compra"
    return "outros"


@cache_ttl(TTL_CACHE_MERCADO_SEGUNDOS, chave_fn=lambda: "selic_cdi_bcb")
def obter_dados_selic_cdi_bcb(forcar: bool = False) -> dict:
    """Consulta a taxa Selic Meta no Banco Central (SGS 432) com cache em RAM de 5 min e fallback seguro."""
    selic = _SELIC_FALLBACK_ANUAL
    data_ref = datetime.now().strftime("%d/%m/%Y")
    fonte = "fallback estimado (API BCB indisponivel)"

    try:
        resp = httpx.get(
            BCB_SELIC_URL,
            timeout=8.0,
            headers={"Accept": "application/json", "User-Agent": "Athena/2.0"},
        )
        if resp.status_code == 200:
            arr = resp.json()
            if isinstance(arr, list) and arr:
                item = arr[-1]
                val = _parse_valor_br(str(item.get("valor", "")))
                if val is not None and 0.0 < val < 50.0:
                    selic = round(val, 2)
                    data_ref = str(item.get("data") or data_ref)
                    fonte = "Banco Central do Brasil (SGS 432)"
    except Exception:
        pass

    cdi_anual = round(max(0.0, selic - 0.10), 2)
    cdi_mensal = round((((1.0 + cdi_anual / 100.0) ** (1.0 / 12.0)) - 1.0) * 100.0, 3)
    rend_1000_mes = round(1000.0 * (cdi_mensal / 100.0), 2)
    rend_1000_ano = round(1000.0 * (cdi_anual / 100.0), 2)

    return {
        "selic_anual": selic,
        "cdi_anual": cdi_anual,
        "cdi_mensal": cdi_mensal,
        "rendimento_1000_mensal_bruto": rend_1000_mes,
        "rendimento_1000_anual_bruto": rend_1000_ano,
        "data_referencia": data_ref,
        "fonte": fonte,
    }


def obter_selic_cdi_bcb(forcar: bool = False) -> str:
    """Retorna Selic/CDI atual do Banco Central (cache RAM 5 min; `forcar=True` atualiza) e rendimento estimado de 100% CDI."""
    d = obter_dados_selic_cdi_bcb(forcar=forcar)
    return (
        "=== TAXAS DE REFERENCIA (BANCO CENTRAL) ===\n"
        f"- Selic Meta: {d['selic_anual']:.2f}% a.a.\n"
        f"- CDI estimado (100% CDI): {d['cdi_anual']:.2f}% a.a. (~{d['cdi_mensal']:.3f}% a.m.)\n"
        f"- Rendimento bruto estimado de R$ 1.000 a 100% CDI: "
        f"~R$ {d['rendimento_1000_mensal_bruto']:.2f}/mes | ~R$ {d['rendimento_1000_anual_bruto']:.2f}/ano\n"
        f"- Referencia: {d['data_referencia']} | Fonte: {d['fonte']}"
    )


def buscar_noticias_mercado(tema: str = "mercado financeiro brasil", limite: int = 8) -> str:
    """Le feeds RSS publicos de financas/economia (Google News Financas e InfoMoney)."""
    consulta = (tema or "mercado financeiro brasil").strip()
    max_itens = max(1, min(int(limite), 15))

    feeds = [
        ("Google News Financas", f"https://news.google.com/rss/search?q={quote_plus(consulta)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"),
        ("InfoMoney", "https://www.infomoney.com.br/feed/"),
    ]

    noticias: list[dict[str, str]] = []
    vistos: set[str] = set()

    for nome_feed, url in feeds:
        if len(noticias) >= max_itens:
            break
        try:
            resp = httpx.get(
                url,
                timeout=8.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 Athena/2.0"},
            )
            if resp.status_code != 200 or not resp.content:
                continue
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item"):
                titulo_raw = (item.findtext("title") or "").strip()
                titulo = html.unescape(re.sub(r"<[^>]+>", "", titulo_raw)).strip()
                if not titulo:
                    continue
                chave = _norm_ascii(titulo)[:80]
                if chave in vistos:
                    continue
                vistos.add(chave)

                fonte_el = item.find("source")
                fonte = (fonte_el.text or "").strip() if (fonte_el is not None and fonte_el.text) else nome_feed
                pub_date = (item.findtext("pubDate") or "").strip()[:25]
                noticias.append({"titulo": titulo, "fonte": fonte, "data": pub_date})
                if len(noticias) >= max_itens:
                    break
        except Exception:
            continue

    if not noticias:
        return f"Nao foi possivel carregar noticias RSS para '{consulta}' no momento (verifique conexao)."

    linhas = [f"=== NOTICIAS DE MERCADO: '{consulta}' ({len(noticias)}) ==="]
    for n in noticias:
        dt = f" | {n['data']}" if n["data"] else ""
        linhas.append(f"- {n['titulo']} ({n['fonte']}{dt})")
    return "\n".join(linhas)


def _extrair_movimentos_csv(dados_bytes: bytes) -> list[tuple[str, str, float]]:
    texto = ""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            texto = dados_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not texto.strip():
        return []

    linhas_raw = [ln for ln in texto.splitlines() if ln.strip()]
    if not linhas_raw:
        return []

    amostra = "\n".join(linhas_raw[:10])
    delim = ";" if amostra.count(";") >= amostra.count(",") else ","

    # Procura linha de cabecalho valida (extratos Mercado Pago as vezes tem linhas de resumo no topo)
    idx_header = 0
    for idx, ln in enumerate(linhas_raw[:15]):
        low = _norm_ascii(ln)
        if any(k in low for k in ("data", "date", "release")) and any(
            k in low for k in ("valor", "amount", "total", "net", "descricao", "transaction")
        ):
            idx_header = idx
            break

    reader = csv.DictReader(io.StringIO("\n".join(linhas_raw[idx_header:])), delimiter=delim)
    movs: list[tuple[str, str, float]] = []

    for row in reader:
        if not isinstance(row, dict):
            continue
        norm_row = {_norm_ascii(k or ""): (v or "").strip() for k, v in row.items() if k is not None}

        data_val = ""
        for k, v in norm_row.items():
            if v and any(tok in k for tok in ("release_date", "data", "date")):
                data_val = v[:19]
                break

        desc_val = ""
        for k, v in norm_row.items():
            if v and any(tok in k for tok in ("descricao", "description", "transaction_type", "detalhe", "tipo", "historico")):
                desc_val = v[:180]
                break

        val_num = None
        for k, v in norm_row.items():
            if v and any(tok in k for tok in ("transaction_net_amount", "valor", "net_amount", "amount", "total_amount", "montante")):
                val_num = _parse_valor_br(v)
                if val_num is not None:
                    break

        if val_num is not None and abs(val_num) > 1e-6:
            movs.append((data_val or datetime.now().strftime("%d/%m/%Y"), desc_val or "Movimentacao Mercado Pago", round(val_num, 2)))

    return movs


def _extrair_movimentos_pdf(dados_bytes: bytes) -> list[tuple[str, str, float]]:
    reader = PdfReader(io.BytesIO(dados_bytes))
    movs: list[tuple[str, str, float]] = []
    padrao_linha = re.compile(
        r"(\d{2}[/-]\d{2}[/-]\d{2,4})\s+(.+?)\s+([+-]?\s*(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}|[+-]?\s*(?:R\$\s*)?\d+,\d{2})"
    )
    for pag in reader.pages:
        txt = pag.extract_text() or ""
        for ln in txt.splitlines():
            ln_limpa = re.sub(r"\s+", " ", ln).strip()
            m = padrao_linha.search(ln_limpa)
            if not m:
                continue
            dt, desc, val_txt = m.group(1), m.group(2).strip(" -|"), m.group(3)
            val = _parse_valor_br(val_txt)
            if val is None or abs(val) < 1e-6:
                continue
            low_desc = _norm_ascii(desc)
            if val > 0 and any(k in low_desc for k in ("pagamento", "compra", "pix enviado", "tarifa", "saque", "debito")):
                val = -val
            movs.append((dt, desc[:180] or "Movimentacao PDF", round(val, 2)))
    return movs


def importar_extrato_mercado_pago(dados_bytes: bytes, nome_arquivo: str) -> dict:
    """Extrai lancamentos de CSV ou PDF do Mercado Pago, calcula entradas/saidas e salva em extrato_movimentos."""
    if not dados_bytes:
        return {"ok": False, "erro": "Arquivo de extrato vazio."}

    ext = Path(nome_arquivo or "").suffix.lower()
    try:
        if ext == ".pdf" or dados_bytes[:4] == b"%PDF":
            movs = _extrair_movimentos_pdf(dados_bytes)
        elif ext in (".csv", ".txt") or ext == "":
            movs = _extrair_movimentos_csv(dados_bytes)
        else:
            return {"ok": False, "erro": f"Formato nao suportado ({ext}). Envie extrato em CSV ou PDF."}
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao ler extrato '{nome_arquivo}': {e}"}

    if not movs:
        return {
            "ok": False,
            "erro": "Nenhum lancamento financeiro reconhecido neste arquivo. Verifique se e um extrato CSV ou PDF do Mercado Pago.",
        }

    importados = 0
    duplicados = 0
    entradas = 0.0
    saidas = 0.0

    with _conn() as c:
        existentes = {
            (r[0], r[1], round(float(r[2]), 2))
            for r in c.execute("SELECT data, descricao, valor FROM extrato_movimentos").fetchall()
        }
        for dt, desc, val in movs:
            chave = (dt, desc, round(val, 2))
            if chave in existentes:
                duplicados += 1
                continue
            cat = _categorizar_movimento(desc, val)
            c.execute(
                "INSERT INTO extrato_movimentos (data, descricao, valor, categoria, origem) VALUES (?, ?, ?, ?, ?)",
                (dt, desc, round(val, 2), cat, "Mercado Pago"),
            )
            existentes.add(chave)
            importados += 1
            if val >= 0:
                entradas += val
            else:
                saidas += abs(val)

    return {
        "ok": True,
        "arquivo": Path(nome_arquivo or "extrato").name,
        "importados": importados,
        "duplicados_ignorados": duplicados,
        "entradas": round(entradas, 2),
        "saidas": round(saidas, 2),
        "saldo_liquido": round(entradas - saidas, 2),
    }


def salvar_ativo_real_estruturado(
    tipo: str,
    nome: str,
    valor_atual: float,
    ticker: str = "",
    taxa_info: str = "",
    instituicao: str = "Mercado Pago",
) -> dict:
    """Adiciona ou atualiza um saldo/ativo na tabela carteira_real."""
    nome_limpo = (nome or "").strip()
    if not nome_limpo:
        return {"ok": False, "erro": "Informe o nome do ativo ou saldo."}
    try:
        val = round(float(valor_atual), 2)
    except (TypeError, ValueError):
        return {"ok": False, "erro": "Valor atual invalido."}
    if val < 0:
        return {"ok": False, "erro": "O valor atual nao pode ser negativo."}

    tipo_norm = _normalizar_tipo_real(tipo)
    inst_limpa = (instituicao or "Mercado Pago").strip() or "Mercado Pago"
    ticker_limpo = (ticker or "").strip().upper()
    taxa_limpa = (taxa_info or "").strip()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")

    with _conn() as c:
        existente = c.execute(
            "SELECT id FROM carteira_real WHERE LOWER(instituicao)=LOWER(?) AND tipo=? AND LOWER(nome)=LOWER(?)",
            (inst_limpa, tipo_norm, nome_limpo),
        ).fetchone()
        if existente:
            ativo_id = int(existente[0])
            c.execute(
                """
                UPDATE carteira_real
                SET ticker = ?, valor_atual = ?, taxa_info = ?, atualizado_em = ?
                WHERE id = ?
                """,
                (ticker_limpo, val, taxa_limpa, agora, ativo_id),
            )
            acao = "atualizado"
        else:
            cur = c.execute(
                """
                INSERT INTO carteira_real (instituicao, tipo, nome, ticker, valor_atual, taxa_info, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (inst_limpa, tipo_norm, nome_limpo, ticker_limpo, val, taxa_limpa, agora),
            )
            ativo_id = int(cur.lastrowid or 0)
            acao = "adicionado"

    return {
        "ok": True,
        "acao": acao,
        "ativo": {
            "id": ativo_id,
            "instituicao": inst_limpa,
            "tipo": tipo_norm,
            "tipo_label": ROTULOS_TIPO_REAL.get(tipo_norm, tipo_norm),
            "nome": nome_limpo,
            "ticker": ticker_limpo,
            "valor_atual": val,
            "taxa_info": taxa_limpa,
            "atualizado_em": agora,
        },
    }


def salvar_ativo_real(
    tipo: str,
    nome: str,
    valor_atual: float,
    ticker: str = "",
    taxa_info: str = "",
) -> str:
    """Adiciona ou atualiza um ativo ou saldo na Carteira Real (Mercado Pago)."""
    res = salvar_ativo_real_estruturado(tipo, nome, valor_atual, ticker=ticker, taxa_info=taxa_info)
    if not res["ok"]:
        return f"Erro ao salvar ativo real: {res['erro']}"
    a = res["ativo"]
    extra = f" | {a['taxa_info']}" if a["taxa_info"] else ""
    tk = f" ({a['ticker']})" if a["ticker"] else ""
    return f"Ativo real {res['acao']}: [{a['tipo_label']}] {a['nome']}{tk} = R$ {a['valor_atual']:.2f}{extra} ({a['instituicao']})."


def obter_painel_carteira_real(forcar: bool = False) -> dict:
    """Retorna estrutura completa da Carteira Real (ativos, BCB Selic/CDI, extrato e propostas)."""
    bcb = obter_dados_selic_cdi_bcb(forcar=forcar)
    with _conn() as c:
        ativos_rows = c.execute(
            "SELECT id, instituicao, tipo, nome, ticker, valor_atual, taxa_info, atualizado_em "
            "FROM carteira_real ORDER BY valor_atual DESC, id DESC"
        ).fetchall()
        mov_rows = c.execute(
            "SELECT id, data, descricao, valor, categoria, origem "
            "FROM extrato_movimentos ORDER BY id DESC LIMIT 30"
        ).fetchall()
        tot_mov = c.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN valor >= 0 THEN valor ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN valor < 0 THEN ABS(valor) ELSE 0 END), 0)
            FROM extrato_movimentos
            """
        ).fetchone()
        prop_rows = c.execute(
            """
            SELECT id, ativo, tipo, valor_sugerido, risco, motivo, comparacao_cdi, status, criado_em
            FROM propostas_investimento
            ORDER BY CASE WHEN status = 'pendente' THEN 0 ELSE 1 END, id DESC
            LIMIT 40
            """
        ).fetchall()

    patrimonio_total = round(sum(float(r[5]) for r in ativos_rows), 2)
    por_cat_val: dict[str, float] = {}
    ativos = []
    base_cdi = 0.0

    for r in ativos_rows:
        aid, inst, tipo, nome, ticker, val, taxa, att = r
        val_f = round(float(val), 2)
        pct = round((val_f / patrimonio_total) * 100.0, 1) if patrimonio_total > 0 else 0.0
        por_cat_val[tipo] = round(por_cat_val.get(tipo, 0.0) + val_f, 2)
        if tipo in ("conta_cdi", "renda_fixa"):
            base_cdi += val_f
        ativos.append({
            "id": int(aid),
            "instituicao": inst or "Mercado Pago",
            "tipo": tipo,
            "tipo_label": ROTULOS_TIPO_REAL.get(tipo, tipo),
            "nome": nome,
            "ticker": ticker or "",
            "valor_atual": val_f,
            "pct": pct,
            "taxa_info": taxa or "",
            "atualizado_em": att or "",
        })

    alocacao = [
        {
            "tipo": tp,
            "label": ROTULOS_TIPO_REAL.get(tp, tp),
            "valor": val_c,
            "pct": round((val_c / patrimonio_total) * 100.0, 1) if patrimonio_total > 0 else 0.0,
        }
        for tp, val_c in sorted(por_cat_val.items(), key=lambda x: x[1], reverse=True)
    ]

    cdi_m = float(bcb["cdi_mensal"]) / 100.0
    cdi_a = float(bcb["cdi_anual"]) / 100.0
    rend_cdi_mensal = round(base_cdi * cdi_m, 2)
    rend_cdi_anual = round(base_cdi * cdi_a, 2)

    pendentes = []
    historico_prop = []
    for r in prop_rows:
        item = {
            "id": int(r[0]),
            "ativo": r[1],
            "tipo": r[2],
            "tipo_label": ROTULOS_TIPO_REAL.get(r[2], r[2]),
            "valor_sugerido": round(float(r[3]), 2),
            "risco": r[4],
            "motivo": r[5],
            "comparacao_cdi": r[6],
            "status": r[7],
            "criado_em": r[8],
        }
        if r[7] == "pendente":
            pendentes.append(item)
        else:
            historico_prop.append(item)

    movimentos = [
        {
            "id": int(r[0]),
            "data": r[1],
            "descricao": r[2],
            "valor": round(float(r[3]), 2),
            "categoria": r[4],
            "origem": r[5],
        }
        for r in mov_rows
    ]

    entradas_tot = round(float(tot_mov[1] or 0.0), 2)
    saidas_tot = round(float(tot_mov[2] or 0.0), 2)

    return {
        "patrimonio_real": patrimonio_total,
        "base_cdi": round(base_cdi, 2),
        "rendimento_cdi_mensal_est": rend_cdi_mensal,
        "rendimento_cdi_anual_est": rend_cdi_anual,
        "bcb": bcb,
        "ativos": ativos,
        "alocacao": alocacao,
        "extrato": {
            "total_lancamentos": int(tot_mov[0] or 0),
            "entradas": entradas_tot,
            "saidas": saidas_tot,
            "saldo_liquido": round(entradas_tot - saidas_tot, 2),
            "movimentos": movimentos,
        },
        "propostas_pendentes": pendentes,
        "propostas_historico": historico_prop,
    }


def ver_carteira_real() -> str:
    """Mostra patrimonio real total (Mercado Pago), % por categoria e rendimento estimado no CDI."""
    p = obter_painel_carteira_real()
    bcb = p["bcb"]
    linhas = [
        "=== CARTEIRA REAL (MERCADO PAGO) ===",
        f"Patrimonio real total: R$ {p['patrimonio_real']:.2f}",
        f"Selic/CDI BCB: Selic {bcb['selic_anual']:.2f}% a.a. | 100% CDI ~{bcb['cdi_anual']:.2f}% a.a. ({bcb['cdi_mensal']:.3f}% a.m.)",
        f"Rendimento bruto est. em Conta CDI/Renda Fixa (R$ {p['base_cdi']:.2f}): "
        f"~R$ {p['rendimento_cdi_mensal_est']:.2f}/mes | ~R$ {p['rendimento_cdi_anual_est']:.2f}/ano",
    ]
    if not p["ativos"]:
        linhas.append("Ativos cadastrados: (nenhum ativo real cadastrado ainda)")
    else:
        linhas.append("\nAlocacao por categoria:")
        for al in p["alocacao"]:
            linhas.append(f"  - {al['label']}: R$ {al['valor']:.2f} ({al['pct']:.1f}%)")
        linhas.append("\nAtivos detalhados:")
        for a in p["ativos"]:
            tk = f" ({a['ticker']})" if a["ticker"] else ""
            tx = f" | {a['taxa_info']}" if a["taxa_info"] else ""
            linhas.append(f"  - [{a['tipo_label']}] {a['nome']}{tk}: R$ {a['valor_atual']:.2f} ({a['pct']:.1f}%){tx} [{a['instituicao']}]")

    ext = p["extrato"]
    if ext["total_lancamentos"] > 0:
        linhas.append(
            f"\nExtrato importado ({ext['total_lancamentos']} lancamento(s)): "
            f"Entradas R$ {ext['entradas']:.2f} | Saidas R$ {ext['saidas']:.2f} | Liquido R$ {ext['saldo_liquido']:+.2f}"
        )

    if p["propostas_pendentes"]:
        linhas.append(f"\nPropostas pendentes de aprovacao: {len(p['propostas_pendentes'])}")
        for pr in p["propostas_pendentes"][:5]:
            linhas.append(f"  - #{pr['id']} {pr['ativo']} ({pr['tipo_label']}): R$ {pr['valor_sugerido']:.2f} | Risco: {pr['risco']}")
    return "\n".join(linhas)


def propor_investimento(
    ativo: str,
    tipo: str,
    valor_sugerido: float,
    risco: str,
    motivo: str,
    comparacao_cdi: str,
) -> str:
    """Registra uma proposta PENDENTE de investimento (NUNCA executa operacoes sem aprovacao humana)."""
    ativo_limpo = (ativo or "").strip()
    if not ativo_limpo:
        return "Erro: informe o nome/ticker do ativo sugerido."
    try:
        val = round(float(valor_sugerido), 2)
    except (TypeError, ValueError):
        return "Erro: valor_sugerido invalido."
    if val <= 0:
        return "Erro: valor_sugerido deve ser maior que zero."

    tipo_norm = _normalizar_tipo_real(tipo)
    risco_limpo = (risco or "Moderado").strip()
    motivo_limpo = (motivo or "").strip()
    comp_limpa = (comparacao_cdi or "").strip()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")

    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO propostas_investimento
                (ativo, tipo, valor_sugerido, risco, motivo, comparacao_cdi, status, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, 'pendente', ?)
            """,
            (ativo_limpo, tipo_norm, val, risco_limpo, motivo_limpo, comp_limpa, agora),
        )
        prop_id = int(cur.lastrowid or 0)

    return (
        f"Proposta #{prop_id} registrada como PENDENTE de aprovacao: "
        f"{ativo_limpo} ({ROTULOS_TIPO_REAL.get(tipo_norm, tipo_norm)}) — R$ {val:.2f} | Risco: {risco_limpo}. "
        "Nenhuma movimentacao financeira e executada automaticamente: aprove ou recuse na aba 'Carteira' da Athena "
        "e, se aprovar, execute o aporte diretamente no app do Mercado Pago/corretora."
    )


def responder_proposta(proposta_id: int, aprovar: bool) -> dict:
    """Aprova ou recusa uma proposta pendente. Se aprovada, atualiza carteira_real e registra em 03-Financas/Aportes.md."""
    with _conn() as c:
        row = c.execute(
            """
            SELECT id, ativo, tipo, valor_sugerido, risco, motivo, comparacao_cdi, status, criado_em
            FROM propostas_investimento WHERE id = ?
            """,
            (int(proposta_id),),
        ).fetchone()
        if not row:
            return {"ok": False, "erro": f"Proposta #{proposta_id} nao encontrada."}

        pid, ativo, tipo, valor_sug, risco, motivo, comp_cdi, status, criado_em = row
        if status != "pendente":
            return {"ok": False, "erro": f"Proposta #{pid} ja foi processada (status: {status})."}

        novo_status = "aprovada" if aprovar else "recusada"
        c.execute("UPDATE propostas_investimento SET status = ? WHERE id = ?", (novo_status, pid))

        if not aprovar:
            return {"ok": True, "status": "recusada", "mensagem": f"Proposta #{pid} ({ativo}) recusada."}

        # Atualiza ou cria ativo na carteira_real somando o aporte aprovado
        agora = datetime.now().strftime("%Y-%m-%d %H:%M")
        existente = c.execute(
            "SELECT id, valor_atual FROM carteira_real WHERE tipo = ? AND LOWER(nome) = LOWER(?)",
            (tipo, ativo),
        ).fetchone()
        if existente:
            novo_val = round(float(existente[1]) + float(valor_sug), 2)
            c.execute(
                "UPDATE carteira_real SET valor_atual = ?, atualizado_em = ? WHERE id = ?",
                (novo_val, agora, int(existente[0])),
            )
        else:
            ticker_cand = ativo.upper() if re.match(r"^[A-Za-z0-9.-]{3,12}$", ativo) else ""
            c.execute(
                """
                INSERT INTO carteira_real (instituicao, tipo, nome, ticker, valor_atual, taxa_info, atualizado_em)
                VALUES ('Mercado Pago', ?, ?, ?, ?, ?, ?)
                """,
                (tipo, ativo, ticker_cand, round(float(valor_sug), 2), f"Aporte #{pid}", agora),
            )

    # Anexa registro da tese no Obsidian (03-Financas/Aportes.md)
    hoje_iso = datetime.now().strftime("%Y-%m-%d")
    bloco_md = (
        f"## [{hoje_iso}] Aporte Aprovado #{pid} — {ativo}\n"
        f"- **Categoria:** {ROTULOS_TIPO_REAL.get(tipo, tipo)} (`{tipo}`)\n"
        f"- **Valor sugerido:** R$ {float(valor_sug):.2f}\n"
        f"- **Risco:** {risco}\n"
        f"- **Comparação com CDI:** {comp_cdi}\n"
        f"- **Tese / Motivo:** {motivo}\n"
        f"- **Tags:** #financas #aporte #{tipo}\n"
    )
    status_obs = escrever_nota_vault(NOTA_OBSIDIAN_APORTES, bloco_md, anexar=True)

    return {
        "ok": True,
        "status": "aprovada",
        "mensagem": f"Proposta #{pid} ({ativo} · R$ {float(valor_sug):.2f}) aprovada, registrada na Carteira Real e salva no Obsidian.",
        "obsidian": status_obs,
    }