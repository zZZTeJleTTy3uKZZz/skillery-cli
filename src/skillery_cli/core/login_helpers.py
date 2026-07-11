"""Helpers для browser-flow login (HTTP-сервер, валидация callback'а).

Разделены из __main__.py чтобы быть testable (unit-тесты без запуска command).
"""
from __future__ import annotations

import html
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

from skillery_cli import _branding

# Страница, которую браузер показывает после callback'а. Самодостаточная (inline
# CSS, без внешних ресурсов), тёмная/светлая по prefers-color-scheme. Отдаётся с
# charset=utf-8 — кириллица корректна.
_PAGE_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__BRAND__</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; padding:24px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:#0b0b0f; color:#e7e7ea; }
.card { width:min(92vw,420px); padding:44px 36px; border-radius:18px; text-align:center;
  background:#14141a; border:1px solid rgba(255,255,255,.06); }
.badge { width:66px; height:66px; margin:0 auto 22px; border-radius:999px;
  display:grid; place-items:center; background:__ACCENT_BG__; }
.badge svg { width:32px; height:32px; stroke:__ACCENT__; fill:none; stroke-width:2.5;
  stroke-linecap:round; stroke-linejoin:round; }
h1 { margin:0 0 10px; font-size:23px; font-weight:650; letter-spacing:-.01em; }
p { margin:0; font-size:15px; line-height:1.55; color:#a0a0ab; }
.brand { margin-top:30px; font-size:12.5px; letter-spacing:.08em; text-transform:uppercase; opacity:.5; }
@media (prefers-color-scheme: light) {
  body { background:#f6f7f9; color:#16161c; }
  .card { background:#fff; border-color:rgba(0,0,0,.06);
    box-shadow:0 1px 3px rgba(0,0,0,.06),0 12px 32px rgba(0,0,0,.06); }
  p { color:#5c5c68; }
}
</style></head>
<body><div class="card">
<div class="badge"><svg viewBox="0 0 24 24">__ICON__</svg></div>
<h1>__TITLE__</h1><p>__MESSAGE__</p>
<div class="brand">__BRAND__</div>
</div></body></html>"""


def render_callback_page(*, ok: bool, title: str, message: str) -> bytes:
    """Собирает брендированную HTML-страницу callback'а (success/error) в UTF-8.

    Плейсхолдеры (не f-string — CSS полон фигурных скобок): __ACCENT__ и т.п.
    Пользовательский текст экранируется (``html.escape``).
    """
    accent = "#22c55e" if ok else "#ef4444"
    accent_bg = "rgba(34,197,94,.14)" if ok else "rgba(239,68,68,.14)"
    icon = '<path d="M20 6 9 17l-5-5"/>' if ok else '<path d="M18 6 6 18M6 6l12 12"/>'
    brand = _branding.APP_NAME.capitalize()
    return (
        _PAGE_TEMPLATE.replace("__ACCENT_BG__", accent_bg)
        .replace("__ACCENT__", accent)
        .replace("__ICON__", icon)
        .replace("__TITLE__", html.escape(title))
        .replace("__MESSAGE__", html.escape(message))
        .replace("__BRAND__", html.escape(brand))
    ).encode("utf-8")


def validate_callback_state(*, expected: str, actual: str) -> None:
    """Проверяет, что state из callback соответствует переданному ожиданию.

    Выбрасывает ValueError если state не совпадает (защита от CSRF).
    """
    if expected != actual:
        raise ValueError(
            f"state mismatch: expected {expected}, got {actual}"
        )


def generate_state() -> str:
    """Генерирует криптографически стойкий state для browser-flow.

    Используется для защиты от CSRF в callback'е.
    """
    return secrets.token_urlsafe(16)


class CallbackHandler(BaseHTTPRequestHandler):
    """HTTP-handler для GET /callback?code={code}&state={state} из браузера.

    Класс-переменные (проставляются перед запуском сервера):
    - result: dict куда записывать {"code": ..., "error": ...}
    - expected_state: str для валидации state параметра
    """

    result: dict[str, Any] = {}
    expected_state: str = ""

    def do_GET(self) -> None:  # noqa: N802
        """Обработчик GET /callback."""
        parsed_path = urlparse(self.path)

        if parsed_path.path != "/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        # Парсим query-параметры: ?code=…&state=…
        query_params = parse_qs(parsed_path.query)
        code = query_params.get("code", [None])[0]
        state = query_params.get("state", [None])[0]
        error = query_params.get("error", [None])[0]

        # Валидируем state (если не ошибка от сервера)
        if error is None and state:
            try:
                validate_callback_state(expected=self.expected_state, actual=state)
            except ValueError as e:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    render_callback_page(
                        ok=False,
                        title="Не удалось войти",
                        message="Проверка безопасности не прошла. Запустите вход в CLI заново.",
                    )
                )
                self.result["error"] = str(e)
                return

        # Записываем результат
        if error:
            self.result["error"] = error
        elif code:
            self.result["code"] = code
            self.result["state"] = state
        else:
            self.result["error"] = "Missing code and error in callback"

        # Ответ браузеру: брендированная страница (success/error).
        is_ok = "error" not in self.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if is_ok:
            page = render_callback_page(
                ok=True,
                title="Готово",
                message="Вход в CLI выполнен. Вернитесь в терминал — сессия уже активна.",
            )
        else:
            page = render_callback_page(
                ok=False,
                title="Не удалось войти",
                message="Код авторизации не получен. Запустите вход в CLI заново.",
            )
        self.wfile.write(page)

    def log_message(self, format, *args) -> None:  # noqa: A002
        """Подавляем логирование HTTP-запросов в stderr."""
        pass


def start_callback_server(
    host: str = "127.0.0.1", port: int = 0
) -> tuple[HTTPServer, int, str]:
    """Запускает HTTP-сервер на свободном порту, ждёт GET /callback.

    Args:
        host: Адрес для bind (дефолт 127.0.0.1 для loopback).
        port: Порт (0 = OS выбирает свободный).

    Returns:
        (server, assigned_port, state): сервер, выданный порт, сгенерированный state.

    Сервер запускается в отдельном потоке (daemon). Вызывающий код должен:
        1. получить порт и state
        2. запустить браузер с URL, содержащим port и state
        3. дождаться callback'а через server.shutdown()
    """
    server = HTTPServer((host, port), CallbackHandler)
    assigned_port = server.server_port
    state = generate_state()

    # Проставляем expected_state и result-контейнер обработчику
    CallbackHandler.expected_state = state
    CallbackHandler.result = {}

    # Запускаем сервер в daemon-потоке
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    return server, assigned_port, state
