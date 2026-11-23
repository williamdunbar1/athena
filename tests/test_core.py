"""Testes deterministicos offline da Athena (zero chamadas a API do Gemini, zero navegador).

Cobre:
1. Roteador Auto, Circuit Breaker (`MODEL_COOLDOWN`) e selecao manual forcada em `agentes.py`.
2. Organizacao, propostas em lote e seguranca em `ferramentas_faxina.py` usando `tmp_path`.
3. Solicitacoes pendentes, whitelist ("Sempre permitir") e bloqueio destrutivo em `ferramentas_windows.py`.
"""

from __future__ import annotations

import os
import sqlite3

os.environ.setdefault("GOOGLE_API_KEY", "test-dummy-key")
os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")

import pytest

import agentes
import ferramentas_faxina as fx
import ferramentas_windows as fw


class _ErroFakeAPI(Exception):
    def __init__(self, msg: str, status_code: int | None = None):
        super().__init__(msg)
        self.status_code = status_code


@pytest.fixture(autouse=True)
def _limpar_cooldown_agentes():
    """Garante isolamento total do Circuit Breaker entre testes."""
    agentes.MODEL_COOLDOWN.clear()
    agentes.MODEL_COOLDOWN_MOTIVO.clear()
    yield
    agentes.MODEL_COOLDOWN.clear()
    agentes.MODEL_COOLDOWN_MOTIVO.clear()


# ==============================================================================
# 1. ROTEADOR AUTO, CIRCUIT BREAKER E MODO MANUAL (agentes.py)
# ==============================================================================


def test_auto_router_complexidade_basica(monkeypatch):
    monkeypatch.setattr(agentes, "PERMITIR_NEON", True)

    modelo_simples, _ = agentes.escolher_modelo_automatico("oi, teste")
    assert modelo_simples == "gemini-3.5-flash-lite"

    modelo_medio, _ = agentes.escolher_modelo_automatico("faça um resumo e plano de estudos da aula")
    assert modelo_medio == "gemini-4-hydrogen"

    modelo_complexo, _ = agentes.escolher_modelo_automatico("faça uma analise profunda da arquitetura do projeto")
    assert modelo_complexo == "gemini-4-neon"


def test_circuit_breaker_429_pula_para_modelos_gratuitos(monkeypatch):
    monkeypatch.setattr(agentes, "PERMITIR_NEON", True)

    # Simula 429 RESOURCE_EXHAUSTED no Neon e no Hydrogen
    err_429 = _ErroFakeAPI("429 RESOURCE_EXHAUSTED: Quota exceeded for free_tier", status_code=429)
    eleg_neon, cd_neon, _ = agentes.registrar_cooldown_modelo("gemini-4-neon", err_429)
    eleg_hyd, cd_hyd, _ = agentes.registrar_cooldown_modelo("gemini-4-hydrogen", err_429)

    assert eleg_neon is True and cd_neon == float(agentes.COOLDOWN_COTA_SEGUNDOS)
    assert eleg_hyd is True and cd_hyd == float(agentes.COOLDOWN_COTA_SEGUNDOS)
    assert agentes.modelo_disponivel("gemini-4-neon") is False
    assert agentes.modelo_disponivel("gemini-4-hydrogen") is False

    # O roteador Auto deve pular imediatamente para gemini-3.8-flash -> gemini-3.5-flash-lite
    fila, _motivo = agentes.obter_fila_modelos_auto("analise profunda e comparativo detalhado")
    assert fila == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    assert "gemini-4-neon" not in fila
    assert "gemini-4-hydrogen" not in fila

    # Enquanto isso, a selecao manual continua forcando a string direta do modelo escolhido (sem FallbackModel)
    mod_manual_hyd = agentes.obter_modelo("gemini-4-hydrogen", usar_fallback=False)
    mod_manual_neon = agentes.obter_modelo("gemini-4-neon", usar_fallback=False)
    assert mod_manual_hyd == "google:gemini-4-hydrogen"
    assert mod_manual_neon == "google:gemini-4-neon"
    assert isinstance(mod_manual_hyd, str)
    assert isinstance(mod_manual_neon, str)


def test_circuit_breaker_503_cooldown_curto_e_recuperacao_automatica():
    err_503 = _ErroFakeAPI("503 UNAVAILABLE: The model is overloaded", status_code=503)

    eleg1, cd1, _ = agentes.registrar_cooldown_modelo("gemini-3.5-flash-lite", err_503)
    eleg2, cd2, _ = agentes.registrar_cooldown_modelo("gemini-3.8-flash", err_503)
    assert eleg1 is True and cd1 == float(agentes.COOLDOWN_503_SEGUNDOS)
    assert eleg2 is True and cd2 == float(agentes.COOLDOWN_503_SEGUNDOS)
    assert agentes.modelo_disponivel("gemini-3.5-flash-lite") is False
    assert agentes.modelo_disponivel("gemini-3.8-flash") is False

    # Protecao contra "todos gratuitos em cooldown por 503": deve limpar automaticamente os 503
    fila, _ = agentes.obter_fila_modelos_auto("oi")
    assert "gemini-3.5-flash-lite" in fila
    assert "gemini-3.8-flash" in fila
    assert agentes.modelo_disponivel("gemini-3.5-flash-lite") is True


def test_especialistas_usam_fallback_model_contra_503():
    from pydantic_ai.models.fallback import FallbackModel

    for nome in (
        "agente_faxineiro",
        "agente_pesquisador",
        "agente_estudos",
        "agente_investimentos",
        "agente_sistema",
        "agente_google",
        "agente_analista",
    ):
        mod = agentes.obter_modelo_especialista(nome, "gemini-3.5-flash-lite", usar_fallback=True)
        assert isinstance(mod, FallbackModel), f"{nome} deveria usar FallbackModel contra 503"
        nomes_modelos = [m.model_name for m in mod.models]
        assert "gemini-3.5-flash-lite" in nomes_modelos
        assert "gemini-3.8-flash" in nomes_modelos
        assert "gemini-4-hydrogen" in nomes_modelos

    # Se Flash-Lite estiver em cooldown por 503, especialistas comecam direto por gemini-3.8-flash
    err_503 = _ErroFakeAPI("503 UNAVAILABLE", status_code=503)
    agentes.registrar_cooldown_modelo("gemini-3.5-flash-lite", err_503)
    mod_fax = agentes.obter_modelo_especialista("agente_faxineiro", "gemini-3.5-flash-lite", usar_fallback=True)
    assert isinstance(mod_fax, FallbackModel)
    assert mod_fax.models[0].model_name == "gemini-3.8-flash"

    # Modo manual no agente_analista continua forcando a string direta
    mod_manual = agentes.obter_modelo_especialista("agente_analista", "gemini-4-hydrogen", usar_fallback=False)
    assert mod_manual == "google:gemini-4-hydrogen"


# ==============================================================================
# 2. AGENTE FAXINEIRO OFFLINE COM tmp_path (ferramentas_faxina.py)
# ==============================================================================


