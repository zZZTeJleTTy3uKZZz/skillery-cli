"""#2264: межпроцессный лок обновления refresh-токена.

Зачем
-----
Процессов, которые могут обновлять сессию, НЕСКОЛЬКО и они независимы: демон
(long-poll очереди заданий + телеметрия), watchdog/апгрейдер и любая команда
пользователя. Все они читают ОДИН refresh-токен из общего хранилища
(keyring/файл) и все могут упереться в 401 одновременно.

Что было на проде (устройство ``DESKTOP-C1CK65V``, 13.08): три ротации одного
токена за три секунды, следом ``revoked_reason='reuse_detected'`` — и дальше
401 на ВСЁ (``/auth/refresh``, ``/telemetry/events``, ``/devices/*/tasks``),
телеметрия молчала 11 суток. Сервер отработал штатно: повторное предъявление
уже провёрнутого токена он обязан считать признаком кражи.

Почему asyncio.Lock недостаточно
--------------------------------
Внутрипроцессный лок сериализует корутины ОДНОГО процесса. Гонка же
межпроцессная: демон и команда — разные процессы, у каждого свой event loop.
Нужен объект, который видят все процессы, — то есть объект ЯДРА.

Механизм: байтовая блокировка файла (и на Windows, и на POSIX)
---------------------------------------------------------------
Один общий файл ``~/.skillery/token-refresh.lock``, эксклюзивная блокировка
первого байта: ``msvcrt.locking`` (Windows, поверх ``LockFile``) и ``flock``
(POSIX). Свойства, ради которых выбран именно он:

- **виден всем процессам** пользователя — блокировку держит ядро, а не наш код;
- **переживает падение владельца**: блокировка привязана к ОТКРЫТОМУ ДЕСКРИПТОРУ,
  а дескрипторы закрывает ядро при завершении процесса — в том числе при
  жёстком kill'е и BSOD-перезагрузке. Поэтому здесь нет самодельного
  lock-файла с PID и TTL: тот врёт после kill'а и переиспользования PID
  системой, и мёртвый владелец вешал бы обновление сессии навсегда;
- **не привязан к потоку**. Это решающее отличие от именованного мьютекса
  (``CreateMutexW``), которым в CLI держатся единственность демона и
  апгрейдера. Мьютекс на Windows потоко-аффинен: ``ReleaseMutex`` обязан
  вызвать ТОТ ЖЕ поток, который дождался владения. Обновление токена живёт в
  async-callback'е: ждать блокировку приходится в рабочем потоке
  (``asyncio.to_thread``, иначе встанет event loop демона), а отпускать —
  позже и, вообще говоря, в другом потоке пула. Мьютекс на этом залип бы
  навсегда (поток пула не умирает), байтовая блокировка — нет.

Ожидание с таймаутом делается опросом (неблокирующая попытка + короткий сон):
блокирующий вариант без таймаута повесил бы CLI, если сосед завис на сети.

Отказ инфраструктуры (нет ``msvcrt``/``fcntl``, не создаётся файл) НЕ блокирует
обновление: лучше вернуться к прежнему поведению (гонка возможна), чем не дать
пользователю обновить сессию вовсе.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

__all__ = ["DEFAULT_TIMEOUT", "lock_path", "token_refresh_lock"]

#: Сколько ждать соседа. Обновление токена — один HTTP-запрос; 35 с покрывают
#: таймаут транспорта (30 с) с запасом на запись в Credential Manager. Дольше
#: ждать бессмысленно: сосед либо уже обновил (мы перечитаем и увидим новый
#: токен), либо завис — и тогда лучше попробовать самим, чем стоять вечно.
DEFAULT_TIMEOUT = 35.0

#: Пауза между попытками взять блокировку. 50 мс: обновление занимает сотни
#: миллисекунд, так что опрос не «жжёт» процессор и не добавляет заметной
#: задержки поверх работы соседа.
_POLL_INTERVAL = 0.05

#: env-оверрайд имени файла лока — нужен ТЕСТАМ и параллельным профилям, чтобы
#: прогон не тормозил НАСТОЯЩИЙ демон на машине разработчика.
_LOCK_ENV = "SKILLERY_TOKEN_REFRESH_LOCK"


def lock_path() -> Path:
    """Путь файла-лока (env-оверрайд ``SKILLERY_TOKEN_REFRESH_LOCK``)."""
    override = os.environ.get(_LOCK_ENV)
    if override:
        return Path(override)
    from skillery_cli import _branding

    home = Path.home() / _branding.HOME_DIR_NAME
    home.mkdir(parents=True, exist_ok=True)
    return home / "token-refresh.lock"


def _try_lock(fh) -> bool:  # файловый объект
    """Одна НЕблокирующая попытка захвата. ``False`` — занято соседом."""
    if sys.platform == "win32":
        import msvcrt

        try:
            # LK_NBLCK — неблокирующая эксклюзивная блокировка 1 байта.
            # Блокируем именно байт с позиции 0: важно, чтобы ВСЕ процессы
            # брали один и тот же диапазон.
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh) -> None:  # файловый объект
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def token_refresh_lock(timeout: float = DEFAULT_TIMEOUT) -> Iterator[bool]:
    """Занять межпроцессный лок обновления токена.

    Отдаёт ``True``, если лок ВЗЯТ (или инфраструктура лока недоступна и мы
    сознательно не блокируем работу), ``False`` — если ждали дольше
    ``timeout`` и не дождались. Освобождение — на выходе из блока; при падении
    процесса дескриптор закрывает ядро, и лок снимается сам.
    """
    try:
        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+b")
    except Exception:  # файл лока не создался: не блокируем вход
        yield True
        return

    locked = False  # блокировка РЕАЛЬНО взята (её и снимаем на выходе)
    passthrough = False  # платформа без msvcrt/fcntl — пускаем без лока
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                locked = _try_lock(fh)
            except Exception:  # нет msvcrt/fcntl на платформе
                passthrough = True
                break
            if locked or time.monotonic() >= deadline:
                break
            time.sleep(_POLL_INTERVAL)
        yield locked or passthrough
    finally:
        if locked:
            with contextlib.suppress(Exception):
                _unlock(fh)
        with contextlib.suppress(Exception):
            fh.close()
