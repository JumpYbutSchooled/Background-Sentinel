"""A calculator for the command line that cannot run arbitrary code.

`eval()` would be four lines and a security hole: anything typed at a prompt
that is one hotkey away from every keystroke has to be parsed, not executed. So
the expression is compiled to an AST and walked, and any node that is not
arithmetic is refused by name.

Supports the operators you would expect, the constants, and a short table of
functions. Integer results stay integers — `calc 2+2` should say 4, not 4.0.
"""

from __future__ import annotations

import ast
import math
import operator

#: Every binary operator that is allowed to appear.
_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_NAMES: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}

_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": lambda *a: sum(a),
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "hypot": math.hypot,
    "degrees": math.degrees,
    "radians": math.radians,
    "factorial": math.factorial,
    "gcd": math.gcd,
}

#: Guards against `9**9**9` locking the daemon up for the rest of the day.
MAX_POWER = 1e6


class CalcError(Exception):
    """The expression was not something this calculator will evaluate."""


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError(f"{node.value!r} is not a number")
        return node.value
    if isinstance(node, ast.BinOp):
        handler = _BINARY.get(type(node.op))
        if handler is None:
            raise CalcError(f"{type(node.op).__name__.lower()} is not allowed")
        left, right = _eval(node.left), _eval(node.right)
        if handler is operator.pow and (abs(right) > MAX_POWER or abs(left) > MAX_POWER):
            raise CalcError("that power would take all day")
        try:
            return handler(left, right)
        except ZeroDivisionError:
            raise CalcError("division by zero") from None
        except (OverflowError, ValueError) as exc:
            raise CalcError(str(exc)) from None
    if isinstance(node, ast.UnaryOp):
        handler = _UNARY.get(type(node.op))
        if handler is None:
            raise CalcError("only + and - can lead a number")
        return handler(_eval(node.operand))
    if isinstance(node, ast.Name):
        if node.id.lower() not in _NAMES:
            raise CalcError(f"{node.id} is not a value I know")
        return _NAMES[node.id.lower()]
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalcError("that is not a function call I will run")
        name = node.func.id.lower()
        function = _FUNCTIONS.get(name)
        if function is None:
            raise CalcError(f"{node.func.id}() is not a function I know")
        if node.keywords:
            raise CalcError("keyword arguments are not supported")
        try:
            return function(*(_eval(arg) for arg in node.args))
        except CalcError:
            raise
        except Exception as exc:
            raise CalcError(f"{name}: {exc}") from None
    raise CalcError("that is not arithmetic")


def evaluate(expression: str) -> float:
    """The value of an arithmetic expression. Raises `CalcError` on anything else."""
    text = expression.strip()
    if not text:
        raise CalcError("nothing to work out")
    # Courtesies for the way people actually type sums at a prompt.
    text = text.replace("×", "*").replace("÷", "/").replace("^", "**")
    # Thousands separators, but only where there is no call for a comma to
    # belong to. Stripping them everywhere turns `round(pi, 4)` into
    # `round(pi4)` and `hypot(3, 4)` into a very confident 34.
    if "(" not in text:
        text = text.replace(",", "")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise CalcError(f"{expression.strip()!r} is not an expression") from None
    return _eval(tree)


def render(value: float) -> str:
    """A number as a person would write it: no trailing .0, no float noise."""
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        if abs(value) < 1e16:
            return f"{int(value):,}"
    rounded = round(float(value), 10)
    text = f"{rounded:,.10f}".rstrip("0").rstrip(".")
    return text or "0"
