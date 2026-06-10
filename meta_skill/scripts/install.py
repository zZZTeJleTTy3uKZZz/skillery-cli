"""Установщик skills-hub CLI.

Запускается агентом клиента при первом вызове skill'а:
    python install.py [--editable] [--source PATH]

Логика:
1. Проверяет что есть Python >= 3.11.
2. Устанавливает skills-hub CLI ИЗ ИСХОДНИКОВ репозитория (папка ``client/``
   монорепо) через pipx / pip (предпочтительнее pipx).
3. Сообщает путь к бинарю.

ВАЖНО: пакет ``skills-hub-cli`` пока НЕ опубликован в PyPI/npm — установка
идёт строго из локального ``client/``. Если эта папка не найдена рядом со
скриптом (скрипт вырван из монорепо), укажи путь явно через ``--source`` или
склонируй репозиторий.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[2]  # client/ (в монорепо)


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _looks_like_cli_package(path: Path) -> bool:
    """Похожа ли папка на исходники CLI (есть pyproject + пакет)."""
    return (path / "pyproject.toml").is_file() and (
        (path / "src" / "skills_hub_cli").is_dir()
        or (path / "skills_hub_cli").is_dir()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--editable", action="store_true", help="pip install -e .")
    parser.add_argument(
        "--source",
        default=str(PACKAGE_DIR),
        help="Путь к client/ (по умолчанию — этот репо)",
    )
    args = parser.parse_args()

    if sys.version_info < (3, 11):
        print("ERROR: требуется Python 3.11+, текущая:", sys.version, file=sys.stderr)
        return 1

    source = Path(args.source).resolve()
    if not _looks_like_cli_package(source):
        print(
            "[install] ERROR: не нашёл исходники CLI в",
            str(source),
            file=sys.stderr,
        )
        print(
            "  Ожидался каталог `client/` монорепо (с pyproject.toml и\n"
            "  src/skills_hub_cli/). Скрипт, видимо, запущен в отрыве от репо.\n"
            "  Решения:\n"
            "    • склонируй репозиторий Skills Hub и запусти client/meta_skill/\n"
            "      scripts/install.py из него;\n"
            "    • либо укажи путь явно:  python install.py --source <repo>/client\n"
            "  Пакета `skills-hub-cli` на PyPI пока нет — установка только из\n"
            "  исходников.",
            file=sys.stderr,
        )
        return 1

    pkg = "-e " + str(source) if args.editable else str(source)
    if _has("pipx"):
        cmd = ["pipx", "install", "--force"] + pkg.split()
    else:
        cmd = [sys.executable, "-m", "pip", "install"] + pkg.split()
    print(f"[install] {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[install] FAILED: {e}", file=sys.stderr)
        return 1

    bin_path = shutil.which("skills-hub") or "(не в PATH)"
    print(f"[install] OK — skills-hub доступен: {bin_path}")
    print("Дальше: skills-hub login <invite-token-or-url>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
