"""Пересъёмка контракта путей backend → ``tests/contract/backend-paths.json``.

Снимок намеренно компактный (путь → методы, ~10 КБ вместо 650 КБ полного
OpenAPI): контрактный тест #1441 сверяет только адресацию, а тащить в репозиторий
все схемы значит получать конфликт на каждом изменении любого DTO.

Источники, по убыванию удобства::

    python scripts/refresh_backend_paths.py            # локальный backend из соседнего репо
    python scripts/refresh_backend_paths.py --backend ../skillery-backend-wt-canon
    python scripts/refresh_backend_paths.py --url https://hub.skillery.ru/api/openapi.json
    python scripts/refresh_backend_paths.py --file /путь/openapi.json

``--backend`` нужен, когда правки backend ещё в worktree ветки: снимок обязан
сниматься с ТОГО кода, который поедет в релиз, а не с соседнего ``dev``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUT = _ROOT / "tests" / "contract" / "backend-paths.json"
_DEFAULT_BACKEND = _ROOT.parent / "skillery-backend"

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _from_local_backend(backend: Path) -> dict:
    """Импортирует ``create_app`` соседнего репозитория его же venv.

    ``backend`` может быть worktree ветки: ``src`` кладётся в НАЧАЛО
    ``sys.path``, поэтому берётся код именно этого дерева, а не того, на
    который venv установлен editable.
    """
    if not backend.exists():
        raise SystemExit(
            f"локального backend нет ({backend}); используй --url или --file"
        )
    venv = backend / ".venv"
    if not venv.exists():
        # worktree обычно без своего venv — берём venv основного репозитория.
        venv = _DEFAULT_BACKEND / ".venv"
    python = venv / "Scripts" / "python.exe"
    if not python.exists():
        python = venv / "bin" / "python"
    if not python.exists():
        raise SystemExit(f"venv backend не найден в {venv}")
    code = (
        "import json,sys;"
        "sys.path.insert(0, r'%s');"
        "from skills_hub_backend.interface.http.app import create_app;"
        "print(json.dumps(create_app().openapi()))" % (backend / "src")
    )
    result = subprocess.run(
        [str(python), "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(backend),
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"backend не отдал OpenAPI:\n{result.stderr}")
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="URL openapi.json живого backend")
    parser.add_argument("--file", help="локальный openapi.json")
    parser.add_argument(
        "--backend",
        help="каталог репозитория/worktree backend (по умолчанию ../skillery-backend)",
    )
    args = parser.parse_args(argv)

    if args.url:
        with urllib.request.urlopen(args.url, timeout=60) as resp:
            spec = json.loads(resp.read().decode("utf-8"))
        source = args.url
    elif args.file:
        spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
        source = args.file
    else:
        backend = Path(args.backend).resolve() if args.backend else _DEFAULT_BACKEND
        spec = _from_local_backend(backend)
        source = "skillery-backend create_app().openapi()"

    paths = {
        path: sorted(m.upper() for m in ops if m.lower() in _HTTP_METHODS)
        for path, ops in sorted(spec["paths"].items())
    }
    _OUT.write_text(
        json.dumps(
            {"source": source, "title": spec["info"]["title"], "paths": paths},
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"{_OUT}: {len(paths)} путей из {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
