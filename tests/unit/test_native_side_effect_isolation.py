"""Unit tests cannot register host services or spawn detached native daemons."""
import os
from pathlib import Path
import sys

import pytest

from skillery_cli.commands import daemon
from skillery_cli.daemon import autostart


def test_appdata_belongs_to_isolated_home() -> None:
    assert Path(os.environ["APPDATA"]).is_relative_to(Path.home())
    assert Path(os.environ["LOCALAPPDATA"]).is_relative_to(Path.home())


def test_native_scheduler_command_requires_an_explicit_mock() -> None:
    with pytest.raises(AssertionError, match="native daemon"):
        autostart.proc_run([sys.executable, "-c", "pass"], timeout=5)


def test_native_daemon_spawn_requires_an_explicit_mock() -> None:
    with pytest.raises(AssertionError, match="native daemon"):
        process = daemon.proc_popen([sys.executable, "-c", "pass"])
        process.wait(timeout=5)
