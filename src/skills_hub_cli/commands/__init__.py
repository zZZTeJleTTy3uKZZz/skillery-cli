"""Modular CLI command groups (E23).

Каждый модуль экспортирует **функции** ``cmd_*`` + ``register(app)``.
``register`` зовётся из ``skills_hub_cli.__main__:build_app`` если у
залогиненного user'а есть нужные permissions.
"""
