"""Modular CLI command groups.

Каждый модуль экспортирует **функции** ``cmd_*`` + ``register(app)``.
``register`` зовётся из ``skillery_cli.__main__:build_app`` если у
залогиненного user'а есть нужные permissions.
"""
