"""#1387: живые креды демона — подхват нового логина и видимая протухшая сессия.

Что было
--------
``_build_runner`` читал ``ClientConfig`` и access-токен РОВНО ОДИН РАЗ, при
построении, и держал их в памяти всё время жизни процесса. Демон переживает
перезагрузку и работает сутками — а значит, после ``skillery auth login``
живой процесс продолжал слать со старым токеном. Живой инцидент: демон,
поднятый 30.07, за сутки сделал 47808 циклов, ``sent=7292``, ``accepted=0``,
outbox вырос до 739 конвертов. Ручной ``daemon stop`` + ``daemon start`` чинил
это мгновенно (``cycles=2 sent=100 accepted=100``) — то есть сутки работы
вхолостую разделяло от нормы одно перечитывание файла.

Хуже того, наблюдаемости не было никакой: в логе безликий ``ConnectError``, в
``status`` тишина, ``doctor`` отвечал «известных авто-починок не нашлось».

Механизм подхвата: почему именно такой
--------------------------------------
Рассматривались четыре варианта.

1. **Перечитывать конфиг каждый цикл.** Просто, но такт long-poll'а — ~2 с, а
   чтение токена — это поход в Windows Credential Manager; плюс гонка с
   записью конфига (можно прочитать файл в момент перезаписи).
2. **Следить за mtime и перечитывать при изменении.** ``login`` ВСЕГДА пишет
   ``config.toml`` (почта + права), поэтому изменение файла — честный сигнал
   «креды сменились». Стоимость — один ``os.stat`` на такт (микросекунды),
   дорогой путь (``ClientConfig.load`` + keyring) выполняется только по факту
   изменения.
3. **При 401 попробовать refresh, а если не вышло — перечитать конфиг.**
   Обязательная часть, но НЕ достаточная: она реагирует только тогда, когда
   есть что отправлять, и не покрывает простой демона.
4. **Сигналить живому демону файлом-флагом при login.** Тот же самый файловый
   сигнал, что и (2), только со СВОИМ состоянием, которое можно потерять,
   забыть удалить или не записать (login с другого профиля, чужой процесс,
   переустановка). Лишняя движущаяся часть без выигрыша над mtime.

Выбрано **(2) + (3)**: stamp-watch (``st_mtime_ns``/``st_size`` конфига И
файла токенов) как основной, дешёвый и не зависящий от трафика механизм, плюс
инвалидация по 401 в единственной точке, где 401 вообще виден — callback
обновления токена. Перезапуск демона при login сознательно НЕ используется:
демон обязан переживать смену кред, а не умирать от неё.

Видимое состояние вместо молотьбы вхолостую
-------------------------------------------
Если 401 пришёл и refresh НЕ помог — это «нужен вход», а не сетевой сбой.
Тогда:

- состояние пишется в ``~/.skillery/daemon.auth.json`` — его читают
  ``daemon status`` и ``doctor`` (файл переживает смерть демона, поэтому
  диагностика работает и по факту, а не только на живом процессе);
- в ``daemon.log`` уходит WARNING с причиной (а не безликий ``ConnectError``);
- клиент больше НЕ строится: отправлять заведомо отвергаемое незачем, очередь
  ничего не теряет (401 — повторяемый статус, конверты остаются в outbox).

Из этого состояния демон выходит сам, без ручного перезапуска, двумя путями:
изменились креды (stamp) — сразу; либо истекло ``RECHECK_AFTER_SEC`` — тогда
разрешается одна пробная попытка (сервер могли починить, токен мог обновить
другой процесс).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skillery_cli.core.logging_setup import session_logger


def _session_log() -> logging.Logger:
    """Журнал сессии: ВСЕГДА пишет в ``daemon.log``, минуя общий уровень ERROR.

    Через обычный ``skillery.daemon`` WARNING в файл не попадал бы — и владелец,
    открыв лог после суток холостой работы, снова не увидел бы причины.
    """
    return session_logger("daemon.log")


#: Состояния сессии демона.
AUTH_OK = "ok"
AUTH_NEEDS_LOGIN = "needs_login"

#: Через сколько секунд «нужен вход» разрешает ОДНУ пробную попытку. Сессию
#: мог починить не только login: другой процесс CLI обновляет ту же пару
#: токенов, а отказ мог быть и временным (эпоха сессий на бэкенде).
RECHECK_AFTER_SEC = 900.0


def default_auth_state_path() -> Path:
    """``~/.skillery/daemon.auth.json`` — рядом с ``daemon.state.json``."""
    from skillery_cli.daemon.daemon_runner import _default_data_dir

    return _default_data_dir() / "daemon.auth.json"


def read_auth_state(path: Path | None = None) -> dict[str, Any]:
    """Состояние сессии демона для ``status``/``doctor``. Никогда не бросает."""
    p = path or default_auth_state_path()
    try:
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def needs_login(state: dict[str, Any] | None = None) -> bool:
    """Удобный предикат для диагностики (``doctor``/``status``)."""
    st = read_auth_state() if state is None else state
    return str(st.get("state") or "") == AUTH_NEEDS_LOGIN


@dataclass
class _Stamp:
    """Отпечаток файлов кред — сравнивается на каждом такте вместо чтения."""

    config: tuple[int, int, str] | None = None
    tokens: tuple[int, int, str] | None = None


def _stat_stamp(path: Path) -> tuple[int, int, str] | None:
    """Отпечаток = (mtime_ns, size, хэш содержимого).

    Пары ``(mtime_ns, size)`` НЕДОСТАТОЧНО, и это не теоретическая придирка:
    перелогин на адрес той же длины (``old@test`` → ``new@test``) не меняет
    размер, а mtime может совпасть, если обе записи попали в один тик часов
    файловой системы. Разрешение mtime зависит от ФС и на некоторых машинах
    заметно грубее, чем наносекунды в названии поля. Тогда отпечаток совпадает,
    демон считает креды прежними и продолжает работать под СТАРЫМ токеном —
    ровно тот случай, который этот класс обязан ловить (поймано гейтом CI:
    локально mtime различался, на раннере — нет).

    Хэш снимает зависимость от разрешения часов вообще. Файлы кред — десятки
    байт, поэтому чтение ничтожно на фоне того дорогого (``ClientConfig.load``
    + keyring), ради чего кэш и существует.
    """
    try:
        st = path.stat()
        digest = hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, digest)


class DaemonCredentials:
    """Кэш кред демона, который сам замечает смену логина.

    Дорогое (``ClientConfig.load`` + чтение keyring) делается только когда
    отпечаток файлов кред изменился либо кэш сброшен явно.
    """

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        recheck_after: float = RECHECK_AFTER_SEC,
        monotonic: Any = time.monotonic,
    ) -> None:
        self._state_path = state_path
        self._recheck_after = recheck_after
        self._monotonic = monotonic
        self._cfg: Any = None
        self._access: str | None = None
        self._stamp = _Stamp()
        self._auth_state: str = AUTH_OK
        self._auth_reason: str | None = None
        self._blocked_since: float | None = None
        self._blocked_stamp: _Stamp | None = None

    # ── пути ──
    def _config_path(self) -> Path:
        from skillery_cli.config import _default_config_file

        return _default_config_file()

    def _tokens_path(self) -> Path:
        from skillery_cli.config import _tokens_file_path

        return _tokens_file_path()

    def state_path(self) -> Path:
        return self._state_path or default_auth_state_path()

    # ── подхват изменений ──
    def _current_stamp(self) -> _Stamp:
        return _Stamp(
            config=_stat_stamp(self._config_path()),
            tokens=_stat_stamp(self._tokens_path()),
        )

    def refresh_if_changed(self) -> bool:
        """Перечитать креды, если файлы изменились. Возвращает факт перечитывания.

        Дешёвая часть — два ``os.stat``. Именно она зовётся на каждом такте.
        """
        stamp = self._current_stamp()
        if stamp == self._stamp and self._cfg is not None:
            return False
        self._stamp = stamp
        self._reload()
        return True

    def _reload(self) -> None:
        from skillery_cli.config import ClientConfig, load_tokens

        self._cfg = ClientConfig.load()
        self._access = None
        email = getattr(self._cfg, "user_email", None)
        if email:
            with suppress(Exception):
                access, _ = load_tokens(email)
                self._access = access

    def reload_now(self) -> None:
        """Перечитать креды с диска СЕЙЧАС, не трогая вердикт о сессии.

        Отдельно от :meth:`refresh_if_changed`: там смена отпечатка означает
        «пользователь что-то сделал» и снимает блокировку, а здесь мы просто
        сверяемся с диском по ходу обработки 401 — вердикт выносится ниже.
        """
        self._stamp = self._current_stamp()
        self._reload()

    # ── доступ ──
    def config(self) -> Any:
        self.refresh_if_changed()
        return self._cfg

    def access(self) -> str | None:
        self.refresh_if_changed()
        return self._access

    # ── состояние сессии ──
    def _set_auth(self, state: str, reason: str | None) -> None:
        self._auth_state = state
        self._auth_reason = reason
        if state == AUTH_NEEDS_LOGIN:
            self._blocked_since = self._monotonic()
            # Запоминаем ИМЕННО те креды, на которых сдались: любое их
            # изменение (= пользователь вошёл заново) снимет блокировку, и
            # снимет точно, а не «потому что кэш был холодный».
            self._blocked_stamp = self._current_stamp()
        else:
            self._blocked_since = None
            self._blocked_stamp = None
        self._write_auth_state()

    def _write_auth_state(self) -> None:
        payload = {
            "state": self._auth_state,
            "reason": self._auth_reason,
            "user_email": getattr(self._cfg, "user_email", None),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        path = self.state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # диагностика не имеет права валить демон

    def mark_needs_login(self, reason: str | None) -> None:
        """Сессию не восстановить — перейти в видимое состояние «нужен вход»."""
        if self._auth_state == AUTH_NEEDS_LOGIN:
            return  # уже там: не переписываем файл и не повторяем WARNING
        self._set_auth(AUTH_NEEDS_LOGIN, reason)
        _session_log().warning(
            "сессия истекла — нужен вход; демон приостановил отправку",
            extra={"context": {
                "reason": reason,
                "action": "skillery auth login",
                "recheck_in": self._recheck_after,
            }},
        )

    def mark_ok(self) -> None:
        if self._auth_state == AUTH_OK:
            return
        self._set_auth(AUTH_OK, None)
        _session_log().warning("сессия восстановлена — отправка возобновлена")

    def auth_state(self) -> str:
        return self._auth_state

    def publish(self) -> None:
        """Записать текущее состояние безусловно (старт демона: «сессия жива»)."""
        self.refresh_if_changed()
        self._write_auth_state()

    def blocked(self) -> bool:
        """Стоит ли молчать в этот такт (сессия мертва и переспросить рано).

        Возврат ``False`` при истёкшем окне — это разрешение на ОДНУ пробную
        попытку: окно взводится заново, только если та тоже упрётся в 401.
        """
        if self._auth_state != AUTH_NEEDS_LOGIN:
            self.refresh_if_changed()
            return False
        if self._current_stamp() != self._blocked_stamp:
            # Креды сменились с момента отказа — вероятнее всего это `login`.
            # Прошлый вердикт больше не про них: перечитываем и пробуем снова.
            self.reload_now()
            self._set_auth(AUTH_OK, None)
            _session_log().warning(
                "креды обновились — демон возобновляет отправку",
                extra={"context": {"user": getattr(self._cfg, "user_email", None)}},
            )
            return False
        since = self._blocked_since
        if since is None:
            return True
        if self._monotonic() - since >= self._recheck_after:
            self._blocked_since = self._monotonic()  # взводим окно под пробу
            return False
        return True

    # ── обновление токена ──
    def refresh_callback(self) -> Any:
        """``on_token_refresh`` для ``HubClient`` с учётом состояния сессии.

        Единственная точка, где 401 наблюдаем как факт: транспорт зовёт этот
        callback ровно при 401 на авторизованном запросе. Успех — обновляем
        кэш и снимаем «нужен вход»; провал — фиксируем причину и уходим в
        видимое состояние вместо молчаливого повтора.
        """

        async def _refresh() -> tuple[str, str] | None:
            from skillery_cli.__main__ import (
                _REFRESH_FAILURE,
                _make_refresh_callback,
            )

            cfg = self.config()
            result = await _make_refresh_callback(cfg)()
            if result is None:
                reason = _REFRESH_FAILURE.get("reason") or "обновление сессии не удалось"
                # Токены мог обновить другой процесс уже после того, как мы
                # прочитали свои — перечитываем, прежде чем объявлять отказ.
                self.reload_now()
                self.mark_needs_login(str(reason))
                return None
            self._access = result[0]
            self.mark_ok()
            return result

        return _refresh


__all__ = [
    "AUTH_NEEDS_LOGIN",
    "AUTH_OK",
    "RECHECK_AFTER_SEC",
    "DaemonCredentials",
    "default_auth_state_path",
    "needs_login",
    "read_auth_state",
]