@pytest.fixture
def ambiente_faxina(tmp_path, monkeypatch):
    fake_desktop = tmp_path / "Desktop"
    fake_desktop.mkdir()
    fake_db = tmp_path / "faxina_test.db"

    monkeypatch.setattr(fx, "DB_PATH", fake_db)
    # Libera apenas segmentos ancestrais do proprio tmp_path neste fixture isolado (mantendo desktop.ini, .env, .git, etc. bloqueados)
    partes_tmp = {p.lower() for p in tmp_path.resolve().parts}
    monkeypatch.setattr(fx, "ITENS_PROIBIDOS", fx.ITENS_PROIBIDOS - partes_tmp)
    monkeypatch.setattr(fx, "PASTAS_PERMITIDAS", {"desktop": fake_desktop.resolve()})
    monkeypatch.setattr(fx, "PASTA_EXTRA_AUTORIZADA", None)
    monkeypatch.setattr(fx, "obter_pasta_desktop", lambda: fake_desktop.resolve())
    monkeypatch.setattr(fx, "_candidatos_desktop", lambda: [fake_desktop.resolve()])
    monkeypatch.setattr(fx, "_listar_icones_desktop_publico", lambda: [])

    # Cria arquivos e atalhos .lnk simulados
    (fake_desktop / "relatorio.pdf").write_bytes(b"%PDF-1.4 fake")
    (fake_desktop / "foto.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (fake_desktop / "Brave.lnk").write_bytes(b"LNK-BRAVE")
    (fake_desktop / "VS Code.lnk").write_bytes(b"LNK-VSCODE")
    sub_atalhos = fake_desktop / "Atalhos" / "Apps"
    sub_atalhos.mkdir(parents=True)
    (sub_atalhos / "Spotify.lnk").write_bytes(b"LNK-SPOTIFY")

    return fake_desktop, fake_db


def test_faxina_listar_subpastas_e_propor_em_lote_sem_apagar(ambiente_faxina):
    fake_desktop, fake_db = ambiente_faxina

    listagem = fx.listar_itens_pasta("Desktop\\Atalhos", recursivo=True)
    assert "Spotify.lnk" in listagem
    assert "[ATALHO]" in listagem

    # Propoe mover tanto da raiz para subpasta quanto de subpasta de volta para raiz
    res = fx.propor_acoes_em_lote([
        {
            "tipo": "mover",
            "origem": str(fake_desktop / "Brave.lnk"),
            "destino": str(fake_desktop / "Atalhos" / "Apps" / "Brave.lnk"),
            "motivo": "Agrupar atalho",
        },
        {
            "tipo": "mover",
            "origem": str(fake_desktop / "Atalhos" / "Apps" / "Spotify.lnk"),
            "destino": str(fake_desktop / "Spotify.lnk"),
            "motivo": "Puxar app frequente para a raiz",
        },
        {
            "tipo": "lixeira",
            "origem": str(fake_desktop / "foto.png"),
            "motivo": "Arquivo temporario",
        },
    ])

    assert "Registradas em 1 transacao: 3" in res
    assert "Bloqueadas/invalidas: 0" in res

    # Nenhum arquivo deve ter sido movido ou apagado antes da aprovacao humana
    assert (fake_desktop / "Brave.lnk").is_file()
    assert (fake_desktop / "Atalhos" / "Apps" / "Spotify.lnk").is_file()
    assert (fake_desktop / "foto.png").is_file()

    with sqlite3.connect(fake_db) as c:
        rows = c.execute("SELECT tipo, status FROM acoes_faxina ORDER BY id ASC").fetchall()
    assert rows == [("mover", "pendente"), ("mover", "pendente"), ("lixeira", "pendente")]


def test_faxina_nunca_apaga_permanentemente_e_usa_send2trash(ambiente_faxina, monkeypatch):
    fake_desktop, fake_db = ambiente_faxina
    enviados_lixeira: list[str] = []

    def _fake_send2trash(caminho: str) -> None:
        enviados_lixeira.append(caminho)

    monkeypatch.setattr(fx, "send2trash", _fake_send2trash)

    fx.propor_acoes_em_lote([
        {
            "tipo": "lixeira",
            "origem": str(fake_desktop / "foto.png"),
            "motivo": "Teste lixeira segura",
        },
        {
            "tipo": "mover",
            "origem": str(fake_desktop / "relatorio.pdf"),
            "destino": str(fake_desktop / "PDFs" / "relatorio.pdf"),
            "motivo": "Organizar PDF",
        },
    ])

    res_aprov = fx.aprovar_todas_pendentes()
    assert res_aprov["ok"] is True
    assert res_aprov["executadas"] == 2
    # send2trash foi chamado em vez de exclusao permanente
    assert len(enviados_lixeira) == 1
    assert enviados_lixeira[0].endswith("foto.png")
    # PDF foi movido para subpasta PDFs sem perda
    assert (fake_desktop / "PDFs" / "relatorio.pdf").is_file()
    assert not (fake_desktop / "relatorio.pdf").exists()


def test_faxina_bloqueia_itens_proibidos(ambiente_faxina):
    fake_desktop, _ = ambiente_faxina
    (fake_desktop / "desktop.ini").write_text("[.ShellClassInfo]", encoding="utf-8")
    (fake_desktop / ".env").write_text("SECRET=123", encoding="utf-8")

    res = fx.propor_acoes_em_lote([
        {"tipo": "lixeira", "origem": str(fake_desktop / "desktop.ini"), "motivo": "Proibido"},
        {"tipo": "lixeira", "origem": str(fake_desktop / ".env"), "motivo": "Proibido"},
    ])
    assert "Registradas em 1 transacao: 0" in res
    assert "Bloqueadas/invalidas: 2" in res
    assert (fake_desktop / "desktop.ini").is_file()
    assert (fake_desktop / ".env").is_file()


# ==============================================================================
# 3. EXECUCAO NATIVA WINDOWS COM PERMISSAO E WHITELIST (ferramentas_windows.py)
# ==============================================================================


@pytest.fixture
def ambiente_windows(tmp_path, monkeypatch):
    fake_db = tmp_path / "windows_test.db"
    fake_app = tmp_path / "FakeBrave.exe"
    fake_app.write_bytes(b"MZ-FAKE-EXE")
    executados: list[tuple[str, str, str]] = []

    monkeypatch.setattr(fw, "DB_PATH", fake_db)
    monkeypatch.setattr(
        fw,
        "_executar_acao_no_windows",
        lambda tipo, alvo, args="": executados.append((tipo, alvo, args)),
    )
    return tmp_path, fake_app, fake_db, executados


def test_windows_sem_whitelist_cria_pendente_e_nunca_executa_direto(ambiente_windows):
    _tmp, fake_app, fake_db, executados = ambiente_windows

    res_app = fw.solicitar_abrir_app_dados(str(fake_app))
    assert res_app["ok"] is True
    assert res_app["auto_executada"] is False
    assert res_app["acao"]["status"] == "pendente"
    assert res_app["acao"]["tipo"] == "abrir_app"
    assert executados == []

    res_url = fw.solicitar_abrir_url_dados("https://github.com/pydantic/pydantic-ai", navegador="padrao")
    assert res_url["ok"] is True
    assert res_url["auto_executada"] is False
    assert res_url["acao"]["status"] == "pendente"
    assert res_url["acao"]["tipo"] == "abrir_url"
    assert executados == []

    # Recusar marca como recusada sem executar
    res_rec = fw.recusar_acao_windows(res_url["acao"]["id"])
    assert res_rec["ok"] is True
    assert res_rec["acao"]["status"] == "recusada"
    assert executados == []

    # Aprovar com sempre_permitir=True executa e grava na whitelist
    res_ap = fw.aprovar_acao_windows(res_app["acao"]["id"], sempre_permitir=True)
    assert res_ap["ok"] is True
    assert res_ap["acao"]["status"] == "executada"
    assert res_ap["sempre_permitir"] is True
    assert len(executados) == 1

    # Proxima chamada ao mesmo app na whitelist abre automaticamente
    res_auto = fw.solicitar_abrir_app_dados(str(fake_app))
    assert res_auto["ok"] is True
    assert res_auto["auto_executada"] is True
    assert res_auto["acao"]["status"] == "executada"
    assert len(executados) == 2

    # Gerenciamento da whitelist (listar + remover)
    wl = fw.listar_whitelist()
    assert any(item["item"] == res_app["acao"]["chave_whitelist"] for item in wl)
    rem = fw.remover_whitelist(res_app["acao"]["chave_whitelist"])
    assert rem["ok"] is True
    assert fw.esta_na_whitelist(res_app["acao"]["chave_whitelist"]) is False


@pytest.mark.parametrize(
    "comando_perigoso",
    [
        "format C:",
        "del C:\\arquivo.txt",
        "rd /s /q C:\\teste",
        "reg delete HKCU\\Software\\Test",
        "shutdown /s /t 0",
        "powershell",
        "cmd.exe",
    ],
)
def test_windows_bloqueia_comandos_destrutivos(ambiente_windows, comando_perigoso):
    _tmp, _fake_app, _fake_db, executados = ambiente_windows
    res = fw.solicitar_abrir_app_dados(comando_perigoso)
    assert res["ok"] is False
    assert "bloqueado" in res["erro"].lower()
    assert executados == []


def test_windows_bloqueia_injecao_args_e_urls_perigosas(ambiente_windows):
    _tmp, fake_app, _fake_db, executados = ambiente_windows

    res_inj = fw.solicitar_abrir_app_dados(str(fake_app), args="--new-tab && shutdown /s")
    assert res_inj["ok"] is False
    assert "bloqueado" in res_inj["erro"].lower()

    for url_ruim in ("javascript:alert(1)", "file:///C:/Windows/win.ini", "data:text/html,<h1>x</h1>"):
        res_u = fw.solicitar_abrir_url_dados(url_ruim, navegador="padrao")
        assert res_u["ok"] is False

    assert executados == []


# ==============================================================================
# 4. BUSCA HIBRIDA QDRANT (DENSA + BM25 + RRF + RERANKER) OFFLINE (rag_qdrant.py)
# ==============================================================================


def test_rag_busca_hibrida_termo_exato_e_semantica(tmp_path, monkeypatch):
    """Valida busca hibrida (dense + bm25 + RRF + reranker) usando Qdrant ':memory:' sem baixar ONNX."""
    import re

    from qdrant_client import QdrantClient, models

    import rag_qdrant as rq

    # Vocabulario deterministico p/ testar BM25 (exato) + Denso (semantico) de forma 100% offline
    def _fake_dense(texto: str) -> list[float]:
        low = texto.lower()
        # Eixo 0: fisica/vetores/mecanica | Eixo 1: calculo/matrizes | Eixo 2: financas/cdi | Eixo 3: neutro
        if any(k in low for k in ("vetor", "forca", "mecanica", "velocidade", "cinematica", "movimento")):
            return [0.95, 0.05, 0.0, 0.05]
        if any(k in low for k in ("matriz", "determinante", "derivada", "integral")):
            return [0.05, 0.95, 0.0, 0.05]
        if any(k in low for k in ("cdi", "selic", "carteira", "investimento", "rendimento")):
            return [0.0, 0.05, 0.95, 0.05]
        return [0.25, 0.25, 0.25, 0.25]

    vocab_esparso: dict[str, int] = {}

    def _fake_bm25(texto: str) -> models.SparseVector:
        tokens = re.findall(r"[a-z0-9_-]{2,}", texto.lower())
        contagem: dict[int, float] = {}
        for tok in tokens:
            idx = vocab_esparso.setdefault(tok, len(vocab_esparso) + 1)
            contagem[idx] = contagem.get(idx, 0.0) + 1.0
        pares = sorted(contagem.items())
        return models.SparseVector(
            indices=[i for i, _ in pares],
            values=[v for _, v in pares],
        )

    class _FakeReranker:
        def rerank(self, query: str, documents: list[str]) -> list[float]:
            q_toks = set(re.findall(r"[a-z0-9_-]{2,}", query.lower()))
            scores = []
            for doc in documents:
                d_toks = set(re.findall(r"[a-z0-9_-]{2,}", doc.lower()))
                sobreposicao = len(q_toks & d_toks)
                # Da boost semantico quando consulta e documento estao no mesmo dominio
                qd = _fake_dense(query)
                dd = _fake_dense(doc)
                cos = sum(a * b for a, b in zip(qd, dd, strict=False))
                scores.append(float(sobreposicao * 2.0 + cos))
            return scores

    client_mem = QdrantClient(":memory:")
    try:
        fake_db = tmp_path / "rag_test.db"
        monkeypatch.setattr(rq, "DB_PATH", fake_db)
        monkeypatch.setattr(rq, "_CLIENT", client_mem)
        monkeypatch.setattr(rq, "_dimensao_densa", lambda _c: 4)
        monkeypatch.setattr(
            rq,
            "_gerar_vetores_documentos",
            lambda textos: ([_fake_dense(t) for t in textos], [_fake_bm25(t) for t in textos]),
        )
        monkeypatch.setattr(
            rq,
            "_gerar_vetores_consulta",
            lambda q: (_fake_dense(q), _fake_bm25(q)),
        )
        monkeypatch.setattr(rq, "_obter_modelo_reranker", lambda: _FakeReranker())

        # 1) Testa migracao automatica de collection legada (vetor unico sem nome) para schema hibrido
        client_mem.create_collection(
            collection_name=rq.COLECAO_ATHENA,
            vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE),
        )
        assert rq._colecao_tem_schema_hibrido(client_mem) is False
        migrada = rq._garantir_colecao(client_mem, forcar_migracao=True)
        assert migrada is True
        assert rq._colecao_tem_schema_hibrido(client_mem) is True

        # 2) Upsert de 3 documentos com vetores nomeados dense + bm25
        docs_meta = [
            {
                "fonte": "pdf",
                "materia": "fisica_teorica",
                "arquivo": "Aula_03_Erros_Incertezas.pdf",
                "pagina": 4,
                "texto": "Codigo tecnico NBR-5410 e formula de propagacao de incerteza padrao em medicoes.",
            },
            {
                "fonte": "pdf",
                "materia": "fisica_pratica",
                "arquivo": "Aula_05_Vetores.pdf",
                "pagina": 2,
                "texto": "Estudo de cinematica, velocidade escalar, movimento retilineo e decomposicao de forca em vetores.",
            },
            {
                "fonte": "obsidian",
                "materia": "03-Financas",
                "arquivo": "03-Financas/Aportes.md",
                "pagina": None,
                "texto": "Comparativo de rendimento da carteira real em conta CDI versus taxa Selic do Banco Central.",
            },
        ]
        pontos = rq._montar_pontos_hibridos("pdf:teste/lote", docs_meta)
        rq._upsert_em_lotes(client_mem, pontos)

        # 3) Consulta por codigo tecnico exato (BM25 + RRF + Reranker)
        res_exato, usou_rr, modo = rq._consultar_hibrido_no_cliente(client_mem, "NBR-5410", top_k=2)
        assert usou_rr is True and modo == "hibrido+rerank"
        assert res_exato[0][0].payload["arquivo"] == "Aula_03_Erros_Incertezas.pdf"

        # 4) Consulta semantica sem repetir palavras exatas do texto (ex.: "mecanica" -> encontra cinematica/vetores)
        res_sem, _, _ = rq._consultar_hibrido_no_cliente(client_mem, "mecanica", top_k=2)
        assert res_sem[0][0].payload["arquivo"] == "Aula_05_Vetores.pdf"

        # 5) Fallback seguro por RRF quando o reranker nao estiver disponivel
        monkeypatch.setattr(rq, "_obter_modelo_reranker", lambda: None)
        res_rrf, usou_rr_fb, modo_fb = rq._consultar_hibrido_no_cliente(client_mem, "NBR-5410", top_k=2)
        assert usou_rr_fb is False and modo_fb == "hibrido+rrf"
        assert res_rrf[0][0].payload["arquivo"] == "Aula_03_Erros_Incertezas.pdf"
        assert res_rrf[0][2] == "rrf"
    finally:
        client_mem.close()


