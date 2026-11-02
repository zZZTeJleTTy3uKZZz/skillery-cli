"""Состояние установленного: ``~/.skillery/products.json``.

Файл — единственный ответ на вопросы ``product list`` и ``product remove``,
поэтому запись детерминирована: ключи сортируются, отступ фиксирован, а
повторная запись того же состояния НЕ трогает файл (иначе повторная установка
меняла бы mtime и «идемпотентность» была бы только на словах).

Файл ОБЩИЙ с Go-CLI (``skillery-go/internal/install/state.go``), поэтому
запись повторяет его контракт (#6011):

- **один и тот же лок** — ``products.json.lock`` рядом с файлом. Go берёт
  его через ``gofrs/flock``: на POSIX это ``flock``, на Windows —
  ``LockFileEx`` на один байт с нулевого смещения. Python берёт ровно тот же
  диапазон: ``fcntl.flock`` на POSIX и ``msvcrt.locking`` на Windows
  (``_locking`` поверх ``LockFile``). ``LockFile`` и ``LockFileEx`` — две
  обёртки над одним механизмом байтовых диапазонов ``NtLockFile``, поэтому
  блокировки видят друг друга независимо от того, кто из CLI её взял;
- **слияние с диском перед записью**: файл мог быть дописан соседним
  процессом (Python или Go) между нашим ``load`` и ``save`` — без слияния
  последний писатель потерял бы чужой продукт;
- **атомарная замена**: уникальный временный файл рядом с целью, ``fsync``
  файла (и каталога на POSIX), затем ``rename``. Права нового файла — 0600
  (файл называет устройства и пути человека), у существующего файла права
  сохраняются — ровно как в Go.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .paths import home_paths

STATE_VERSION = 1

#: Суффикс файла блокировки. Имя фиксировано и совпадает с Go
#: (``StateLockPath``): только один писатель в единицу времени, иначе
#: чтение-изменение-запись одного процесса затрёт продукт другого.
LOCK_SUFFIX = ".lock"

#: Сколько ждать соседа. Блокировка держится на время чтения-записи файла, а не
#: на время установки: 30 с — это заведомо дольше любой штатной записи и
#: достаточно мало, чтобы не подвесить команду навсегда при зависшем соседе.
DEFAULT_TIMEOUT = 30.0

#: Пауза между попытками взять блокировку. 20 мс: запись состояния — это
#: миллисекунды, так что опрос не жжёт процессор и не добавляет заметной
#: задержки поверх работы соседа.
_POLL_INTERVAL = 0.02

#: errno «занято»: POSIX отдаёт EAGAIN/EWOULDBLOCK (иногда EACCES), CRT на
#: Windows — EACCES при занятом диапазоне.
_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})

#: Windows ERROR_LOCK_VIOLATION: Python на этой ошибке ставит errno EACCES, но
#: проверяем и winerror — так «занято» не перепутается с настоящей ошибкой.
_WIN_LOCK_VIOLATION = 33


class StateError(RuntimeError):
    """Файл состояния не читается или имеет неожидаемый вид."""


def state_path() -> Path:
    """Путь состояния от текущего HOME/конфига (вычисляется при вызове)."""
    return home_paths().products_file


def state_lock_path(path: Path) -> Path:
    """Файл блокировки рядом с состоянием — тот же, что у Go-CLI."""
    return path.with_name(path.name + LOCK_SUFFIX)


def _is_busy(exc: OSError) -> bool:
    if exc.errno in _BUSY_ERRNOS:
        return True
    return getattr(exc, "winerror", None) == _WIN_LOCK_VIOLATION


def _try_lock(handle, *, exclusive: bool) -> bool:  # файловый объект
    """Одна НЕблокирующая попытка захвата. ``False`` — занято соседом.

    На Windows ``msvcrt.locking`` умеет только исключительную блокировку
    (общий режим в CRT не выражен), поэтому там чтение тоже берёт
    исключительную: это строже Go, но не мешает ему — диапазон тот же.
    """
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if _is_busy(exc):
                return False
            raise
        return True

    import fcntl

    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(handle, operation | fcntl.LOCK_NB)
    except OSError as exc:
        if _is_busy(exc):
            return False
        raise
    return True


def _unlock(handle) -> None:  # файловый объект
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def state_file_lock(
    path: Path,
    *,
    exclusive: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> Iterator[None]:
    """Занять межпроцессный лок состояния; ждать соседа ограниченное время.

    Лок привязан к ОТКРЫТОМУ дескриптору: при падении процесса ядро закрывает
    его и снимает блокировку само (переживает и жёсткий kill). Поэтому здесь
    нет самодельного lock-файла с PID и TTL — тот врёт после kill'а и
    переиспользования PID системой.

    Отказ инфраструктуры (нет ``msvcrt``/``fcntl``, файл лока не создаётся) —
    это ``StateError``, а не «продолжаем без лока»: запись без блокировки
    теряет продукты соседнего процесса, и молчать об этом нельзя.
    """
    lock_file = state_lock_path(path)
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_file.open("a+b")
    except OSError as exc:
        raise StateError(
            f"блокировка состояния не берётся ({lock_file}): {exc}"
        ) from exc

    locked = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                locked = _try_lock(handle, exclusive=exclusive)
            except (ImportError, OSError) as exc:
                raise StateError(
                    f"блокировка состояния не берётся ({lock_file}): {exc}"
                ) from exc
            if locked:
                break
            if time.monotonic() >= deadline:
                raise StateError(
                    f"блокировка состояния не берётся ({lock_file}): "
                    f"сосед держит её дольше {timeout:g} с"
                )
            time.sleep(_POLL_INTERVAL)
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                _unlock(handle)
        with contextlib.suppress(OSError):
            handle.close()


def _read_products(file: Path) -> dict[str, Any]:
    """Разобрать продукты из файла; отсутствующий — пустая карта."""
    try:
        raw = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StateError(f"состояние продуктов не читается ({file}): {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(f"состояние продуктов не разобрано как JSON ({file}): {exc}") from exc

    products = document.get("products") if isinstance(document, dict) else None
    if not isinstance(products, dict):
        raise StateError(f"состояние продуктов повреждено ({file}): нет products")
    return products


def _write_atomic(file: Path, text: str) -> None:
    """Записать текст атомарно: уникальный tmp + fsync + rename."""
    data = text.encode("utf-8")
    mode = _target_mode(file)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{file.name}.tmp-", dir=str(file.parent))
    tmp = Path(tmp_name)
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _sync_directory(file.parent)


def _target_mode(file: Path) -> int:
    """Права замены: у существующего файла сохраняются, у нового — 0600.

    Повторяет Go: подмена rename'ом не должна ни сужать, ни расширять права
    файла, который человек или прежняя версия CLI уже настроил. Запрошенные
    0600 — значение по умолчанию только для нового файла.
    """
    try:
        info = file.lstat()
    except FileNotFoundError:
        return 0o600
    if stat.S_ISDIR(info.st_mode):
        raise StateError(f"состояние продуктов не записывается ({file}): это каталог")
    if stat.S_ISREG(info.st_mode):
        return stat.S_IMODE(info.st_mode)
    return 0o600


def _sync_directory(directory: Path) -> None:
    """Сбросить каталог на диск: без этого rename может пережить падение, а
    содержимое — нет. На Windows каталог штатно не открывается — шаг пропускаем."""
    if sys.platform == "win32":
        return
    with contextlib.suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class InstalledState:
    """Реестр установленных продуктов и их компонентов."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self.products: dict[str, dict[str, Any]] = {}
        #: Продукты, снятые в ЭТОМ процессе. Без этой памяти слияние с диском
        #: при ``save`` вернуло бы их обратно из старого файла (как в Go).
        self._dropped: set[str] = set()

    @property
    def path(self) -> Path:
        return self._path or state_path()

    @classmethod
    def load(cls, path: Path | None = None, *, timeout: float = DEFAULT_TIMEOUT) -> InstalledState:
        state = cls(path)
        file = state.path
        # Каталога нет — нет и файла: лок-файл не создаём, чтобы чтение не
        # оставляло следов на чистой машине (Go проверяет так же).
        if not file.parent.is_dir():
            return state
        with state_file_lock(file, exclusive=False, timeout=timeout):
            state.products = _read_products(file)
        return state

    def save(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Записать состояние, если оно изменилось (под локом, со слиянием диска)."""
        file = self.path
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError(
                f"каталог состояния не создаётся ({file.parent}): {exc}"
            ) from exc

        with state_file_lock(file, exclusive=True, timeout=timeout):
            if not file.parent.is_dir():  # каталог унесли, пока ждали лок
                raise StateError(f"каталог состояния исчез ({file.parent})")
            disk = _read_products(file)
            for slug in self._dropped:
                disk.pop(slug, None)
            # Сосед (Python или Go) мог дописать свой продукт после нашего
            # чтения: его запись не наша — переносим на диск целиком.
            for slug, item in disk.items():
                self.products.setdefault(slug, item)

            payload = {"version": STATE_VERSION, "products": self.products}
            text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            try:
                existing = file.read_text(encoding="utf-8")
            except FileNotFoundError:
                existing = None
            except OSError as exc:
                raise StateError(
                    f"состояние продуктов не читается ({file}): {exc}"
                ) from exc
            if existing == text:
                return
            try:
                _write_atomic(file, text)
            except OSError as exc:
                raise StateError(
                    f"состояние продуктов не записывается ({file}): {exc}"
                ) from exc

    def get_product(self, slug: str) -> dict[str, Any] | None:
        return self.products.get(slug)

    def record_component(
        self,
        product_slug: str,
        product_version: str,
        *,
        key: str,
        component_type: str,
        status: str,
        detail: str = "",
        path: str = "",
        hosts: Sequence[str] = (),
        artifact: Mapping[str, Any] | None = None,
    ) -> None:
        self._dropped.discard(product_slug)
        product = self.products.setdefault(
            product_slug, {"version": product_version, "components": {}}
        )
        product["version"] = product_version
        components = product.setdefault("components", {})
        entry: dict[str, Any] = {
            "type": component_type,
            "status": status,
            "detail": detail,
            "path": path,
            "artifact": dict(artifact) if artifact else {},
        }
        # Харнес установленного плагина — часть состояния: без него `remove`
        # не знает, из каких харнесов вычищать, а `list` — куда он лёг.
        if hosts:
            entry["hosts"] = sorted({str(host) for host in hosts})
        components[key] = entry

    def drop_component(self, product_slug: str, key: str) -> None:
        product = self.products.get(product_slug)
        if product is None:
            return
        components = product.get("components", {})
        components.pop(key, None)

    def set_version(self, product_slug: str, version: str) -> None:
        """Записать фактическую версию продукта после обновления (#5988).

        ``record_component`` делает это за установленный компонент, но при
        обновлении часть компонентов остаётся нетронутой — без этой записи
        products.json продолжал бы называть старую версию, и следующий sync
        снова считал бы продукт устаревшим.
        """
        self._dropped.discard(product_slug)
        product = self.products.get(product_slug)
        if product is None:
            product = self.products.setdefault(
                product_slug, {"version": version, "components": {}}
            )
        product["version"] = version

    def drop_product(self, slug: str) -> None:
        self.products.pop(slug, None)
        self._dropped.add(slug)
