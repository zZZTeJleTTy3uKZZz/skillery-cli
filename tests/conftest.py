"""Глобальная настройка client-тестов.

CLI всегда конфигурирует кит ``skillkit`` под бренд **skillery** при старте
(``core/_kit_config`` → ``skillkit.configure``). Делаем то же для тестов, чтобы
брендо-зависимое поведение (сообщения «не управляется skillery», rc-маркер
PATH, проектный манифест ``.skillery/skills.toml`` + legacy ``.skillery``
fallback) было детерминированным в т.ч. при запуске одного теста в изоляции
(иначе бренд зависит от того, импортнулся ли транзитивно один из шимов).

ВТОРОЕ (и не менее важное) — ИЗОЛЯЦИЯ HOME. Живой инцидент 2026-07-24: прогон
pytest писал в БОЕВОЙ ``~/.skillery`` владельца машины (в ``daemon.log``
находились записи с именами тест-функций), а апгрейд-мьютекс
``Global\\SkilleryUpgradeSingleton``, взятый тестом, заставлял НАСТОЯЩИЙ демон
считать «апгрейд уже идёт» и пропускать push-задачу ``cli_upgrade``. Поэтому
на каждый тест:

- ``HOME``/``USERPROFILE``/``HOMEDRIVE``/``HOMEPATH`` → в ``tmp_path`` (это
  накрывает ВСЁ, что резолвится от ``Path.home()``: ``config._default_config_dir``,
  ``logging_setup.log_dir``, ``single_instance._lock_path``,
  ``_upgrade_worker.lock_path``/``result_path``/``upgrade.log``);
- env-оверрайды каталогов (``SKILLERY_CONFIG_DIR``/``SKILLERY_STORE_DIR``)
  снимаются — иначе разработческая переменная увела бы тест обратно в боевой
  каталог;
- имена ЯДЕРНЫХ локов (демона и апгрейда) уводятся на уникальные per-run —
  мьютекс живёт в ядре, а не в HOME, и HOME-изоляция его не покрывает.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

# Сайд-эффект импорта: skillkit.configure(SkillkitConfig(app_name="skillery", ...)).
import skillery_cli.core._kit_config  # noqa: F401
import sys


def pytest_configure(config: pytest.Config) -> None:
    """Прогон обязан идти интерпретатором ОКРУЖЕНИЯ ПРОЕКТА (skl#2545).

    01.09.2026 `uv run pytest` молча брал СИСТЕМНЫЙ pytest: инструменты прогона
    были объявлены как extra, а без `--extra dev` их в окружении нет. Тесты шли
    чужим интерпретатором с чужими версиями зависимостей — то есть проверяли не
    то, что собирается и уезжает пользователю. Прогон был зелёным, пока у живого
    человека падал `skillery login`.

    Зелёный прогон не того окружения хуже красного: он гасит тревогу.
    """
    корень = Path(__file__).resolve().parent.parent
    окружение = корень / ".venv"
    if not окружение.exists():
        return  # CI и чужие сборки ставят зависимости иначе — там судить не о чем

    свой = str(окружение).lower()
    текущий = str(Path(sys.prefix).resolve()).lower()
    if not текущий.startswith(свой):
        raise pytest.UsageError(
            "pytest запущен НЕ из окружения проекта." + chr(10)
            + f"  интерпретатор: {sys.executable}" + chr(10)
            + f"  ожидалось внутри: {окружение}" + chr(10)
            + "Проверяется не то, что собирается. Выполни: uv sync && uv run pytest"
        )


#: Настоящий HOME, зафиксированный ДО любой подмены — тесты сверяются с ним,
#: не читая и не трогая его содержимое.
REAL_HOME = Path.home()

#: Уникальные на прогон имена ядерных локов: тест не должен «занимать» лок,
#: который слушает живой демон/апгрейдер на этой же машине.
_RUN_ID = uuid.uuid4().hex[:12]
DAEMON_MUTEX_ENV = "SKILLERY_DAEMON_MUTEX"
UPGRADE_MUTEX_ENV = "SKILLERY_UPGRADE_MUTEX"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME каждого теста — в ``tmp_path``; боевой ``~/.skillery`` неприкосновенен."""
    home = tmp_path / "_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    drive, tail = os.path.splitdrive(str(home))
    monkeypatch.setenv("HOMEDRIVE", drive or "")
    monkeypatch.setenv("HOMEPATH", tail or str(home))
    # Оверрайды каталогов снимаем: с ними ~ не при чём и тест ушёл бы в боевой путь.
    for var in ("SKILLERY_CONFIG_DIR", "SKILLERY_STORE_DIR", "SKILLERY_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    # Ядерные локи — на уникальное имя (мьютекс не живёт в HOME).
    monkeypatch.setenv(DAEMON_MUTEX_ENV, f"Local\\SkilleryDaemonTest-{_RUN_ID}")
    monkeypatch.setenv(UPGRADE_MUTEX_ENV, f"Local\\SkilleryUpgradeTest-{_RUN_ID}")
    return home


@pytest.fixture(autouse=True)
def _reset_outbox_throttle():
    """Троттл/backoff воркера общего outbox'а — ПРОЦЕССНОЕ состояние.

    Оно живёт в модуле (окно «не чаще раза в N секунд» общее на процесс), а
    значит протекает между тестами: тест, сходивший по сети, глушил бы соседа
    троттлом или backoff'ом. Сбрасываем до и после каждого теста — иначе порядок
    прогона начинает влиять на результат (ровно тот класс флейков, который тут
    уже ловили с ``matchMedia``).
    """
    from skillery_cli.core import outbox_worker

    outbox_worker.reset_throttle()
    yield
    outbox_worker.reset_throttle()
