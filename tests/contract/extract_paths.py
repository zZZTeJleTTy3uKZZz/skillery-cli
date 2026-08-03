"""Извлечение HTTP-путей из ``core/transport.py`` статически (AST).

Зачем не грепом: путь в транспорте бывает трёх видов —

1. литерал: ``self._request("GET", "/me")``;
2. f-строка: ``self._request("GET", f"/skills/{slug}")``;
3. локальная переменная, к которой дописан query:
   ``path = "/me/device-queue"; path += "?" + urlencode(params);
   await self._request("GET", path)``.

Грep ловит только (1) и (2), а именно (3) — путь очереди заданий демона, тот
самый, тихая поломка которого и породила #1441.

Нормализация: имя path-параметра значения не имеет (в OpenAPI он может
называться иначе, чем локальная переменная CLI), поэтому любой ``{...}``
схлопывается в ``{}``; query отрезается.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

#: Вызовы, первый аргумент которых — HTTP-метод, второй — путь.
_METHOD_PATH_CALLS = frozenset({"_request", "stream", "request"})

_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)


@dataclass(frozen=True)
class PathUse:
    """Один вызов backend из транспорта."""

    method: str
    path: str  # нормализованный: без query, параметры схлопнуты в ``{}``
    lineno: int
    raw: str  # как написано в коде — для сообщения об ошибке

    def __str__(self) -> str:  # pragma: no cover — только для отчёта
        return f"{self.method} {self.path}  (transport.py:{self.lineno} — {self.raw})"


def normalize(path: str) -> str:
    """``/skills/{slug}/versions/{semver}?x=1`` → ``/skills/{}/versions/{}``."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    out: list[str] = []
    depth = 0
    for ch in path:
        if ch == "{":
            depth += 1
            if depth == 1:
                out.append("{}")
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _literal(node: ast.AST) -> str | None:
    """Строковое значение узла: литерал или f-строка (подстановки → ``{}``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{}")
            else:  # pragma: no cover — других узлов в f-строке не бывает
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal(node.left)
        if left is not None:
            # ``"/x" + something`` — правую часть считаем query/хвостом:
            # если слева уже есть путь, его достаточно (query отрезается).
            right = _literal(node.right)
            return left + (right if right is not None else "")
    return None


class _FunctionScanner(ast.NodeVisitor):
    """Одна функция = одна область видимости локальных путей."""

    def __init__(self) -> None:
        self.uses: list[PathUse] = []
        self._vars: dict[str, str] = {}

    def visit_Assign(self, node: ast.Assign) -> None:
        value = _literal(node.value)
        if value is not None and value.startswith("/"):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._vars[target.id] = value
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and isinstance(node.target, ast.Name):
            value = _literal(node.value)
            if value is not None and value.startswith("/"):
                self._vars[node.target.id] = value
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # ``path += "?" + urlencode(...)`` — путь не меняется, меняется query.
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else None
        if name not in _METHOD_PATH_CALLS or len(node.args) < 2:
            return
        method = _literal(node.args[0])
        if method is None or method.upper() not in _HTTP_METHODS:
            return
        arg = node.args[1]
        raw = ast.unparse(arg)
        path = _literal(arg)
        if path is None and isinstance(arg, ast.Name):
            path = self._vars.get(arg.id)
        if path is None or not path.startswith("/"):
            return
        self.uses.append(
            PathUse(
                method=method.upper(),
                path=normalize(path),
                lineno=node.lineno,
                raw=raw,
            )
        )


def extract(source_path: Path) -> list[PathUse]:
    """Все вызовы backend из файла, отсортированные по строке."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    uses: list[PathUse] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scanner = _FunctionScanner()
            for child in node.body:
                scanner.visit(child)
            uses.extend(scanner.uses)
    return sorted(uses, key=lambda u: u.lineno)
