"""Deliberately small, deterministic action surface. No eval, shell, or arbitrary Python."""
import ast
import math
import operator

_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
              ast.Pow: operator.pow}

def calculate(expression):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 200:
        raise ValueError("Calculator expressions must contain 1 to 200 characters.")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise ValueError("Invalid arithmetic expression.") from exc
    if sum(1 for _ in ast.walk(tree)) > 60:
        raise ValueError("Arithmetic expression is too complex.")

    def bounded(value):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or abs(value) > 1e15:
            raise ValueError("Arithmetic result is outside the permitted range.")
        return value

    def visit(node, depth=0):
        if depth > 12:
            raise ValueError("Arithmetic nesting limit exceeded.")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return bounded(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return bounded((1 if isinstance(node.op, ast.UAdd) else -1) * visit(node.operand, depth + 1))
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and (abs(right) > 12 or abs(left) > 1e6):
                raise ValueError("Exponentiation exceeds the calculator budget.")
            try:
                return bounded(_OPERATORS[type(node.op)](left, right))
            except (ArithmeticError, OverflowError) as exc:
                raise ValueError("Arithmetic operation is undefined or too large.") from exc
        raise ValueError("Only numeric arithmetic is allowed.")
    return visit(tree.body)
