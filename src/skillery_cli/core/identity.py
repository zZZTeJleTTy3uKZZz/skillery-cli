"""Стабильная идентичность устройства (client_device_id).

Зачем: web↔CLI мост связывает CLI-сессию с конкретным устройством и рисует флаг
«подключено» / кнопку «Переподключить». Раньше идентичность устройства была =
hostname (имя в User-Agent + ключ upsert в реестре). Из-за этого:

- переименование устройства в вебе рвало связь (UA несёт hostname, а не новое
  имя → матч терялся);
- «Переподключить» после ренейма плодило дубль (upsert по имени не находил
  строку с прежним hostname → вставлял новую).

Решение: **client-generated стабильный UUID**, живущий в отдельном файле
``<config_dir>/device_id`` (НЕ в config.toml — переживает сброс конфига
self-heal-доктором). Он едет в UA как ``id:<uid>`` и в теле регистрации
устройства; backend матчит/апсертит по нему, а ``name`` становится ЧИСТО
визуальным лейблом, который можно свободно переименовывать.

Идентичность — **на машину, не на профиль**: физическое устройство одно
независимо от того, под каким профилем/аккаунтом логинятся. Поэтому файл берём
из БАЗОВОГО каталога конфига (env ``SKILLERY_CONFIG_DIR`` учитывается, профиль —
нет), чтобы UA (строится при импорте, до установки профиля) и регистрация
(runtime, профиль уже задан) давали ОДИН и тот же id.
"""
from __future__ import annotations

import hashlib
import socket
import uuid
from pathlib import Path

from skillery_cli import _branding
from skillery_cli.config import _resolve_home_base

_CACHED: str | None = None


def _device_id_file() -> Path:
    """``<base_config_dir>/device_id`` — БЕЗ учёта профиля (id на машину)."""
    base = _resolve_home_base(_branding.env("CONFIG_DIR"), f"~/{_branding.HOME_DIR_NAME}")
    return base / "device_id"


def _fallback_uid() -> str:
    """Детерминированный id из hostname — если файл недоступен (RO-FS и т.п.).

    Стабилен между запусками (одинаковый hostname → одинаковый id), поэтому
    связка device↔session продолжает работать даже без персистентного файла.
    """
    raw = (socket.gethostname() or "cli").encode("utf-8", "replace")
    return "h" + hashlib.sha256(raw).hexdigest()[:31]


def device_uid() -> str:
    """Стабильный client_device_id этой машины (кэш в памяти на процесс).

    Порядок: файл ``device_id`` → если пуст/нет, генерируем UUID4 и пишем →
    при любой ошибке I/O — детерминированный fallback от hostname. Никогда не
    бросает: используется при построении User-Agent на импорте.
    """
    global _CACHED
    if _CACHED:
        return _CACHED
    try:
        f = _device_id_file()
        if f.is_file():
            val = f.read_text(encoding="utf-8").strip()
            if val:
                _CACHED = val
                return val
        f.parent.mkdir(parents=True, exist_ok=True)
        val = uuid.uuid4().hex
        f.write_text(val, encoding="utf-8")
        _CACHED = val
        return val
    except Exception:  # noqa: BLE001 — идентичность не должна валить импорт/запрос
        val = _fallback_uid()
        _CACHED = val
        return val


def reset_cache() -> None:
    """Сбросить in-memory кэш (для тестов, меняющих SKILLERY_CONFIG_DIR)."""
    global _CACHED
    _CACHED = None


__all__ = ["device_uid", "reset_cache"]
