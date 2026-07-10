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
                self.end_headers()
                html_response = f"""<html><body>
<h1>Ошибка</h1>
<p>{html.escape(str(e))}</p>
</body></html>"""
                self.wfile.write(html_response.encode("utf-8"))
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

        # Ответ браузеру: минималистичный HTML по-русски
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html_response = """<html><head><meta charset="utf-8"></head><body>
<h1>Готово</h1>
<p>Вернитесь в терминал для завершения входа.</p>
</body></html>"""
        self.wfile.write(html_response.encode("utf-8"))

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
