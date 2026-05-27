"""Установщик skills-hub CLI.

Запускается агентом клиента при первом вызове skill'а:
    python install.py [--editable]

Логика:
1. Проверяет что есть Python >= 3.11.
2. Устанавливает skills-hub-cli через pip / pipx (предпочтительнее pipx).
3. Сообщает путь к бинарю.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[2]  # client/


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


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

    pkg = "-e " + args.source if args.editable else args.source
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
