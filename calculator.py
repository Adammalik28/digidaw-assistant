"""
Digidaw's sums, said in words or symbols ("berapa 20 * 30", "15 ditambah 8", "25 persen dari 80", "akar 144"): answered
here, instantly and exactly, instead of asking an AI. Only numbers and these operators are ever evaluated.
"""
import ast
import math
import operator
import re

_WORDS = [
    (r"\b(\d+(?:\.\d+)?) ?(?:persen|%) dari\b", r"(\1 / 100) *"),
    (r"\bakar(?: kuadrat)?(?: dari)? (\d+(?:\.\d+)?)", r"sqrt(\1)"),
    (r"\b(?:ditambah|tambah|plus)\b", "+"),
    (r"\b(?:dikurangi|dikurang|kurang|minus)\b", "-"),
    (r"\b(?:dikali|dikalikan|kali|x)\b|×", "*"),
    (r"\b(?:dibagi|bagi)\b|÷|:", "/"),
    (r"\bpangkat\b", "**"),
]
_ONLY_SUM_RE = re.compile(r"(?:sqrt\(|[\d.+\-*/() ])+")
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow}
MAX_POWER = 64                       # "9 pangkat 9 pangkat 9" would keep the CPU busy for ever


def _number(text: str) -> str:
    """Indonesian numbers: "1.000" is a thousand, "2,5" two and a half."""
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        return text.replace(".", "")
    return text.replace(",", ".")


def parse(text: str) -> str | None:
    """The sum in a sentence, as an expression, or None when it isn't one (no operator: "volume 20")."""
    sum_ = re.sub(r"\b\d{1,3}(?: \d{3})+\b", lambda m: m[0].replace(" ", ""), text.lower().strip())   # (punctuation gone)
    sum_ = re.sub(r"\d[\d.,]*\d|\d", lambda m: _number(m[0]), sum_)
    for pattern, replacement in _WORDS:
        sum_ = re.sub(pattern, replacement, sum_)
    sum_ = sum_.strip()
    if len(sum_) > 60 or not _ONLY_SUM_RE.fullmatch(sum_) or not re.search(r"\d", sum_) \
            or not re.search(r"[+\-*/]|sqrt", sum_):
        return None
    try:
        ast.parse(sum_, mode="eval")
    except (SyntaxError, ValueError):
        return None
    return sum_


def _value(node):
    if isinstance(node, ast.Expression):
        return _value(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_value(node.operand)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _value(node.left), _value(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > MAX_POWER or abs(left) > 10 ** 6):
            raise OverflowError
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "sqrt" and len(node.args) == 1:
        return math.sqrt(_value(node.args[0]))
    raise ValueError(type(node).__name__)


def said(value: float) -> str:
    """600, 2,5, 1.000.000: the way it's written and read in Indonesian."""
    rounded = round(value, 4)
    whole = abs(rounded - round(rounded)) < 1e-9
    text = f"{round(rounded):,}" if whole else f"{rounded:,.4f}".rstrip("0")
    return text.translate(str.maketrans(",.", ".,"))


def solve(text: str) -> str | None:
    """The result as it should be said; None when the sum can't be worked out (not a sum, or dividing by zero)."""
    sum_ = parse(text)
    if not sum_:
        return None
    try:
        return said(_value(ast.parse(sum_, mode="eval")))
    except (ArithmeticError, ValueError, TypeError):
        return None
