"""Secret-scan для publish (ТЗ §10): gitleaks --no-git + regex-fallback.

gitleaks — внешний бинарь, может отсутствовать. Поэтому:

* если `gitleaks` в PATH → `gitleaks detect --no-git --source <dir>
  --report-format json --report-path <tmp>` и парсим JSON-отчёт;
* иначе → минимальный regex-fallback по типовым паттернам утечки
  (AWS key, PEM private key, `password=`, токен-присваивания,
  высокоэнтропийные base64/hex строки в подозрительном контексте).

Обе ветки возвращают `list[Finding]`. `Finding.snippet` ВСЕГДА маскирован —
полный секрет наружу не печатается (видны первые/последние символы).

Модуль чистый (без typer/rich), чтобы тестироваться и переиспользоваться.
Ничего не печатает сам — решение abort/warning принимает caller (`cmd_publish`).
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Папки, которые НЕ сканируем (локальный state / служебное).
# Совпадает по смыслу с manifest_builder._IGNORE_PATHS.
_IGNORE_DIRS = frozenset(
    {
        "_local",
        "browser_profiles",
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        "dist",
        "build",
    }
)

# Расширения, которые точно не текст (не сканируем — экономим и не ловим
# ложные срабатывания энтропии на бинарях).
_BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
        ".pdf", ".zip", ".gz", ".tar", ".tgz", ".whl", ".so", ".dylib",
        ".dll", ".exe", ".bin", ".woff", ".woff2", ".ttf", ".otf",
        ".mp3", ".mp4", ".mov", ".wav", ".pyc",
    }
)

# Лимит на размер файла для regex-скана (большие файлы пропускаем).
_MAX_SCAN_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Finding:
    """Одна находка секрета.

    `snippet` уже замаскирован — безопасно печатать/логировать.
    """

    file: str  # POSIX-относительный путь внутри сканируемой папки
    line: int  # номер строки (1-based; 0 если неизвестно)
    rule: str  # идентификатор правила (gitleaks RuleID или fallback-имя)
    snippet: str  # замаскированный фрагмент


@dataclass(frozen=True, slots=True)
class ScanResult:
    findings: list[Finding]
    backend: str  # "gitleaks" | "regex" — каким движком получено
    gitleaks_available: bool


def gitleaks_available() -> bool:
    """True если `gitleaks` найден в PATH."""
    return shutil.which("gitleaks") is not None


# ---------------------------------------------------------------------------
# Маскировка
# ---------------------------------------------------------------------------
def mask_secret(value: str, *, keep: int = 4) -> str:
    """Маскирует секрет, оставляя первые/последние `keep` символов.

    `AKIAIOSFODNN7EXAMPLE` → `AKIA…MPLE` (len=20).
    Короткие значения (<= 2*keep) полностью заменяются звёздочками,
    чтобы не раскрыть весь секрет.
    """
    value = value.strip()
    n = len(value)
    if n == 0:
        return ""
    if n <= 2 * keep:
        return "*" * n
    return f"{value[:keep]}…{value[-keep:]}"


def _mask_line(line: str, *, max_len: int = 120) -> str:
    """Маскирует длинные «секретоподобные» подстроки внутри строки.

    Используется для fallback-snippet: показываем строку, но прячем
    длинные base64/hex/quoted-значения.
    """
    def _repl(m: re.Match[str]) -> str:
        return mask_secret(m.group(0))

    # Любые длинные (>=12) последовательности из base64/hex алфавита.
    masked = re.sub(r"[A-Za-z0-9+/=_\-]{12,}", _repl, line)
    masked = masked.strip()
    if len(masked) > max_len:
        masked = masked[:max_len] + "…"
    return masked


# ---------------------------------------------------------------------------
# Regex fallback
# ---------------------------------------------------------------------------
# (rule_id, compiled regex). Порядок важен только для приоритета имени.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:[A-Z ]*)(?:PRIVATE KEY|RSA)-----"),
    ),
    (
        "generic-password",
        re.compile(r"(?i)\bpassword\s*[=:]\s*['\"]?[^\s'\"]{6,}"),
    ),
    (
        "generic-token",
        re.compile(
            r"""(?ix)
            \b(?:token|secret|api[_-]?key|access[_-]?key|auth)
            \s*[=:]\s*
            ['"][^'"]{16,}['"]
            """
        ),
    ),
)

# Контекст, повышающий подозрительность высокоэнтропийной строки.
_SECRET_CONTEXT_RE = re.compile(
    r"(?i)(secret|token|key|passw|cred|api|auth|bearer|private)"
)

# Кандидат-строка для энтропийной проверки: длинная base64/hex.
_HIGH_ENTROPY_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/=]{32,}")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _scan_text(rel_path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, int, str]] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        # 1. Прямые паттерны.
        for rule, pat in _PATTERNS:
            m = pat.search(line)
            if m:
                key = (rule, lineno, rel_path)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    Finding(
                        file=rel_path,
                        line=lineno,
                        rule=rule,
                        snippet=_mask_line(line),
                    )
                )
        # 2. Высокоэнтропийные строки в подозрительном контексте.
        if _SECRET_CONTEXT_RE.search(line):
            for cand in _HIGH_ENTROPY_CANDIDATE_RE.findall(line):
                if _shannon_entropy(cand) >= 4.0:
                    key = ("high-entropy", lineno, rel_path)
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append(
                        Finding(
                            file=rel_path,
                            line=lineno,
                            rule="high-entropy-string",
                            snippet=_mask_line(line),
                        )
                    )
                    break
    return findings


def scan_text(rel_path: str, text: str) -> list[Finding]:
    """Публичный regex-скан одного текстового блока (для backend-переиспользования
    через свою копию — здесь экспортируется для тестов CLI).
    """
    return _scan_text(rel_path, text)


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        parts = rel.parts
        if any(part in _IGNORE_DIRS for part in parts):
            continue
        if p.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            if p.stat().st_size > _MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        yield p, rel.as_posix()


def regex_scan_dir(root: Path) -> list[Finding]:
    """Regex-fallback скан всей папки."""
    findings: list[Finding] = []
    for path, rel in _iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # бинарь / нечитаемое — пропускаем
            continue
        findings.extend(_scan_text(rel, text))
    return findings


# ---------------------------------------------------------------------------
# gitleaks
# ---------------------------------------------------------------------------
def _parse_gitleaks_report(report_text: str, root: Path) -> list[Finding]:
    """Парсит gitleaks JSON-отчёт в `list[Finding]` (с маскировкой).

    Формат gitleaks v8: массив объектов
    {File, StartLine, RuleID, Secret, Match, ...}.
    """
    try:
        raw = json.loads(report_text) if report_text.strip() else []
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    findings: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        file_field = str(item.get("File", "") or item.get("file", ""))
        # gitleaks может отдать абсолютный путь — приводим к относительному.
        rel = file_field
        try:
            rel = Path(file_field).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            rel = Path(file_field).name or file_field
        secret = str(item.get("Secret", "") or item.get("Match", ""))
        snippet = mask_secret(secret) if secret else "***"
        findings.append(
            Finding(
                file=rel,
                line=int(item.get("StartLine", item.get("startLine", 0)) or 0),
                rule=str(item.get("RuleID", item.get("ruleID", "gitleaks")) or "gitleaks"),
                snippet=snippet,
            )
        )
    return findings


def gitleaks_scan_dir(root: Path) -> list[Finding]:
    """Запускает gitleaks поверх tmp-папки (`--no-git`) и парсит отчёт.

    Кидает `RuntimeError` если gitleaks упал по причине, не связанной с
    находками (exit code != 0 и != 1). gitleaks возвращает 1 когда находит
    leaks — это НЕ ошибка для нас.
    """
    from skillery_cli import _branding

    with tempfile.TemporaryDirectory(prefix=f"{_branding.APP_NAME}-gitleaks-") as tmp:
        report_path = Path(tmp) / "report.json"
        cmd = [
            "gitleaks",
            "detect",
            "--no-git",
            "--source",
            str(root),
            "--report-format",
            "json",
            "--report-path",
            str(report_path),
            "--exit-code",
            "1",
            "--no-banner",
        ]
        # Фиксированный argv, без shell — безопасно.
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        # 0 = clean, 1 = leaks found. Прочее = реальная ошибка gitleaks.
        if proc.returncode not in (0, 1):
            raise RuntimeError(
                f"gitleaks завершился с кодом {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
            )
        if not report_path.exists():
            return []
        report_text = report_path.read_text(encoding="utf-8", errors="ignore")
    return _parse_gitleaks_report(report_text, root)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
def scan_dir(root: Path, *, prefer_gitleaks: bool = True) -> ScanResult:
    """Сканирует папку на секреты.

    Если gitleaks доступен и `prefer_gitleaks` — используем его; при сбое
    (RuntimeError) откатываемся на regex. Иначе сразу regex.
    """
    available = gitleaks_available()
    if available and prefer_gitleaks:
        try:
            findings = gitleaks_scan_dir(root)
            return ScanResult(findings=findings, backend="gitleaks", gitleaks_available=True)
        except (RuntimeError, OSError):
            # gitleaks есть, но упал — деградируем на regex, не теряя проверку.
            findings = regex_scan_dir(root)
            return ScanResult(findings=findings, backend="regex", gitleaks_available=True)
    findings = regex_scan_dir(root)
    return ScanResult(findings=findings, backend="regex", gitleaks_available=available)
