"""Правка блока ``insert:`` в ``cordis.patch.yml`` по id (порт install.ps1).

Правило одно: **чужие строки переносятся как есть**. Правим только записи,
чьи id назвал плагин; дубли наших id схлопываем; комментарии и записи соседей
не трогаем. Блок ищется по отступу, как его находит сам Harness, а не по
номеру строки — человек вправе дописать свой YAML выше.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_HEAD_RE = re.compile(r"^(?P<pad> *)- insert: *$")
_ITEM_RE = re.compile(r"^(?P<pad> +)- id: *(?P<id>[^ ]+) *$")
#: Простое значение не начинается с цифры (``1.9`` вернулось бы из YAML числом)
#: и с ``@`` (YAML держит его за резерв под директиву, и разбор файла падает) —
#: ровно те же два правила, что в ``dsh-plugins/install.ps1``.
_SAFE_SCALAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_./@+-]*$")
_RESERVED_SCALARS = {"true", "false", "null", "yes", "no", "on", "off", "~"}


class InsertBlockError(ValueError):
    """Раскладка профиля не даёт безопасно править блок ``insert:``."""


def find_insert_block(lines: Sequence[str]) -> tuple[int, int, int] | None:
    """Вернуть ``(строка_заголовка, отступ, конец_исключая)`` первого блока."""
    for at, line in enumerate(lines):
        match = _HEAD_RE.match(line)
        if not match:
            continue
        indent = len(match.group("pad"))
        end = at
        for nxt in range(at + 1, len(lines)):
            bare = lines[nxt].strip()
            if not bare or bare.startswith("#"):
                continue
            if _indent(lines[nxt]) <= indent:
                break
            end = nxt
        return at, indent, end + 1
    return None


def insert_block_ids(text: str) -> list[str]:
    """id записей, которые сейчас лежат в блоке ``insert:``."""
    lines = text.splitlines()
    block = find_insert_block(lines)
    if block is None:
        return []
    head, indent, end = block
    return [item[0] for item in _split_block(lines, head, indent, end) if item[0]]


def update_insert_block(
    text: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    remove_ids: Sequence[str] = (),
) -> str:
    """Вписать строки в блок по id или снять их; вернуть НОВЫЙ текст."""
    newline = "\r\n" if "\r\n" in text else "\n"
    had_trailing = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    block = find_insert_block(lines)
    removing = bool(remove_ids)

    if block is None:
        if removing:
            return text
        rendered = ["- insert:"]
        for row in rows:
            rendered.extend(render_row(row, 4))
        new_lines = [*lines, *rendered]
        return newline.join(new_lines) + newline

    head, indent, end = block
    body, kept_items = _build_body(lines, head, indent, end, rows, remove_ids)
    if removing and kept_items == 0:
        new_lines = [*lines[:head], *lines[end:]]
    else:
        new_lines = [*lines[: head + 1], *body, *lines[end:]]

    updated = newline.join(new_lines)
    if had_trailing or not text:
        updated += newline
    return updated


def render_row(row: Mapping[str, Any], indent: int) -> list[str]:
    """Отрисовать запись (``- id:`` + name/disabled/config) с отступом."""
    pad = " " * indent
    row_id = _require_id(row)
    out = [f"{pad}- id: {_scalar(row_id)}"]
    name = row.get("name")
    if isinstance(name, str) and name:
        out.append(f"{pad}  name: {_scalar(name)}")
    disabled = row.get("disabled")
    if isinstance(disabled, bool):
        out.append(f"{pad}  disabled: {'true' if disabled else 'false'}")
    config = row.get("config")
    if isinstance(config, Mapping) and config:
        # ``config:`` — это узел, а не набор полей строки: его ключи идут на
        # два пробела глубже самого узла, иначе Harness увидит у записи чужие
        # поля и настройки плагина не применятся (так же пишет install.ps1).
        out.append(f"{pad}  config:")
        out.extend(_render_mapping(config, indent + 4))
    return out


def _require_id(row: Mapping[str, Any]) -> str:
    value = row.get("id")
    if not isinstance(value, str) or not value.strip():
        raise InsertBlockError("в записи insert нет id")
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _split_block(
    lines: Sequence[str], head: int, indent: int, end: int
) -> list[tuple[str, list[str]]]:
    """Разбить тело блока на ``(id|"", строки)``; ``""`` — чужие строки."""
    segments: list[tuple[str, list[str]]] = []
    raw: list[str] = []
    item_id = ""
    item_lines: list[str] = []
    item_indent = 0

    def flush_item() -> None:
        nonlocal item_id, item_lines
        if item_lines:
            segments.append((item_id, item_lines))
        item_id, item_lines = "", []

    def flush_raw() -> None:
        nonlocal raw
        if raw:
            segments.append(("", raw))
        raw = []

    for at in range(head + 1, end):
        line = lines[at]
        match = _ITEM_RE.match(line)
        if match and len(match.group("pad")) > indent:
            flush_item()
            flush_raw()
            item_id = match.group("id")
            item_indent = len(match.group("pad"))
            item_lines = [line]
            continue
        bare = line.strip()
        if item_lines and bare.startswith("#") and _indent(line) <= item_indent:
            flush_item()
            raw.append(line)
            continue
        if item_lines:
            item_lines.append(line)
        else:
            raw.append(line)
    flush_item()
    flush_raw()
    return segments


def _build_body(
    lines: Sequence[str],
    head: int,
    indent: int,
    end: int,
    rows: Sequence[Mapping[str, Any]],
    remove_ids: Sequence[str],
) -> tuple[list[str], int]:
    segments = _split_block(lines, head, indent, end)
    removing = bool(remove_ids)
    managed: dict[str, Mapping[str, Any] | None] = (
        {str(row_id): None for row_id in remove_ids}
        if removing
        else {_require_id(row): row for row in rows}
    )

    item_indent = indent + 4
    for segment_id, segment_lines in segments:
        if segment_id:
            item_indent = _indent(segment_lines[0])
            break

    body: list[str] = []
    done: set[str] = set()
    kept_items = 0
    raw_start = -1
    raw_is_preamble = False
    is_preamble = True

    for segment_id, segment_lines in segments:
        if not segment_id:
            raw_start = len(body)
            raw_is_preamble = is_preamble
            body.extend(segment_lines)
            continue
        if segment_id in managed:
            if removing:
                if raw_start >= 0 and not raw_is_preamble:
                    del body[raw_start:]
                raw_start = -1
                is_preamble = False
                continue
            if segment_id in done:
                continue
            for note in segment_lines:
                if note.strip().startswith("#"):
                    body.append(" " * item_indent + note.strip())
            body.extend(render_row(managed[segment_id] or {}, item_indent))
            done.add(segment_id)
            kept_items += 1
            raw_start = -1
            is_preamble = False
            continue
        body.extend(segment_lines)
        kept_items += 1
        raw_start = -1
        is_preamble = False

    if not removing:
        for row in rows:
            row_id = _require_id(row)
            if row_id in done:
                continue
            body.extend(render_row(row, item_indent))
            done.add(row_id)
            kept_items += 1

    return body, kept_items


def _render_mapping(mapping: Mapping[str, Any], indent: int) -> list[str]:
    pad = " " * indent
    out: list[str] = []
    for key, value in mapping.items():
        if isinstance(value, Mapping) and value:
            out.append(f"{pad}{_scalar(str(key))}:")
            out.extend(_render_mapping(value, indent + 2))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if not value:
                out.append(f"{pad}{_scalar(str(key))}: []")
                continue
            out.append(f"{pad}{_scalar(str(key))}:")
            for item in value:
                out.append(f"{pad}  - {_scalar_value(item)}")
        else:
            out.append(f"{pad}{_scalar(str(key))}: {_scalar_value(value)}")
    return out


def _scalar_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return _scalar(value)
    return "null"


def _scalar(value: str) -> str:
    if value and _SAFE_SCALAR_RE.match(value) and value.lower() not in _RESERVED_SCALARS:
        return value
    return json.dumps(value, ensure_ascii=False)
