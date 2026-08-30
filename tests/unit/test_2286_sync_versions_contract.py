"""#2286: ``skill sync-versions`` печатал KeyError над УСПЕШНОЙ синхронизацией.

НАЙДЕНО НА ЖИВОМ ПРОДЕ 2026-08-25. Команда выводила «✗ Ошибка: KeyError:
skill_slug» и запускала self-check — при том что задача создавалась и
отрабатывала: в ``sync_jobs`` появлялась запись, статус доходил до ``done``,
версии 0.1.10 и 0.1.11 принимались. То есть команда РАБОТАЛА, но выглядела
сломанной, и человек не знал, что операция прошла.

ПРИЧИНА. После REST-19/26 ``POST /skills/{id}/sync-jobs`` отвечает ВСЕГДА 202 и
отдаёт только квитанцию ``{job_id, status}``; синхронного ``?wait=true``
больше нет. Разбор же остался от прежней синхронной ручки и читал
``p['skill_slug']`` — ключа, которого в квитанции нет и быть не может.

ЧТО СТЕРЕЖЁТ ЭТОТ НАБОР. Две вещи, и вторая важнее первой:

1. ни одна из ТРЁХ форм джобы (принята / сделана / упала) не роняет вывод;
2. разбор не читает ключей, которых бэкенд не объявляет, — иначе такое же
   расхождение контрактов вернётся следующим переименованием поля, и снова
   на проде. Формы взяты из схем хаба (``SyncJobAcceptedResponse``,
   ``SyncJobStatusResponse``) и из того, что кладёт в ``result`` sync-worker.
"""
from __future__ import annotations

from typing import Any

import pytest

from skillery_cli import output as output_module
from skillery_cli.__main__ import (
    SYNC_JOB_ACCEPTED_KEYS,
    SYNC_JOB_RESULT_KEYS,
    SYNC_JOB_STATUS_KEYS,
    _render_sync_job,
)

#: Квитанция постановки — ДОСЛОВНО ``SyncJobAcceptedResponse`` хаба.
ПРИНЯТА: dict[str, Any] = {"job_id": 17, "status": "queued"}

#: Готовая джоба — ``SyncJobStatusResponse`` + ``result`` от sync-worker.
#: Числа взяты из прода 2026-08-25 (версии 0.1.10 и 0.1.11 приняты).
СДЕЛАНА: dict[str, Any] = {
    "job_id": 17,
    "skill_slug": "telegram",
    "status": "done",
    "result": {
        "skill_slug": "telegram",
        "new_versions": ["0.1.10", "0.1.11"],
        "existing_versions": ["0.1.9"],
        "blocked_versions": {"0.1.12": 2},
        "blocked_findings": {
            "0.1.12": [
                {"rule": "aws-key", "file": "src/x.py", "line": 12, "severity": "high"},
                {"rule": "token", "file": "README.md", "line": 3, "severity": "medium"},
            ]
        },
    },
    "error": None,
}

УПАЛА: dict[str, Any] = {
    "job_id": 18,
    "skill_slug": "telegram",
    "status": "error",
    "result": None,
    "error": "GitUnavailable: 403 от origin",
}


#: Все ключи, которые хаб вообще объявляет по джобе. Разбор ОДИН на три формы
#: (иначе форм разбора тоже стало бы три), поэтому и сторож проверяет союз:
#: запрещено читать то, чего нет в контракте ВООБЩЕ, а не то, чего нет в этой
#: конкретной форме — отсутствующее поле законно приходит как None.
ВСЕ_КЛЮЧИ = tuple({*SYNC_JOB_ACCEPTED_KEYS, *SYNC_JOB_STATUS_KEYS})


