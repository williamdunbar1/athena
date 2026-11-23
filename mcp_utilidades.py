"""Servidor MCP local de exemplo para a Athena usando FastMCP (transporte stdio).

Expoe ferramentas utilitarias seguras para validar o protocolo MCP ponta a ponta:
- calcular_expressao_segura: avaliador matematico baseado em AST (sem eval)
- status_sistema_pc: diagnostico somente leitura do ambiente local
"""

from __future__ import annotations

import ast
import math
import operator
import os
import platform
import shutil
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

mcp = FastMCP("Athena Utilidades")

_OPS_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_OPS_UN = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _fatorial_seguro(n: float) -> int:
    ival = int(n)
    if ival != n or ival < 0 or ival > 100:
        raise ValueError("factorial aceita apenas inteiros entre 0 e 100.")
    return math.factorial(ival)


_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "ceil": math.ceil,
    "floor": math.floor,
    "radians": math.radians,
    "degrees": math.degrees,
    "factorial": _fatorial_seguro,
}

_CONSTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


def _avaliar_ast(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _avaliar_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name):
        chave = node.id.lower()
        if chave in _CONSTS:
            return float(_CONSTS[chave])
        raise ValueError(f"Identificador nao permitido: '{node.id}'")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS_UN:
        return float(_OPS_UN[type(node.op)](_avaliar_ast(node.operand)))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS_BIN:
        esq = _avaliar_ast(node.left)
        dir_ = _avaliar_ast(node.right)
        if isinstance(node.op, ast.Pow) and abs(dir_) > 1000:
            raise ValueError("Expoente muito grande (limite: 1000).")
        return float(_OPS_BIN[type(node.op)](esq, dir_))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = _FUNCS.get(node.func.id.lower())
        if fn is None or node.keywords:
            raise ValueError(f"Funcao nao permitida: '{node.func.id}'")
        args = [_avaliar_ast(a) for a in node.args]
        return float(fn(*args))
    raise ValueError("Expressao matematica invalida ou nao suportada.")


@mcp.tool()
def calcular_expressao_segura(expressao: str) -> str:
    """Avalia uma expressao matematica com seguranca via AST (suporta +, -, *, /, **, sqrt, sin, cos, log, pi, e)."""
    expr = (expressao or "").strip()
    if not expr or len(expr) > 300:
        return "Erro: informe uma expressao matematica de ate 300 caracteres."
    try:
        arvore = ast.parse(expr, mode="eval")
        res = _avaliar_ast(arvore)
        if math.isfinite(res) and abs(res - round(res)) < 1e-12:
            return f"{expr} = {int(round(res))}"
        return f"{expr} = {res:.10g}"
    except Exception as e:
        return f"Erro ao calcular '{expr}': {e}"


@mcp.tool()
def status_sistema_pc() -> str:
    """Retorna diagnostico rapido (somente leitura) do sistema: SO, Python, CPUs, disco e horario local."""
    try:
        fuso = ZoneInfo("America/Sao_Paulo")
    except Exception:
        fuso = timezone(timedelta(hours=-3))
    agora = datetime.now(fuso).strftime("%Y-%m-%d %H:%M:%S %Z")

    drive = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
    try:
        uso = shutil.disk_usage(drive)
        gb = 1024 ** 3
        disco_txt = f"{uso.free / gb:.1f} GB livres de {uso.total / gb:.1f} GB ({(uso.used / uso.total) * 100:.1f}% usado)"
    except OSError:
        disco_txt = "Indisponivel"

    return (
        f"Sistema: {platform.system()} {platform.release()} ({platform.machine()})\n"
        f"Python: {platform.python_version()}\n"
        f"CPUs logicas: {os.cpu_count() or '?'}\n"
        f"Disco ({drive}): {disco_txt}\n"
        f"Horario local: {agora}"
    )


if __name__ == "__main__":
    mcp.run()
