"""Прогон тестов не имеет права трогать БОЕВОЙ ``~/.skillery``.

Живой инцидент 2026-07-24: в ``daemon.log`` владельца машины оказались записи с
именами тест-функций (``test_roundtrip_push_then_pull_syncs_set.<locals>._spy_chain()``),
а апгрейд-мьютекс ``Global\\SkilleryUpgradeSingleton``, взятый тестом, заставил
НАСТОЯЩИЙ демон считать «апгрейд уже идёт» и пропустить push-задачу ``cli_upgrade``.

Инвариант проверяем через подменённый HOME (``tests/conftest.py``), НЕ читая и
не трогая настоящий каталог: достаточно убедиться, что каждый резолвер путей
уводит внутрь tmp_path, а имена ядерных локов — не боевые.
"""
from __future__ import annotations

from pathlib import Path

from tests.conftest import REAL_HOME


def test_path_home_points_into_tmp(isolated_home: Path) -> None:
    assert Path.home() == isolated_home
    assert Path.home() != REAL_HOME


def test_all_state_resolvers_live_under_tmp_home(isolated_home: Path) -> None:
    """config / logs / очередь / локи демона и апгрейда — всё внутри временного HOME."""
    from telemetrykit import outbox

    from skillery_cli import _upgrade_worker
    from skillery_cli.config import _default_config_dir, _default_store_dir
    from skillery_cli.core.analytics_sync import legacy_queue_path
    from skillery_cli.core.logging_setup import log_dir
    from skillery_cli.daemon import single_instance
    from skillery_cli.daemon.daemon_runner import (
        default_guard_path,
        default_pid_path,
        default_state_path,
    )

    resolved = [
        _default_config_dir(),
        _default_store_dir(),
        log_dir(),
        single_instance._lock_path(),
        _upgrade_worker.lock_path(),
        _upgrade_worker.result_path(),
        default_pid_path(),
        default_state_path(),
        default_guard_path(),
        # ОБЩАЯ исходящая очередь (#1174/#1180) — единственная на машине.
        outbox.path(),
        # Наследство третьей очереди: путь миграции тоже обязан быть в tmp.
        legacy_queue_path(),
    ]
    # ⚠️ tmp_path на Windows сам лежит внутри профиля пользователя, поэтому
    # сверяемся не с REAL_HOME, а с БОЕВЫМ каталогом состояния ``~/.skillery``.
    real_state = REAL_HOME / ".skillery"
    for path in resolved:
        p = Path(path)
        assert isolated_home in p.parents or p == isolated_home, (
            f"{path} уходит мимо изолированного HOME (боевой каталог в опасности)"
        )
        assert p != real_state and real_state not in p.parents, (
            f"{path} ведёт в боевой ~/.skillery"
        )


def test_kernel_locks_use_isolated_names() -> None:
    """Мьютекс живёт в ЯДРЕ, а не в HOME — имя обязано быть тестовым."""
    from skillery_cli import _upgrade_worker
    from skillery_cli.daemon import single_instance

    assert single_instance.default_mutex_name() != single_instance._MUTEX_NAME
    assert _upgrade_worker.mutex_name() != _upgrade_worker.MUTEX_NAME
    # Боевые имена — Global\…; тестовые не должны их повторять.
    assert "SkilleryDaemonSingleton" not in single_instance.default_mutex_name()
    assert "SkilleryUpgradeSingleton" not in _upgrade_worker.mutex_name()


def test_writing_state_lands_in_tmp_not_real_home(isolated_home: Path) -> None:
    """Реальная запись (лог демона) появляется во временном HOME."""
    from skillery_cli.core.logging_setup import install_logger, log_dir

    install_logger("daemon.log").info("проверка изоляции")
    target = log_dir() / "daemon.log"
    assert target.is_file()
    assert isolated_home in target.parents
