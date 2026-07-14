"""``skillery ask`` — реальный AI-адвайзер навыков из CLI (тот же, что в вебе).

В отличие от ``suggest`` (лексический/семантический подбор по стору+хабу), ``ask``
ходит в LLM-адвайзер бэкенда (``POST /advisor/messages``, SSE): настоящий ответ
языковой модели с RAG по каталогу (RBAC-фильтрован) + карточки рекомендованных
навыков. Ответ стримится токенами по мере генерации.

Беседа персистентна: ``--conversation/-c <id>`` продолжает существующую (id
печатается после ответа и в ``meta``-событии), без флага — создаётся новая.
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console

from skillery_cli.commands._common import make_client, run
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data, emit_error, is_json

console = Console()


def cmd_ask(
    query: str = typer.Argument(
        ..., metavar="ЗАПРОС", help="Вопрос адвайзеру: «чем автоматизировать релизы?»"
    ),
    conversation_id: int | None = typer.Option(
        None,
        "--conversation",
        "-c",
        help="Продолжить существующую беседу по её id (иначе — новая).",
    ),
) -> None:
    """Спросить AI-адвайзера навыков (реальный LLM + RAG по каталогу).

    Требует логина (адвайзер авторизован, ретривал RBAC-фильтрован). Ответ
    стримится; в конце — карточки рекомендованных навыков и id беседы для
    продолжения через ``-c``.
    """
    cfg = ClientConfig.load()
    if not cfg.is_logged_in():
        emit_error("NOT_LOGGED_IN", "Сначала залогиньтесь: skillery login")
        raise typer.Exit(1)
    from skillery_cli.config import load_tokens

    access, _ = load_tokens(cfg.user_email or "")
    if not access:
        emit_error("NO_TOKEN", "Локальный access-токен не найден. Сделайте login заново.")
        raise typer.Exit(1)

    json_mode = is_json()

    async def _do() -> None:
        client = make_client(cfg, access)
        conv_id: int | None = conversation_id
        answer: list[str] = []
        cards: list[dict[str, Any]] = []
        try:
            if not json_mode:
                console.print("[bold cyan]Адвайзер:[/] ", end="")
            async for event, data in client.advisor_stream(
                message=query, conversation_id=conversation_id
            ):
                if event == "meta":
                    conv_id = data.get("conversation_id", conv_id)
                elif event == "token":
                    text = str(data.get("text", ""))
                    answer.append(text)
                    if not json_mode:
                        # Живой токен-стрим: печатаем дельту без переноса.
                        console.print(text, end="", soft_wrap=True)
                elif event == "skills":
                    cards = list(data.get("skills") or [])
                elif event == "done":
                    conv_id = data.get("conversation_id", conv_id)
        finally:
            await client.close()

        payload = {
            "conversation_id": conv_id,
            "answer": "".join(answer),
            "skills": cards,
        }
        emit_data(payload, text_renderer=lambda p: _render_tail(p, cards))

    run(_do())


def _render_tail(payload: dict[str, Any], cards: list[dict[str, Any]]) -> None:
    """Текстовый хвост после стрима: карточки навыков + id беседы + подсказки."""
    console.print()  # закрыть строку токен-стрима
    if cards:
        console.print("\n[bold]Рекомендованные навыки:[/]")
        for c in cards:
            slug = c.get("slug") or c.get("skill_id")
            title = c.get("title") or ""
            reason = c.get("reason") or ""
            console.print(f"  • [green]{slug}[/] — {title}")
            if reason:
                console.print(f"    [dim]{reason}[/]")
        console.print(
            "\n[dim]Установить: [/]skillery install <slug>"
        )
    conv_id = payload.get("conversation_id")
    if conv_id is not None:
        console.print(
            f"[dim]Продолжить беседу: [/]skillery ask -c {conv_id} \"...\""
        )
    console.print(
        "[dim]Ответ сгенерирован ИИ и может быть неточным — проверяйте.[/]"
    )


def register(app: typer.Typer) -> None:
    app.command(name="ask")(cmd_ask)
