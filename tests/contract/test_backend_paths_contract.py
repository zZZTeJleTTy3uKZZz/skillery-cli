"""Контракт CLI ↔ OpenAPI backend (#1441).

**Зачем этот тест существует.** В ``core/transport.py`` больше сотни литералов
путей, и до этого теста ни один из них ни с чем не сверялся. Весь фоновый путь
демона глушит исключения (``contextlib.suppress`` / ``except Exception: return``),
поэтому переименование пути очереди заданий на backend проходило CI **зелёным**,
а на машинах владельца выглядело как «устройство просто перестало получать
задания»: без падения, без симптома, без строчки в логе.

Тест берёт снимок путей backend (``tests/contract/backend-paths.json``, снимается
``scripts/refresh_backend_paths.py``) и сверяет с ним **каждый** вызов из
``transport.py``, падая с адресом первого расхождения.

Живой режим: ``SKILLERY_OPENAPI_URL=https://hub.skillery.ru/api/openapi.json``
— сверка с реально задеплоенным backend вместо снимка. Именно в этом режиме тест
гоняется после деплоя как приёмка.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from tests.contract.extract_paths import PathUse, extract, normalize

_TRANSPORT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "skillery_cli"
    / "core"
    / "transport.py"
)
_SNAPSHOT = Path(__file__).with_name("backend-paths.json")

#: Вызовы, без которых демон немой. Проверяются поимённо: если кто-то удалит из
#: transport.py и метод, и путь, общий тест останется зелёным — а устройство
#: перестанет получать задания.
DAEMON_CRITICAL: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/me/device-queue"),
        ("GET", "/me/devices/queue/stream"),
        ("POST", "/me/device-queue/report"),
        ("POST", "/devices/{}/tasks/{}/report"),
        ("POST", "/me/devices"),
        ("GET", "/me/installs"),
        ("POST", "/auth/refresh"),
        ("GET", "/skills/{}/install-bundle"),
    }
)


def load_backend_paths() -> dict[str, set[str]]:
    """``{нормализованный путь: {методы}}`` — из живого backend или из снимка."""
    url = os.environ.get("SKILLERY_OPENAPI_URL")
    if url:
        with urllib.request.urlopen(url, timeout=30) as resp:
            spec = json.loads(resp.read().decode("utf-8"))
        raw = {path: list(ops) for path, ops in spec["paths"].items()}
    else:
        raw = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))["paths"]
    known: dict[str, set[str]] = {}
    for path, methods in raw.items():
        known.setdefault(normalize(path), set()).update(
            m.upper() for m in methods
        )
    return known


def violations(
    uses: list[PathUse], known: dict[str, set[str]]
) -> list[str]:
    """Список расхождений «CLI зовёт → backend не умеет». Пусто = контракт цел."""
    out: list[str] = []
    for use in uses:
        methods = known.get(use.path)
        if methods is None:
            out.append(f"пути нет на backend: {use}")
        elif use.method not in methods:
            out.append(
                f"метод не поддержан (backend знает {sorted(methods)}): {use}"
            )
    return out


@pytest.fixture(scope="module")
def backend_paths() -> dict[str, set[str]]:
    return load_backend_paths()


@pytest.fixture(scope="module")
def transport_uses() -> list[PathUse]:
    return extract(_TRANSPORT)


def test_transport_extraction_is_not_empty(transport_uses: list[PathUse]) -> None:
    """Страховка самого извлечения: молчаливый ноль путей = тест-пустышка."""
    assert len(transport_uses) >= 100, (
        "AST-извлечение путей сломалось: из transport.py достали "
        f"{len(transport_uses)} вызовов. Тест, который ничего не проверяет, "
        "опаснее отсутствующего."
    )
    used = {(u.method, u.path) for u in transport_uses}
    missing = sorted(DAEMON_CRITICAL - used)
    assert not missing, (
        f"из transport.py пропали критичные для демона вызовы: {missing}. "
        "Если путь переименован — обнови DAEMON_CRITICAL тем же коммитом."
    )


def test_every_transport_path_exists_in_backend(
    transport_uses: list[PathUse], backend_paths: dict[str, set[str]]
) -> None:
    """Каждый путь из transport.py существует на backend с тем же методом."""
    found = violations(transport_uses, backend_paths)
    assert not found, (
        "CLI зовёт то, чего backend не знает — демон получит 404 и замолчит:\n"
        + "\n".join(found)
        + "\n\nПочини transport.py либо пересними контракт: "
        "python scripts/refresh_backend_paths.py"
    )


def test_daemon_paths_are_served_by_backend(
    backend_paths: dict[str, set[str]]
) -> None:
    """Backend отдаёт все роуты, на которых висит демон."""
    missing = sorted(
        (m, p) for m, p in DAEMON_CRITICAL if m not in backend_paths.get(p, set())
    )
    assert not missing, f"backend не отдаёт критичные для демона роуты: {missing}"


def test_backend_rename_breaks_the_contract(
    transport_uses: list[PathUse], backend_paths: dict[str, set[str]]
) -> None:
    """Имитация поломки: путь очереди переименован на backend → тест падает.

    Это и есть доказательство, что гейт работает. Без него «зелёный контрактный
    тест» ничего не гарантирует: он может быть зелёным потому, что ничего не
    сверяет.
    """
    mutated = dict(backend_paths)
    victim = "/me/device-queue"
    assert victim in mutated, "снимок backend устарел — пересними контракт"
    del mutated[victim]
    mutated["/me/devices/{}/tasks"] = {"GET"}

    found = violations(transport_uses, mutated)
    assert any(victim in item for item in found), (
        "переименование пути очереди на backend НЕ уронило проверку — гейт "
        "фиктивный"
    )
