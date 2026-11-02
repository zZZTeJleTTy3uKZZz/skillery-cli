"""Локальные пути продуктов: HOME, bin, конфиги агентов, состояние.

Все пути вычисляются В МОМЕНТ вызова, а не при импорте: тесты подменяют HOME
(и это единственный способ проверить установку, не залезая в боевой каталог),
а у пользователя HOME тоже не константа — на Windows он переезжает вместе с
профилем.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from skillery_cli import _branding

#: Имя профиля Harness по умолчанию; переопределяется ``DSH_PROFILE``.
DEFAULT_DSH_PROFILE = "desktop"


@dataclass(frozen=True)
class HomePaths:
    """Куда что ставится на этом устройстве."""

    home: Path
    bin_dir: Path
    claude_dir: Path
    claude_settings: Path
    claude_plugins: Path
    dsh_home: Path
    dsh_plugins: Path
    dsh_profile: Path
    skill_store: Path
    products_file: Path
    downloads_dir: Path

    @property
    def dsh_patch(self) -> Path:
        """Файл раскладки профиля Harness: сюда пишется блок ``insert:``."""
        return self.dsh_profile / "cordis.patch.yml"


def _is_windows() -> bool:
    return sys.platform == "win32" or os.name == "nt"


def home_paths() -> HomePaths:
    """Собрать пути от текущего HOME (и env-оверрайдов раскладок)."""
    home = Path.home()
    config_dir = Path(
        os.environ.get(_branding.env("CONFIG_DIR")) or home / _branding.HOME_DIR_NAME
    ).expanduser()
    bin_dir = path_bin_dir(home)
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
    dsh_home = Path(os.environ.get("DSH_HOME") or home / ".dsh").expanduser()

    profile_value = os.environ.get("DSH_PROFILE", "").strip()
    if profile_value and any(sep in profile_value for sep in ("/", "\\")):
        dsh_profile = Path(profile_value).expanduser()
    else:
        dsh_profile = dsh_home / "profiles" / (profile_value or DEFAULT_DSH_PROFILE)

    return HomePaths(
        home=home,
        bin_dir=bin_dir,
        claude_dir=claude_dir,
        claude_settings=claude_dir / "settings.json",
        claude_plugins=claude_dir / "dsh-plugins",
        dsh_home=dsh_home,
        dsh_plugins=dsh_home / "plugins",
        dsh_profile=dsh_profile,
        skill_store=config_dir / "store",
        products_file=config_dir / "products.json",
        downloads_dir=config_dir / "cache" / "installers",
    )


def path_bin_dir(home: Path) -> Path:
    """Каталог бинарей: POSIX ``~/.local/bin``, Windows ``%LOCALAPPDATA%\\skillery\\bin``."""
    if _is_windows():
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / _branding.APP_NAME / "bin"
    return home / ".local" / "bin"
