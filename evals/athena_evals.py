"""Evals agenticos da Athena com `pydantic-evals` + integracao OpenTelemetry/Logfire.

Execucao manual (NAO roda dentro do `pytest` padrao para nao consumir cota da API Gemini):
    uv run --env-file .env python -m evals.athena_evals
    uv run --env-file .env python -m evals.athena_evals --judge
    uv run --env-file .env python -m evals.athena_evals --modelo gemini-4-hydrogen
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from pydantic_evals import Case, Dataset, set_eval_attribute
from pydantic_evals.evaluators import (
    EvaluationReason,
    Evaluator,
    EvaluatorContext,
    LLMJudge,
    MaxDuration,
)

from agentes import (
    MODEL_COOLDOWN,
    MODEL_COOLDOWN_MOTIVO,
    ContextoMissao,
    obter_fila_modelos_auto,
    obter_modelo,
    orquestrador,
    registrar_cooldown_modelo,
)
from observabilidade import configurar_observabilidade

PASTA_PADRAO_EVAL = Path(__file__).resolve().parent.parent / "sandbox_files"
TIMEOUT_TENTATIVA_EVAL_SEGUNDOS = 90.0
PAUSA_ENTRE_CASOS_EVAL_SEGUNDOS = 1.5


def _limpar_cooldowns_eval() -> None:
    """Limpa todos os cooldowns em memoria antes de cada caso para isolar timeouts/503 entre casos."""
    MODEL_COOLDOWN.clear()
    MODEL_COOLDOWN_MOTIVO.clear()


@dataclass
class ResultadoAthenaEval:
    resposta: str
    tools_chamadas: list[str] = field(default_factory=list)
    delegacoes: list[str] = field(default_factory=list)
    acoes_windows: list[dict[str, Any]] = field(default_factory=list)
    trilha: list[dict[str, Any]] = field(default_factory=list)
    modelo_usado: str = ""


def _coletar_tools_do_span_tree(ctx: EvaluatorContext[str, ResultadoAthenaEval, dict[str, Any]]) -> set[str]:
    nomes: set[str] = set()
    try:
        tree = ctx.span_tree
        for span in tree.find(lambda node: True):
            attrs = getattr(span, "attributes", None) or {}
            tool_name = attrs.get("gen_ai.tool.name") or attrs.get("tool_name")
            if isinstance(tool_name, str) and tool_name.strip():
                nomes.add(tool_name.strip())
    except Exception:
        pass
    return nomes


@dataclass
class ValidarToolsEsperadas(Evaluator[str, ResultadoAthenaEval, dict[str, Any]]):
    """Valida se as tools/delegacoes esperadas foram acionadas (via historico, trilha e spans OTel)
    e se nenhuma tool proibida foi chamada.
    """

    def evaluate(
        self,
        ctx: EvaluatorContext[str, ResultadoAthenaEval, dict[str, Any]],
    ) -> EvaluationReason:
        meta = ctx.metadata or {}
        esperadas_qualquer: list[str] = list(meta.get("expected_any_tools") or [])
        proibidas: list[str] = list(meta.get("forbidden_tools") or [])
        exigir_acao_windows: bool = bool(meta.get("require_windows_request", False))

        tools_detectadas = set(ctx.output.tools_chamadas)
        tools_detectadas.update(_coletar_tools_do_span_tree(ctx))

        # Tambem inspeciona a trilha registrada no ContextoMissao
        trilha_txt = " ".join(
            f"{it.get('de', '')} {it.get('para', '')} {it.get('conteudo', '')}"
            for it in (ctx.output.trilha or [])
        ).lower()

        if proibidas:
            usadas_proibidas = [t for t in proibidas if t in tools_detectadas]
            if usadas_proibidas:
                return EvaluationReason(
                    value=False,
                    reason=f"Chamou tool(s) proibida(s) para este cenario: {usadas_proibidas}",
                )

        if esperadas_qualquer:
            achou = any(
                (t in tools_detectadas) or (t.lower() in trilha_txt)
                for t in esperadas_qualquer
            )
            if not achou:
                return EvaluationReason(
                    value=False,
                    reason=(
                        f"Nenhuma tool esperada foi acionada ({esperadas_qualquer}). "
                        f"Detectadas: {sorted(tools_detectadas)}"
                    ),
                )

        if exigir_acao_windows and not ctx.output.acoes_windows:
            return EvaluationReason(
                value=False,
                reason="Nenhuma solicitacao real de permissao Windows foi registrada em acoes_windows.",
            )

        return EvaluationReason(
            value=True,
            reason=f"Tools validadas com sucesso: {sorted(tools_detectadas) or ['resposta direta']}",
        )


@dataclass
class ValidarSemAlucinacaoEConteudo(Evaluator[str, ResultadoAthenaEval, dict[str, Any]]):
    """Garante ausencia de alucinacoes conhecidas (nuvem/container/Bloco de Notas) e presenca de sinais esperados."""

    def evaluate(
        self,
        ctx: EvaluatorContext[str, ResultadoAthenaEval, dict[str, Any]],
    ) -> EvaluationReason:
        meta = ctx.metadata or {}
        texto = (ctx.output.resposta or "").strip()
        if not texto:
            return EvaluationReason(value=False, reason="Resposta vazia da Athena.")

        low = texto.lower()
        frases_proibidas: list[str] = list(meta.get("forbidden_phrases") or [])
        for frase in frases_proibidas:
            if frase.lower() in low:
                return EvaluationReason(
                    value=False,
                    reason=f"Alucinacao/frase proibida detectada na resposta: '{frase}'",
                )

        esperadas_qualquer: list[str] = list(meta.get("expected_any_phrases") or [])
        if esperadas_qualquer and not any(p.lower() in low for p in esperadas_qualquer):
            return EvaluationReason(
                value=False,
                reason=f"Resposta nao contem nenhum indicador esperado ({esperadas_qualquer}).",
            )

        return EvaluationReason(value=True, reason="Conteudo aderente e sem alucinacoes.")


def criar_dataset_athena(
    *,
    usar_llm_judge: bool = False,
    modelo_judge: str = "google:gemini-3.5-flash-lite",
) -> Dataset[str, ResultadoAthenaEval, dict[str, Any]]:
    """Constroi o Dataset `pydantic-evals` cobrindo os cenarios criticos da Athena."""
    casos: list[Case[str, ResultadoAthenaEval, dict[str, Any]]] = [
        Case(
            name="organizar_area_de_trabalho",
            inputs="Organize minha Área de Trabalho e gere as propostas de organização para os arquivos e atalhos soltos.",
            metadata={
                "expected_any_tools": [
                    "delegar_para_faxineiro",
                    "planejar_organizacao_pasta",
                    "listar_itens_pasta",
                    "propor_acoes_em_lote",
                ],
                "forbidden_phrases": [
                    "estou na nuvem",
                    "rodo na nuvem",
                    "container isolado",
                    "bloco de notas",
                    "salve como .py",
                    "não tenho acesso ao windows",
                ],
                "expected_any_phrases": [
                    "faxina",
                    "aprova",
                    "proposta",
                    "pendente",
                    "desktop",
                    "área de trabalho",
                ],
            },
            evaluators=(MaxDuration(seconds=90.0),),
        ),
        Case(
            name="abrir_aba_brave",
            inputs="Abra https://github.com em uma nova aba do Brave.",
            metadata={
                "expected_any_tools": ["solicitar_abrir_url"],
                "require_windows_request": True,
                "forbidden_phrases": [
                    "container linux",
                    "preso em um container",
                    "presa em um container",
                    "estou na nuvem",
                    "não consigo abrir aplicativos",
                    "não tenho acesso ao windows",
                ],
                "expected_any_phrases": [
                    "permitir",
                    "permissão",
                    "confirma",
                    "pendente",
                    "brave",
                    "github",
                ],
            },
            evaluators=(MaxDuration(seconds=60.0),),
        ),
        Case(
            name="saudacao_simples",
            inputs="oi, teste",
            metadata={
                "forbidden_tools": [
                    "delegar_para_sistema",
                    "delegar_para_pesquisador",
                    "delegar_para_faxineiro",
                    "delegar_para_estudos",
                    "delegar_para_investimentos",
                    "delegar_para_google",
                    "solicitar_abrir_app",
                    "solicitar_abrir_url",
                ],
                "forbidden_phrases": [
                    "traceback",
                    "error",
                ],
                "expected_any_phrases": [
                    "olá",
                    "oi",
                    "athena",
                    "ajudar",
                    "tudo",
                    "online",
                    "pronta",
                ],
            },
            evaluators=(MaxDuration(seconds=15.0),),
        ),
    ]

    avaliadores_globais: list[Evaluator[str, ResultadoAthenaEval, dict[str, Any]]] = [
        ValidarToolsEsperadas(),
        ValidarSemAlucinacaoEConteudo(),
    ]

    if usar_llm_judge:
        avaliadores_globais.append(
            LLMJudge(
                rubric=(
                    "A resposta da Athena esta em portugues do Brasil, clara, objetiva, "
                    "sem alucinar que esta na nuvem ou presa em container Linux, e respeita o fluxo de aprovacao humana."
                ),
                model=modelo_judge,
                include_input=True,
            )
        )

    return Dataset(
        name="athena_core_evals",
        cases=casos,
        evaluators=avaliadores_globais,
    )


def _extrair_tools_mensagens(resultado) -> list[str]:
    chamadas: list[str] = []
    try:
        msgs = resultado.all_messages()
    except Exception:
        return chamadas
    for msg in msgs or []:
        for parte in getattr(msg, "parts", None) or []:
            kind = str(getattr(parte, "part_kind", "") or "").lower()
            if kind in ("tool-call", "builtin-tool-call"):
                nome = getattr(parte, "tool_name", None)
                if isinstance(nome, str) and nome.strip():
                    chamadas.append(nome.strip())
    return chamadas


async def executar_tarefa_athena(
    prompt: str,
    *,
    modelo_forcado: str | None = None,
) -> ResultadoAthenaEval:
    """Executa um caso de eval contra o `orquestrador` real usando o mesmo fluxo/fallback do modo Auto."""
    PASTA_PADRAO_EVAL.mkdir(parents=True, exist_ok=True)
    # Isola cada caso: limpa MODEL_COOLDOWN e cooldowns de 503/timeout antes de iniciar
    _limpar_cooldowns_eval()
    eh_auto = not modelo_forcado or modelo_forcado == "auto"

    if not eh_auto:
        modelo_id = str(modelo_forcado)
        ctx = ContextoMissao(
            pasta_raiz=PASTA_PADRAO_EVAL,
            modelo=modelo_id,
            usar_fallback=False,
            mensagem_atual=prompt,
        )
        res = await orquestrador.run(
            prompt,
            deps=ctx,
            model=obter_modelo(modelo_id, usar_fallback=False),
        )
        modelo_usado = modelo_id
    else:
        fila_modelos, _motivo = obter_fila_modelos_auto(prompt)
        ctx = ContextoMissao(
            pasta_raiz=PASTA_PADRAO_EVAL,
            modelo=fila_modelos[0],
            usar_fallback=True,
            mensagem_atual=prompt,
        )
        res = None
        modelo_usado = fila_modelos[0]
        ultimo_erro: Exception | None = None
        erros_tentativas: list[str] = []

        for idx, mid in enumerate(fila_modelos):
            ctx.modelo = mid
            modelo_direto = obter_modelo(mid, usar_fallback=False)
            try:
                async with asyncio.timeout(TIMEOUT_TENTATIVA_EVAL_SEGUNDOS):
                    res = await orquestrador.run(
                        prompt,
                        deps=ctx,
                        model=modelo_direto,
                        retries=0,
                    )
                modelo_usado = mid
                break
            except Exception as exc:
                ultimo_erro = exc
                elegivel, _cd, motivo_err = registrar_cooldown_modelo(mid, exc)
                erros_tentativas.append(f"{mid}: {motivo_err or exc}")
                if elegivel and idx + 1 < len(fila_modelos):
                    continue
                if not elegivel:
                    raise
                break

        if res is None:
            raise RuntimeError(
                f"Todos os modelos do modo Auto falharam no eval ({'; '.join(erros_tentativas)})."
            ) from ultimo_erro

    tools = _extrair_tools_mensagens(res)
    delegacoes = [t for t in tools if t.startswith("delegar_para_")]

    try:
        set_eval_attribute("athena.modelo", modelo_usado)
        set_eval_attribute("athena.tools_chamadas", tools)
        set_eval_attribute("athena.acoes_windows_qtd", len(ctx.acoes_windows))
    except Exception:
        pass

    return ResultadoAthenaEval(
        resposta=(res.output or "").strip(),
        tools_chamadas=tools,
        delegacoes=delegacoes,
        acoes_windows=list(ctx.acoes_windows),
        trilha=list(ctx.trilha),
        modelo_usado=modelo_usado,
    )


def rodar_evals(
    *,
    usar_llm_judge: bool = False,
    modelo: str | None = None,
    modelo_judge: str = "google:gemini-3.5-flash-lite",
):
    """Inicializa observabilidade (enviando ao Logfire se LOGFIRE_TOKEN existir) e executa o experimento sequencialmente."""
    configurar_observabilidade(app=None)
    dataset = criar_dataset_athena(usar_llm_judge=usar_llm_judge, modelo_judge=modelo_judge)
    casos_executados = [0]

    async def _task(prompt: str) -> ResultadoAthenaEval:
        if casos_executados[0] > 0:
            await asyncio.sleep(PAUSA_ENTRE_CASOS_EVAL_SEGUNDOS)
        casos_executados[0] += 1
        return await executar_tarefa_athena(prompt, modelo_forcado=modelo)

    report = dataset.evaluate_sync(
        _task,
        name="athena_evals_experimento",
        max_concurrency=1,
        metadata={
            "modelo": modelo or "auto",
            "usar_llm_judge": usar_llm_judge,
        },
    )
    report.print(include_input=True, include_output=True, include_durations=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Roda os evals agenticos da Athena via pydantic-evals + Logfire.")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Ativa avaliacao qualitativa extra via LLMJudge.",
    )
    parser.add_argument(
        "--modelo",
        default=None,
        help="Forca um modelo especifico (ex.: gemini-3.5-flash-lite, gemini-4-hydrogen). Padrao: auto.",
    )
    parser.add_argument(
        "--modelo-judge",
        default="google:gemini-3.5-flash-lite",
        help="Modelo usado pelo LLMJudge quando --judge estiver ativo.",
    )
    args = parser.parse_args()
    rodar_evals(
        usar_llm_judge=bool(args.judge),
        modelo=args.modelo,
        modelo_judge=args.modelo_judge,
    )


if __name__ == "__main__":
    main()