# ==============================================================================
# 5. LOGFIRE MCP + TOOLS DIRETAS + CLASSIFICACAO DE ERROS (NAO MASCARAR 503)
# ==============================================================================


def test_mcp_logfire_removido_e_sem_vazar_token(monkeypatch, tmp_path):
    import json

    import mcp_manager as mm

    # O servidor MCP remoto do Logfire foi removido de mcp_servers.json (mesmo com LOGFIRE_READ_TOKEN definido)
    monkeypatch.setenv("LOGFIRE_READ_TOKEN", "dummy-logfire-token")
    toolsets_padrao = mm.carregar_servidores_mcp(recarregar=True)
    assert not any("logfire" in (ts.id or "").lower() for ts in toolsets_padrao)
    st_padrao = {item["nome"]: item for item in mm.listar_servidores_mcp()}
    assert "logfire" not in st_padrao
    assert "logfire" not in mm.CONFIG_PATH.read_text(encoding="utf-8").lower()
    assert "dummy-logfire-token" not in str(st_padrao)

    # Verifica que servidores HTTP condicionais em geral ainda expandem Bearer em memoria sem vazar
    cfg_tmp = tmp_path / "mcp_servers_test.json"
    cfg_tmp.write_text(
        json.dumps(
            {
                "servidores": [
                    {
                        "nome": "exemplo-seguro",
                        "ativo": True,
                        "opcional_sem_env": True,
                        "requer_env": ["MCP_TESTE_TOKEN"],
                        "transporte": "http",
                        "url": "https://exemplo.invalid/mcp",
                        "headers": {"Authorization": "Bearer ${MCP_TESTE_TOKEN}"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mm, "CONFIG_PATH", cfg_tmp)

    monkeypatch.delenv("MCP_TESTE_TOKEN", raising=False)
    assert mm.carregar_servidores_mcp(recarregar=True) == []

    token_fake = "dummy-mcp-token"
    monkeypatch.setenv("MCP_TESTE_TOKEN", token_fake)
    toolsets_com = mm.carregar_servidores_mcp(recarregar=True)
    ts_ex = next((ts for ts in toolsets_com if ts.id == "mcp:exemplo-seguro"), None)
    assert ts_ex is not None
    st_com = {item["nome"]: item for item in mm.listar_servidores_mcp()}
    assert st_com["exemplo-seguro"]["status"] == "ativo"
    assert token_fake not in str(st_com)


def test_erros_mcp_tool_http_schema_nao_sao_mascarados_como_503():
    import httpx
    from pydantic_ai.exceptions import ModelHTTPError, ModelUnavailableError, ToolFailedError
    from pydantic_ai.messages import ToolReturnPart

    # Erros reais da API Gemini DEVEM ser classificados como 503/elegivel
    err_gemini_503 = ModelHTTPError(status_code=503, model_name="gemini-3.5-flash-lite", body="UNAVAILABLE")
    assert agentes.eh_erro_503(err_gemini_503) is True
    eleg, cd, motivo = agentes.classificar_erro_modelo(err_gemini_503)
    assert eleg is True and cd == float(agentes.COOLDOWN_503_SEGUNDOS) and "503" in motivo

    err_unavail = ModelUnavailableError(model_name="gemini-3.5-flash-lite", message="503 UNAVAILABLE overloaded")
    assert agentes.eh_erro_503(err_unavail) is True

    # Erros de MCP, ferramentas, httpx ou schema 400 NUNCA podem ser classificados como 503 nem acionar cooldown
    req = httpx.Request("POST", "https://logfire-us.pydantic.dev/mcp")
    resp_503 = httpx.Response(503, request=req, text="Service Unavailable")
    err_httpx_503 = httpx.HTTPStatusError("503 Service Unavailable", request=req, response=resp_503)

    err_tool = ToolFailedError(
        ToolReturnPart(
            tool_name="mcp_logfire",
            content="MCP server unavailable (503) ao consultar Logfire",
            tool_call_id="call_test_1",
        )
    )
    err_runtime_mcp = RuntimeError("MCP unavailable: failed to connect to logfire-us.pydantic.dev")
    err_schema_400 = ModelHTTPError(
        status_code=400,
        model_name="gemini-3.5-flash-lite",
        body="INVALID_ARGUMENT: function_declarations schema rejected",
    )

    for exc in (err_httpx_503, err_tool, err_runtime_mcp, err_schema_400):
        assert agentes.eh_erro_503(exc) is False, f"{type(exc).__name__} nao deveria ser 503 Gemini"
        eleg_x, cd_x, _ = agentes.registrar_cooldown_modelo("gemini-3.5-flash-lite", exc)
        assert eleg_x is False and cd_x == 0.0
        assert agentes.modelo_disponivel("gemini-3.5-flash-lite") is True


def test_ferramentas_logfire_query_client_offline(monkeypatch):
    import ferramentas_logfire as fl

    # Sem token retorna aviso amigavel sem levantar excecao
    monkeypatch.delenv("LOGFIRE_READ_TOKEN", raising=False)
    assert "LOGFIRE_READ_TOKEN" in fl.buscar_erros_logfire(minutos=60)
    assert "LOGFIRE_READ_TOKEN" in fl.consultar_trace_logfire("chat")

    consultas: list[str] = []

    class _FakeQueryClient:
        def __init__(self, read_token: str, base_url=None, timeout=None):
            self.read_token = read_token
            self.base_url = base_url

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def query_json_rows(self, sql: str, *, min_timestamp=None, limit=None, **kwargs):
            consultas.append(sql)
            if "is_exception = true" in sql:
                return {
                    "rows": [
                        {
                            "start_timestamp": "2026-11-23T14:00:00Z",
                            "service_name": "athena",
                            "span_name": "POST /api/chat-stream",
                            "message": "ToolFailedError: falha simulada",
                            "level": 17,
                            "exception_type": "ToolFailedError",
                            "exception_message": "conexao recusada",
                            "exception_stacktrace": "Traceback...\nToolFailedError: conexao recusada",
                            "trace_id": "abc1234567890def",
                            "duration": 1.25,
                        }
                    ]
                }
            return {
                "rows": [
                    {
                        "start_timestamp": "2026-11-23T14:05:00Z",
                        "service_name": "athena",
                        "span_name": "agent run",
                        "message": "chat gemini-3.5-flash-lite",
                        "level": 9,
                        "is_exception": False,
                        "exception_type": None,
                        "exception_message": None,
                        "trace_id": "fff1234567890aaa",
                        "duration": 0.42,
                        "attributes": {
                            "gen_ai.request.model": "gemini-3.5-flash-lite",
                            "gen_ai.usage.input_tokens": 120,
                            "gen_ai.usage.output_tokens": 45,
                        },
                    }
                ]
            }

    monkeypatch.setenv("LOGFIRE_READ_TOKEN", "pylf_v1_us_fake_token")
    monkeypatch.setattr(fl, "LogfireQueryClient", _FakeQueryClient)

    out_erros = fl.buscar_erros_logfire(minutos=60, filtro="chat")
    assert "[DADOS DE DIAGNOSTICO LOGFIRE" in out_erros
    assert "ToolFailedError" in out_erros
    assert "abc123456789" in out_erros
    assert "pylf_v1_us_fake_token" not in out_erros

    out_trace = fl.consultar_trace_logfire("agent run")
    assert "[DADOS DE DIAGNOSTICO LOGFIRE" in out_trace
    assert "120 in / 45 out" in out_trace
    assert "gemini-3.5-flash-lite" in out_trace
    assert "420 ms" in out_trace
    assert len(consultas) == 2


def test_ferramentas_logfire_respeitam_limite_3000_caracteres(monkeypatch):
    import ferramentas_logfire as fl

    rows_longas = [
        {
            "start_timestamp": "2026-11-23T14:00:00Z",
            "service_name": "athena",
            "span_name": f"invoke_agent_longo_{i}_" + ("x" * 120),
            "message": f"Mensagem detalhada {i}: " + ("detalhe " * 80),
            "level": 17,
            "is_exception": True,
            "exception_type": "ModelUnavailableError",
            "exception_message": "503 UNAVAILABLE " + ("high demand " * 60),
            "exception_stacktrace": "\n".join(f"File line {j}: " + ("trace " * 30) for j in range(10)),
            "trace_id": f"01a208c3601fbd{i:02d}abcdef1234567890",
            "duration": 1.23,
            "attributes": {
                "gen_ai.request.model": "gemini-4-hydrogen",
                "gen_ai.usage.input_tokens": 500,
                "gen_ai.usage.output_tokens": 120,
            },
        }
        for i in range(25)
    ]

    class _FakeVerboseClient:
        def __init__(self, read_token: str, base_url=None, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def query_json_rows(self, sql: str, *, min_timestamp=None, limit=None, **kwargs):
            return {"rows": rows_longas}

    monkeypatch.setenv("LOGFIRE_READ_TOKEN", "pylf_v1_us_fake_token")
    monkeypatch.setattr(fl, "LogfireQueryClient", _FakeVerboseClient)

    out_erros = fl.buscar_erros_logfire(minutos=1440, limite=25)
    out_trace = fl.consultar_trace_logfire("invoke_agent", minutos=1440, limite=25)
    assert len(out_erros) <= fl.MAX_CARACTERES_RETORNO
    assert len(out_trace) <= fl.MAX_CARACTERES_RETORNO
    assert "resumida em 3000 caracteres" in out_erros
    assert "resumida em 3000 caracteres" in out_trace


def test_retry_on_model_errors_configurado_nos_agentes():
    from pydantic_ai.capabilities import RetryOnModelErrors
    from pydantic_ai.exceptions import (
        ModelHTTPError,
        ModelOverloadedError,
        ModelUnavailableError,
        ToolFailedError,
    )
    from pydantic_ai.messages import ToolReturnPart

    def _extrair_retry_caps(agente) -> list[RetryOnModelErrors]:
        raiz = getattr(agente, "_root_capability", None)
        caps = getattr(raiz, "capabilities", None)
        lista = list(caps) if caps is not None else ([raiz] if raiz is not None else [])
        return [c for c in lista if isinstance(c, RetryOnModelErrors)]

    todos = [
        ("orquestrador", agentes.orquestrador, 4),
        ("agente_sistema", agentes.agente_sistema, 3),
        ("agente_pesquisador", agentes.agente_pesquisador, 3),
        ("agente_analista", agentes.agente_analista, 3),
        ("agente_estudos", agentes.agente_estudos, 3),
        ("agente_investimentos", agentes.agente_investimentos, 3),
        ("agente_faxineiro", agentes.agente_faxineiro, 3),
        ("agente_google", agentes.agente_google, 3),
    ]
    for nome, ag, esperado_attempts in todos:
        retries = _extrair_retry_caps(ag)
        assert retries, f"{nome} deveria possuir RetryOnModelErrors"
        cap = retries[0]
        assert cap.max_attempts == esperado_attempts
        assert cap.initial_delay == 2.0
        assert cap.max_delay == 6.0

    # Valida que o predicado dispara apenas em 503/ModelUnavailable/ModelOverloaded e NAO em 400 ou ToolFailedError
    cap_orq = _extrair_retry_caps(agentes.orquestrador)[0]
    handlers = cap_orq._predicates.exception_handlers

    def _bate(exc: Exception) -> bool:
        return any(bool(h(exc)) for h in handlers)

    assert _bate(ModelUnavailableError("gemini-4-hydrogen", "503")) is True
    assert _bate(ModelOverloadedError("gemini-4-hydrogen", "overloaded")) is True
    assert _bate(ModelHTTPError(503, "gemini-4-hydrogen", body="UNAVAILABLE")) is True
    assert _bate(ModelHTTPError(400, "gemini-4-hydrogen", body="INVALID_ARGUMENT")) is False
    assert _bate(
        ToolFailedError(ToolReturnPart(tool_name="buscar_erros_logfire", content="503", tool_call_id="c1"))
    ) is False


def _executar_coro_sync(coro):
    """Executa uma coroutine puramente em memoria sem abrir socketpair de event loop."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("Coroutine suspendeu inesperadamente em teste deterministico offline")


def test_auto_fallback_503_apos_retry_nao_aborta_no_flash_lite(monkeypatch, tmp_path):
    """Garante que ModelUnavailableError (503) apos retries do Flash-Lite alterna para 3.8-flash -> hydrogen."""
    from contextlib import asynccontextmanager

    from pydantic_ai.exceptions import ModelUnavailableError

    import servidor

    @asynccontextmanager
    async def _timeout_noop(_seg):
        yield

    chamados: list[str] = []

    class _FakeResult:
        def __init__(self, model_name: str, output: str):
            self.output = output
            self._model_name = model_name

        def all_messages(self):
            class _Msg:
                def __init__(self, mn):
                    self.model_name = mn

            return [_Msg(self._model_name)]

    async def _fake_run(entrada, *, deps, model, retries=0):
        chamados.append(str(model))
        if "gemini-3.5-flash-lite" in str(model):
            raise ModelUnavailableError("gemini-3.5-flash-lite", "503 UNAVAILABLE high demand")
        if "gemini-3.8-flash" in str(model):
            raise ModelUnavailableError("gemini-3.8-flash", "503 UNAVAILABLE high demand")
        return _FakeResult("gemini-4-hydrogen", "Resposta recuperada pelo Hydrogen")

    monkeypatch.setattr(servidor.asyncio, "timeout", _timeout_noop)
    monkeypatch.setattr(servidor.orquestrador, "run", _fake_run)
    ctx = agentes.ContextoMissao(pasta_raiz=tmp_path, modelo="gemini-3.5-flash-lite", usar_fallback=True)
    fila = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-4-hydrogen"]

    res, modelo_usado = _executar_coro_sync(
        servidor._executar_orquestrador_auto_ou_manual("veja erros no logfire", ctx, fila, True)
    )
    assert res.output == "Resposta recuperada pelo Hydrogen"
    assert modelo_usado == "gemini-4-hydrogen"
    assert chamados == [
        "google:gemini-3.5-flash-lite",
        "google:gemini-3.8-flash",
        "google:gemini-4-hydrogen",
    ]
    assert agentes.modelo_disponivel("gemini-3.5-flash-lite") is False
    assert agentes.modelo_disponivel("gemini-3.8-flash") is False


# ==============================================================================
# 6. AGENT SKILLS NATIVAS (skills_manager.py + skills/*/SKILL.md)
# ==============================================================================


def test_skills_descoberta_catalogo_e_carregamento():
    import skills_manager as sm

    skills = sm.listar_skills()
    nomes = {s["name"] for s in skills}
    assert {"plano-estudo-prova", "analise-financeira", "diagnostico-athena"}.issubset(nomes)
    for s in skills:
        assert s["description"]
        assert "\n" not in s["description"]

    # Catalogo ultra enxuto (1 linha por skill, sem vazar corpo completo)
    catalogo = sm.gerar_catalogo_skills()
    linhas = [ln for ln in catalogo.splitlines() if ln.strip()]
    assert len(linhas) == len(skills)
    assert "- plano-estudo-prova:" in catalogo
    assert "- analise-financeira:" in catalogo
    assert "- diagnostico-athena:" in catalogo
    assert "## 1." not in catalogo

    # Carregamento completo sob demanda
    corpo = sm.carregar_skill("diagnostico-athena")
    assert "[SKILL CARREGADA: diagnostico-athena]" in corpo
    assert "buscar_erros_logfire" in corpo
    assert "consultar_trace_logfire" in corpo

    # Skill inexistente retorna mensagem amigavel listando disponiveis
    nao_existe = sm.carregar_skill("skill-inexistente-xyz")
    assert "nao encontrada" in nao_existe.lower()
    assert "diagnostico-athena" in nao_existe


@pytest.mark.parametrize(
    "nome_malicioso",
    [
        "../.env",
        "..\\..\\Windows\\win.ini",
        "plano-estudo-prova/../../.env",
        "/etc/passwd",
        "C:\\Users\\test\\.env",
        "skill\x00invalida",
        "..",
        "   ",
    ],
)
def test_skills_bloqueia_path_traversal(nome_malicioso, tmp_path):
    import skills_manager as sm

    # Cria um arquivo sensivel fora da pasta skills para provar que nunca e lido
    segredo_fora = tmp_path / ".env"
    segredo_fora.write_text("SEGREDO_FORA=NAO_LER", encoding="utf-8")
    pasta_skills = tmp_path / "skills"
    (pasta_skills / "demo").mkdir(parents=True)
    (pasta_skills / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Skill de teste\n---\nCorpo seguro.",
        encoding="utf-8",
    )

    out = sm.carregar_skill(nome_malicioso, diretorio=pasta_skills)
    assert "SEGREDO_FORA" not in out
    assert "erro" in out.lower() or "bloqueado" in out.lower() or "invalido" in out.lower()

    # Confirma que a skill legitima no mesmo tmp_path funciona normalmente
    ok_out = sm.carregar_skill("demo", diretorio=pasta_skills)
    assert "[SKILL CARREGADA: demo]" in ok_out
    assert "Corpo seguro." in ok_out


def test_skills_registrada_no_orquestrador_e_endpoint():
    import servidor

    assert "carregar_skill" in agentes.orquestrador._function_toolset.tools
    resp = servidor.ver_skills()
    assert isinstance(resp, dict)
    nomes = {item["name"] for item in resp["skills"]}
    assert {"plano-estudo-prova", "analise-financeira", "diagnostico-athena"}.issubset(nomes)


# ==============================================================================
# 7. VOZ NEURAL OFFLINE (voz.py + endpoints /api/voz/* com mocks, sem baixar Whisper)
# ==============================================================================


def test_voz_limpar_texto_para_fala():
    import voz

    bruto = (
        "## 🚀 Resumo do Plano\n\n"
        "Confira o **guia completo** em [Obsidian Vault](https://exemplo.com/nota) "
        "ou acesse https://site.invalid/teste.\n\n"
        "```python\nprint('segredo tecnico')\n```\n\n"
        "- Item `codigo_inline` com *enfase* e ~~tachado~~ 🔥✨\n"
        "> Citacao importante\n"
    )
    limpo = voz.limpar_texto_para_fala(bruto)
    assert "```" not in limpo
    assert "segredo tecnico" not in limpo
    assert "trecho de código omitido" in limpo
    assert "Obsidian Vault" in limpo
    assert "https://" not in limpo
    assert "**" not in limpo and "~~" not in limpo and "##" not in limpo
    assert "🚀" not in limpo and "🔥" not in limpo and "✨" not in limpo
    assert "codigo inline" in limpo
    assert "Citacao importante" in limpo


def test_voz_transcrever_e_sintetizar_com_mocks_e_fallback_sem_extras(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    import voz

    caminhos_recebidos: list[Path] = []

    class _FakeWhisper:
        def transcribe(self, caminho: str, *, language: str = "pt", vad_filter: bool = True):
            p = Path(caminho)
            caminhos_recebidos.append(p)
            assert p.is_file()
            assert language == "pt"
            assert vad_filter is True
            return [SimpleNamespace(text="Ola Athena,"), SimpleNamespace(text="tudo bem?")], None

    monkeypatch.setattr(voz, "_WHISPER_MODEL", _FakeWhisper())
    texto = voz.transcrever_audio(b"WEBM-FAKE-BYTES", extensao="audio/webm;codecs=opus")
    assert texto == "Ola Athena, tudo bem?"
    # Garante que o arquivo temporario foi removido apos transcrever
    assert caminhos_recebidos and not caminhos_recebidos[0].exists()

    # Testa sintetizar_fala com mock do edge_tts sem rede
    class _FakeCommunicate:
        def __init__(self, text: str, *, voice: str):
            self.text = text
            self.voice = voice

        async def stream(self):
            assert "**" not in self.text
            assert self.voice == "pt-BR-FranciscaNeural"
            yield {"type": "audio", "data": b"ID3-MP3-CHUNK-1"}
            yield {"type": "WordBoundary", "text": "Ola"}
            yield {"type": "audio", "data": b"-CHUNK-2"}

    fake_edge_mod = SimpleNamespace(Communicate=_FakeCommunicate)
    monkeypatch.setattr(voz.importlib, "import_module", lambda nome: fake_edge_mod if nome == "edge_tts" else None)

    audio_mp3 = _executar_coro_sync(voz.sintetizar_fala("**Olá** mundo 🚀!"))
    assert audio_mp3 == b"ID3-MP3-CHUNK-1-CHUNK-2"

    # Fallback seguro (VozIndisponivelError) quando extra opcional nao esta instalado
    def _faltando(_nome: str):
        raise ModuleNotFoundError("no module")

    monkeypatch.setattr(voz, "_WHISPER_MODEL", None)
    monkeypatch.setattr(voz.importlib, "import_module", _faltando)
    with pytest.raises(voz.VozIndisponivelError):
        voz.transcrever_audio(b"abc", extensao="webm")
    with pytest.raises(voz.VozIndisponivelError):
        _executar_coro_sync(voz.sintetizar_fala("teste"))


def test_endpoints_voz_servidor_com_mocks(monkeypatch):
    from fastapi import HTTPException

    import servidor
    import voz as mod_voz

    # 1) /api/voz/transcrever com UploadFile fake + mock de asyncio.to_thread
    class _FakeUpload:
        filename = "mensagem.webm"
        content_type = "audio/webm"

        async def read(self) -> bytes:
            return b"FAKE-AUDIO-BYTES"

    async def _fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(servidor.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(servidor, "transcrever_audio", lambda dados, ext: f"transcrito({len(dados)},{ext})")

    res_stt = _executar_coro_sync(servidor.transcrever_voz(_FakeUpload()))  # type: ignore[arg-type]
    assert res_stt == {"texto": "transcrito(16,webm)"}

    # 2) /api/voz/falar retorna audio/mpeg
    async def _fake_tts(texto: str, voz: str = ""):
        assert texto == "Ola Athena"
        return b"FAKE-MP3-DATA"

    monkeypatch.setattr(servidor, "sintetizar_fala", _fake_tts)
    resp_tts = _executar_coro_sync(servidor.falar_voz(servidor.PedidoVozFalar(texto="Ola Athena")))
    assert resp_tts.media_type == "audio/mpeg"
    assert resp_tts.body == b"FAKE-MP3-DATA"

    # 3) Quando extra voice nao esta instalado -> HTTP 503 limpo (aciona fallback no front)
    async def _fake_tts_indisp(texto: str, voz: str = ""):
        raise mod_voz.VozIndisponivelError("edge-tts ausente")

    monkeypatch.setattr(servidor, "sintetizar_fala", _fake_tts_indisp)
    with pytest.raises(HTTPException) as exc_info:
        _executar_coro_sync(servidor.falar_voz(servidor.PedidoVozFalar(texto="Oi")))
    assert exc_info.value.status_code == 503


# ==============================================================================
# 8. LOGS ESTRUTURADOS (structlog + OTel) E MIGRATIONS ALEMBIC (memoria.db)
# ==============================================================================


def test_structlog_emite_json_valido_com_campos_e_otel(monkeypatch):
    import io
    import json
    from types import SimpleNamespace

    import opentelemetry.trace as otel_trace

    import logging_config as lc

    buf = io.StringIO()
    try:
        fmt = lc.configurar_logging("json", stream=buf, forcar=True)
        assert fmt == "json"

        fake_ctx = SimpleNamespace(
            is_valid=True,
            trace_id=0x1234567890ABCDEF1234567890ABCDEF,
            span_id=0x0102030405060708,
        )
        fake_span = SimpleNamespace(get_span_context=lambda: fake_ctx)
        monkeypatch.setattr(otel_trace, "get_current_span", lambda: fake_span)

        log = lc.obter_logger("teste_athena")
        log.info(
            "chat_concluido",
            conversa_id="conv-123",
            modelo="gemini-3.5-flash-lite",
            latencia_ms=412,
        )

        linhas = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(linhas) == 1
        registro = json.loads(linhas[0])
        assert registro["event"] == "chat_concluido"
        assert registro["level"] == "info"
        assert registro["componente"] == "teste_athena"
        assert registro["conversa_id"] == "conv-123"
        assert registro["modelo"] == "gemini-3.5-flash-lite"
        assert registro["latencia_ms"] == 412
        assert registro["trace_id"] == "1234567890abcdef1234567890abcdef"
        assert registro["span_id"] == "0102030405060708"
        assert "timestamp" in registro
    finally:
        lc.configurar_logging("console", forcar=True)


def test_alembic_migrations_cria_schema_e_preserva_dados_idempotente(tmp_path):
    import memoria

    db_teste = tmp_path / "memoria_teste.db"

    # 1) Primeira execucao em banco novo: cria schema completo (ate a migration 002) e registra revisao
    revisao_1 = memoria.executar_migrations(db_teste)
    assert revisao_1 == "002_resumos_conversa"
    assert db_teste.is_file()

    with sqlite3.connect(db_teste) as c:
        tabelas = {
            r[0]
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {
            "alembic_version",
            "mensagens",
            "fatos",
            "telemetria",
            "webauthn_credenciais",
            "resumos_conversa",
        }.issubset(tabelas)
        # Insere dados para comprovar que rodar novamente nao apaga nem recria nada
        c.execute(
            "INSERT INTO fatos (chave, valor, atualizado_em) VALUES (?, ?, ?)",
            ("curso", "Engenharia", "2026-11-23T13:00:00"),
        )
        c.execute(
            "INSERT INTO mensagens (role, conteudo, trilha_json, criado_em, meta_json) VALUES (?, ?, ?, ?, ?)",
            ("user", "mensagem preservada", "[]", "2026-11-23T13:00:00", "{}"),
        )
        c.commit()

    # 2) Segunda execucao (idempotencia): mantem revisao e 100% dos dados intactos
    revisao_2 = memoria.executar_migrations(db_teste)
    assert revisao_2 == "002_resumos_conversa"

    with sqlite3.connect(db_teste) as c:
        fato = c.execute("SELECT valor FROM fatos WHERE chave = 'curso'").fetchone()
        msg = c.execute("SELECT conteudo FROM mensagens").fetchone()
        ver = c.execute("SELECT version_num FROM alembic_version").fetchone()
    assert fato == ("Engenharia",)
    assert msg == ("mensagem preservada",)
    assert ver == ("002_resumos_conversa",)


# ==============================================================================
# 9. FAN-OUT PARALELO DE SUBAGENTES (asyncio.gather + Semaphore + isolamento)
# ==============================================================================


def _executar_async_real_teste(coro):
    """Executa coroutine com event loop asyncio real (portavel em Linux CI e sandbox Windows)."""
    import asyncio
    import sys
    import threading

    if sys.platform != "win32":
        return asyncio.run(coro)

    class _LoopTesteWindows(asyncio.ProactorEventLoop):
        def _make_self_pipe(self):
            self._ssock = None
            self._csock = None

        def _close_self_pipe(self):
            self._ssock = None
            self._csock = None

        def _write_to_self(self, data=b""):
            return None

        def _loop_self_reading(self, f=None):
            return None

    resultado: list = []
    erro: list[BaseException] = []

    def _runner():
        loop = _LoopTesteWindows()
        try:
            asyncio.set_event_loop(loop)
            resultado.append(loop.run_until_complete(coro))
        except BaseException as exc:
            erro.append(exc)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=15.0)
    if erro:
        raise erro[0]
    assert resultado, "Timeout ao aguardar execucao do loop asyncio de teste"
    return resultado[0]


def test_fanout_paralelo_concorrencia_e_isolamento_de_falhas(monkeypatch, tmp_path):
    import asyncio
    import time
    from types import SimpleNamespace

    assert "consultar_especialistas_em_paralelo" in agentes.orquestrador._function_toolset.tools

    eventos_sse: list[dict] = []

    async def _fake_estudos(instrucao, **kwargs):
        await asyncio.sleep(0.20)
        return SimpleNamespace(output=f"Estudos OK: {instrucao}")

    async def _fake_google(instrucao, **kwargs):
        await asyncio.sleep(0.20)
        return SimpleNamespace(output=f"Google OK: {instrucao}")

    async def _fake_investimentos(instrucao, **kwargs):
        await asyncio.sleep(0.20)
        raise RuntimeError("API BCB temporariamente fora")

    monkeypatch.setattr(agentes.agente_estudos, "run", _fake_estudos)
    monkeypatch.setattr(agentes.agente_google, "run", _fake_google)
    monkeypatch.setattr(agentes.agente_investimentos, "run", _fake_investimentos)

    ctx = agentes.ContextoMissao(
        pasta_raiz=tmp_path,
        modelo="gemini-3.5-flash-lite",
        on_evento=lambda ev: eventos_sse.append(ev),
    )
    tarefas = [
        {"agente": "estudos", "instrucao": "checar provas A1"},
        {"agente": "google", "instrucao": "listar agenda de hoje"},
        {"agente": "investimentos", "instrucao": "consultar carteira"},
    ]

    t0 = time.perf_counter()
    relatorio = _executar_async_real_teste(
        agentes.executar_especialistas_em_paralelo(tarefas, ctx, max_concorrencia=4, timeout_por_agente=5.0)
    )
    duracao = time.perf_counter() - t0

    # 1) Prova execucao concorrente: soma sequencial seria >= 0.60s; paralelo termina perto de ~0.20s
    assert duracao < 0.48, f"Execucao pareceu sequencial ({duracao:.3f}s >= 0.48s)"

    # 2) Prova isolamento de falhas: 2 sucessos + 1 erro parcial isolado no relatorio consolidado
    assert "2/3 concluido(s) com sucesso" in relatorio
    assert "Estudos OK: checar provas A1" in relatorio
    assert "Google OK: listar agenda de hoje" in relatorio
    assert "ERRO PARCIAL (ERRO)" in relatorio
    assert "API BCB temporariamente fora" in relatorio

    # 3) Eventos SSE de progresso emitidos para os 3 subagentes
    destinos_deleg = {e["para"] for e in eventos_sse if e["tipo"] == "delegacao"}
    assert {"Agente Estudos", "Agente Google", "Agente Investimentos"}.issubset(destinos_deleg)


def test_fanout_paralelo_timeout_individual_nao_trava_demais(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    async def _fake_rapido(instrucao, **kwargs):
        await asyncio.sleep(0.02)
        return SimpleNamespace(output="Pesquisa concluida rapido")

    async def _fake_lento(instrucao, **kwargs):
        await asyncio.sleep(2.0)
        return SimpleNamespace(output="Nunca chega")

    monkeypatch.setattr(agentes.agente_pesquisador, "run", _fake_rapido)
    monkeypatch.setattr(agentes.agente_sistema, "run", _fake_lento)

    ctx = agentes.ContextoMissao(pasta_raiz=tmp_path, modelo="gemini-3.5-flash-lite")
    relatorio = _executar_async_real_teste(
        agentes.executar_especialistas_em_paralelo(
            [
                {"agente": "pesquisador", "instrucao": "buscar docs"},
                {"agente": "sistema", "instrucao": "comando lento"},
            ],
            ctx,
            timeout_por_agente=0.08,
        )
    )
    assert "1/2 concluido(s) com sucesso" in relatorio
    assert "Pesquisa concluida rapido" in relatorio
    assert "ERRO PARCIAL (TIMEOUT)" in relatorio


# ==============================================================================
# 10. COMPACTACAO DE CONTEXTO E EXTRACAO AUTOMATICA DE MEMORIA (compactador.py)
# ==============================================================================


def test_compactador_mantem_recentes_e_usa_fallback_deterministico(monkeypatch, tmp_path):
    import compactador as cp
    import memoria

    db_teste = tmp_path / "compactacao_teste.db"
    assert memoria.executar_migrations(db_teste) == "002_resumos_conversa"

    # 1) Conversa curta (<= 16 msgs): retorna sem compactar
    msgs_curtas = [{"role": "user" if i % 2 == 0 else "bot", "conteudo": f"Msg curta {i}"} for i in range(10)]
    res_curto, hist_curto = _executar_async_real_teste(
        cp.preparar_historico_compactado("conv-curta", msgs_curtas, limite_msgs=16, manter_recentes=8, db_path=db_teste)
    )
    assert res_curto == ""
    assert hist_curto == msgs_curtas

    # 2) Conversa longa (> 16 msgs) com falha 503 na API -> fallback deterministico + persiste em resumos_conversa
    chamadas_llm = {"n": 0}

    async def _falhar_llm(_msgs, resumo_anterior=""):
        chamadas_llm["n"] += 1
        raise RuntimeError("503 UNAVAILABLE simulado")

    monkeypatch.setattr(cp, "_resumir_com_llm", _falhar_llm)

    msgs_longas = [
        {"role": "user" if i % 2 == 0 else "bot", "conteudo": f"Mensagem numero {i} sobre plano A1"}
        for i in range(20)
    ]
    resumo, recentes = _executar_async_real_teste(
        cp.preparar_historico_compactado("conv-longa", msgs_longas, limite_msgs=16, manter_recentes=8, db_path=db_teste)
    )
    assert len(recentes) == 8
    assert recentes == msgs_longas[-8:]
    assert "Mensagem numero 0" in resumo
    assert "Mensagem numero 11" in resumo
    assert "Mensagem numero 19" not in resumo
    assert chamadas_llm["n"] == 1

    # Confirma persistencia em resumos_conversa e reaproveitamento em chamada repetida
    salvo_txt, salvo_qtd = cp.obter_resumo_conversa("conv-longa", db_path=db_teste)
    assert salvo_txt == resumo
    assert salvo_qtd == 12

    resumo_cache, recentes_cache = _executar_async_real_teste(
        cp.preparar_historico_compactado("conv-longa", msgs_longas, limite_msgs=16, manter_recentes=8, db_path=db_teste)
    )
    assert resumo_cache == resumo
    assert recentes_cache == recentes
    assert chamadas_llm["n"] == 1  # nao rechamou LLM porque o cache ja cobre as 12 antigas


def test_auto_memory_ignora_triviais_segredos_e_evita_duplicatas(monkeypatch, tmp_path):
    import compactador as cp
    import memoria

    db_teste = tmp_path / "automem_teste.db"
    memoria.executar_migrations(db_teste)
    monkeypatch.setenv("ATHENA_AUTO_MEMORY", "true")

    chamadas = {"n": 0}

    async def _fake_extrair_llm(msg_u: str, resp_a: str):
        chamadas["n"] += 1
        return [
            {"chave": "meta_estudos", "valor": "Revisar Fisica e Calculo toda terca a noite"},
            {"chave": "senha_banco", "valor": "minha senha secreta 123456"},
        ]

    monkeypatch.setattr(cp, "_extrair_fatos_com_llm", _fake_extrair_llm)

    # 1) Mensagens triviais / curtas sao ignoradas antes mesmo de chamar o LLM
    for trivial in ("oi", "teste", "ok", "bom dia", "valeu demais"):
        salvos_triv = _executar_async_real_teste(
            cp.extrair_memorias_em_background(trivial, "Ola!", db_path=db_teste)
        )
        assert salvos_triv == []
    assert chamadas["n"] == 0

    # 2) Mensagem contendo segredo/token e bloqueada antes de chamar o LLM
    salvos_seg = _executar_async_real_teste(
        cp.extrair_memorias_em_background(
            "Anota minha senha do portal e o token sk-1234567890abcdefghijklmnop por favor",
            "Nao salvo senhas.",
            db_path=db_teste,
        )
    )
    assert salvos_seg == []
    assert chamadas["n"] == 0

    # 3) Mensagem duradoura valida salva apenas o fato seguro (filtra 'senha_banco')
    msg_real = "Prefiro revisar Fisica e Calculo toda terca-feira a noite antes das provas A1."
    salvos_1 = _executar_async_real_teste(
        cp.extrair_memorias_em_background(msg_real, "Combinado!", db_path=db_teste)
    )
    assert len(salvos_1) == 1
    assert salvos_1[0][0] == "meta_estudos"
    assert chamadas["n"] == 1

    # 4) Segunda execucao com o mesmo fato e ignorada pela verificacao anti-duplicata
    salvos_2 = _executar_async_real_teste(
        cp.extrair_memorias_em_background(msg_real, "Combinado de novo!", db_path=db_teste)
    )
    assert salvos_2 == []

    fatos_db = memoria.listar_fatos(db_path=db_teste)
    assert len(fatos_db) == 1
    assert fatos_db[0]["chave"] == "meta_estudos"
