"""Skillery CLI — клиент."""
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Версию берём из МЕТАДАННЫХ установленного дистрибутива (их пишет uv/pip из
# pyproject при сборке), а не из ручной константы: хардкод уже разъезжался с
# pyproject (0.5.58 ≠ 0.5.60) — CLI сообщал о себе старую версию, апдейт-чек
# вечно видел «доступно обновление», после апгрейда версия не менялась → цикл.
# Fallback-строка нужна лишь при запуске из исходников без установки как дистрибутив;
# держим её в синхроне с pyproject, но правда о версии — метаданные.
try:
    __version__ = _pkg_version("skillery-cli")
except PackageNotFoundError:  # dev-запуск из дерева исходников, не установлен
    __version__ = "0.5.71"