class _СтрогийОтвет(dict):
    """Словарь, который РУГАЕТСЯ на чтение ключа вне контракта хаба.

    Тихое ``.get`` по опечатке — это тот же баг, только без исключения: он
    вернёт ``None``, и вывод молча обеднеет. Здесь такое чтение падает в
    тесте, а не на проде.
    """

    def __init__(self, данные: dict, разрешено: tuple[str, ...]) -> None:
        super().__init__(данные)
        self._разрешено = set(разрешено)

    def get(self, ключ, по_умолчанию=None):  # type: ignore[override]
        assert ключ in self._разрешено, f"ключа «{ключ}» в контракте хаба нет"
        значение = super().get(ключ, по_умолчанию)
        if ключ == "result" and isinstance(значение, dict):
            return _СтрогийОтвет(значение, SYNC_JOB_RESULT_KEYS)
        return значение

    def __getitem__(self, ключ):  # type: ignore[override]
        assert ключ in self._разрешено, f"ключа «{ключ}» в контракте хаба нет"
        return super().__getitem__(ключ)


@pytest.fixture(autouse=True)
def _текстовый_режим() -> None:
    output_module._mode = "text"


@pytest.mark.parametrize("тело", [ПРИНЯТА, СДЕЛАНА, УПАЛА])
def test_ни_одна_форма_джобы_не_роняет_вывод(тело, capsys) -> None:
    """Тот самый KeyError: у квитанции нет ``skill_slug``, у упавшей — ``result``."""
    _render_sync_job(_СтрогийОтвет(тело, ВСЕ_КЛЮЧИ))
    напечатано = capsys.readouterr().out
    assert напечатано.strip(), "вывод обязан быть — молчание читается как провал"


def test_квитанция_не_выдаётся_за_результат(capsys) -> None:
    """``queued`` — это «ещё считает», а не «ничего не приняли». Разница видна
    только если сказать её вслух."""
    _render_sync_job(dict(ПРИНЯТА))
    напечатано = capsys.readouterr().out
    assert "queued" in напечатано
    assert "ещё выполняется" in напечатано


def test_печатается_то_за_чем_команду_звали(capsys) -> None:
    """Какие версии приняты и какие уже были — ради этого команда и есть."""
    _render_sync_job(dict(СДЕЛАНА))
    напечатано = capsys.readouterr().out
    assert "0.1.10" in напечатано and "0.1.11" in напечатано
    assert "0.1.9" in напечатано
    assert "telegram" in напечатано


def test_блокировка_называет_причину_а_не_только_число(capsys) -> None:
    """«Версия заблокирована» без правила и места не даёт автору навыка ничего,
    кроме тревоги: он не знает, что чинить."""
    _render_sync_job(dict(СДЕЛАНА))
    напечатано = capsys.readouterr().out
    assert "0.1.12" in напечатано
    assert "aws-key" in напечатано and "src/x.py:12" in напечатано


def test_отказ_джобы_называется_текстом_ошибки(capsys) -> None:
    _render_sync_job(dict(УПАЛА))
    assert "GitUnavailable" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_команда_опрашивает_джобу_до_терминального_статуса(monkeypatch) -> None:
    """Ждём результат, а не печатаем квитанцию: без опроса команда физически не
    может ответить на вопрос «какие версии приняты»."""
    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/skills/telegram").mock(
            return_value=Response(200, json={"id": "5", "slug": "telegram"})
        )
        постановка = router.post("/skills/5/sync-jobs").mock(
            return_value=Response(202, json=ПРИНЯТА)
        )
        опрос = router.get("/skills/telegram/sync-jobs/17").mock(
            return_value=Response(200, json=СДЕЛАНА)
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            принята = await client.sync_skill("telegram")
            assert set(принята) == set(SYNC_JOB_ACCEPTED_KEYS), (
                "хаб отдаёт КВИТАНЦИЮ; версий в ней нет и быть не может"
            )
            состояние = await client.get_sync_job("telegram", str(принята["job_id"]))
        finally:
            await client.close()

    assert постановка.called and опрос.called
    assert состояние["result"]["new_versions"] == ["0.1.10", "0.1.11"]
