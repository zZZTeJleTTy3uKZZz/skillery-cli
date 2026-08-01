"""Регресс #1387/0.5.80: зеркалирование режима не должно ломать ContextVar.

Мина, сработавшая на 0.5.79: `_sync_clikit_mode` присваивал
`_clikit._mode = _mode`, подменяя ContextVar строкой. После этого
`is_json()` звал `.get()` у строки и падал — ЛЮБАЯ команда CLI умирала.
Тест держит оба контракта: новый clikit (ContextVar) и старый (строка).
"""
from contextvars import ContextVar

from skillery_cli import output as sk_output


def test_ctxvar_mode_survives_sync(monkeypatch) -> None:
    """clikit >=0.1.7: ContextVar обновляется через .set(), а не подменяется."""
    var: ContextVar[str] = ContextVar("mode_probe", default="json")
    fake = type("FakeClikit", (), {"_mode": var})()
    monkeypatch.setattr(sk_output, "_clikit", fake)
    monkeypatch.setattr(sk_output, "_mode", "text")

    sk_output._sync_clikit_mode()

    assert fake._mode is var, "ContextVar подменён — is_json() упадёт"
    assert var.get() == "text"


def test_plain_attr_mode_still_mirrored(monkeypatch) -> None:
    """Старый clikit со строковым _mode продолжает работать."""
    fake = type("FakeClikit", (), {"_mode": "json"})()
    monkeypatch.setattr(sk_output, "_clikit", fake)
    monkeypatch.setattr(sk_output, "_mode", "text")

    sk_output._sync_clikit_mode()

    assert fake._mode == "text"
