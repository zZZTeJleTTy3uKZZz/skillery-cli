"""#6011: ``products.json`` — общий файл Python-CLI и Go-CLI.

Совместимость держится на трёх вещах, и каждая проверяется отдельно:

- блокировка ОДНОГО и того же файла ``products.json.lock`` (в Go её берёт
  ``gofrs/flock``): имя лока фиксировано, иначе два писателя не увидят друг
  друга;
- слияние с диском ПОД блокировкой: продукт, записанный соседним процессом
  (Python или Go) между нашим ``load`` и ``save``, не пропадает;
- атомарная запись: уникальный временный файл рядом с целью, ``fsync``,
  ``rename`` и права 0600 у нового файла.

Гонка проверяется НАСТОЯЩИМИ процессами: внутрипроцессный замок ничего не
доказывает — терялись записи именно между процессами.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from skillery_cli.products.state import (
    InstalledState,
    StateError,
    state_lock_path,
)

SRC_DIR = Path(__file__).resolve().parents[2] / "src"

#: Сколько ждать соседний процесс: щедро, но конечно — тест не должен висеть.
CHILD_TIMEOUT = 40.0


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / ".skillery" / "products.json"


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    return env


def _run_child(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT,
        check=False,
    )


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"не дождались {path}")
        time.sleep(0.01)


#: Писатель: читает состояние ДО первой чужой записи (барьер), затем пишет своё.
#: Без слияния под блокировкой второй писатель затёр бы продукт первого.
WRITER_SCRIPT = """
import sys, time
from pathlib import Path
from skillery_cli.products.state import InstalledState

path = Path(sys.argv[1])
slug = sys.argv[2]
barrier = Path(sys.argv[3])
expected = int(sys.argv[4])

state = InstalledState.load(path)
(barrier / (slug + ".loaded")).write_text("loaded", encoding="utf-8")
deadline = time.monotonic() + 20
while len(list(barrier.glob("*.loaded"))) < expected:
    if time.monotonic() > deadline:
        raise SystemExit("barrier timeout")
    time.sleep(0.01)

state.record_component(
    slug,
    "1.0.0",
    key="plugin-" + slug,
    component_type="plugin",
    status="installed",
)
state.save()
"""

#: Держатель лока: процесс стоит с блокировкой, пока родитель не отпустит его
#: файлом-сигналом. Доказывает, что лок виден ДРУГОМУ процессу.
HOLDER_SCRIPT = """
import sys, time
from pathlib import Path
from skillery_cli.products.state import state_file_lock

path = Path(sys.argv[1])
ready = Path(sys.argv[2])
done = Path(sys.argv[3])

with state_file_lock(path, exclusive=True, timeout=10):
    ready.write_text("locked", encoding="utf-8")
    deadline = time.monotonic() + 20
    while not done.exists():
        if time.monotonic() > deadline:
            raise SystemExit("release timeout")
        time.sleep(0.01)
"""

#: Держатель лока, который отпускает его сам: доказывает, что ``save`` ЖДЁТ
#: соседа, а не сдаётся с ошибкой на первой неудачной попытке.
HOLDER_TIMED_SCRIPT = """
import sys, time
from pathlib import Path
from skillery_cli.products.state import state_file_lock

path = Path(sys.argv[1])
ready = Path(sys.argv[2])

with state_file_lock(path, exclusive=True, timeout=10):
    ready.write_text("locked", encoding="utf-8")
    time.sleep(0.3)
"""


# ── имя лока: контракт с Go ─────────────────────────────────────────────────


def test_lock_file_is_products_json_lock(state_file: Path) -> None:
    """Имя лока — ровно то, что берёт Go: ``<state>.lock`` рядом с файлом."""
    assert state_lock_path(state_file) == state_file.with_name("products.json.lock")
    assert state_lock_path(state_file).parent == state_file.parent


def test_load_missing_state_creates_no_lock_file(state_file: Path) -> None:
    """Устройство без продуктов: чтение не оставляет лок-файл в ``~/.skillery``."""
    state = InstalledState.load(state_file)
    assert state.products == {}
    assert not state_file.parent.exists()


# ── атомарная запись ────────────────────────────────────────────────────────


def test_save_creates_state_file_0600(state_file: Path) -> None:
    """Файл называет устройства и пути человека — права нового файла 0600."""
    state = InstalledState.load(state_file)
    state.record_component(
        "demo", "1.0.0", key="plugin-demo", component_type="plugin", status="installed"
    )
    state.save()

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["products"]["demo"]["components"]["plugin-demo"]["status"] == "installed"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600


def test_save_writes_unique_tmp_then_renames(
    state_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tmp уникален (не ``products.json.tmp``) и до ``replace`` уже сброшен на диск."""
    state = InstalledState.load(state_file)
    state.record_component(
        "demo", "1.0.0", key="plugin-demo", component_type="plugin", status="installed"
    )

    renamed: list[tuple[Path, Path, bool]] = []
    fsynced_fds: list[int] = []
    real_replace = os.replace
    real_fsync = os.fsync

    def _replace(src: object, dst: object) -> None:
        source = Path(src)
        renamed.append((source, Path(dst), source.exists()))
        real_replace(src, dst)

    def _fsync(fd: int) -> None:
        fsynced_fds.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "replace", _replace)
    monkeypatch.setattr(os, "fsync", _fsync)
    state.save()

    assert len(renamed) == 1
    source, target, existed = renamed[0]
    assert (source.parent, target) == (state_file.parent, state_file)
    assert existed, "tmp удалён до rename — замена не атомарна"
    assert source.name != "products.json.tmp", "имя tmp фиксировано — соседи затрут друг друга"
    assert source.name.startswith(".products.json.tmp-")
    assert fsynced_fds, "tmp не сброшен на диск перед rename"
    leftovers = [item.name for item in state_file.parent.iterdir() if ".tmp-" in item.name]
    assert leftovers == [], f"временные файлы остались: {leftovers}"


def test_save_keeps_mode_of_existing_file(
    state_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Права существующего файла не сужаются и не расширяются (как в Go)."""
    state_file.parent.mkdir(parents=True)
    state_file.write_text('{"version": 1, "products": {}}\n', encoding="utf-8")
    os.chmod(state_file, 0o640)

    state = InstalledState.load(state_file)
    state.record_component(
        "demo", "1.0.0", key="plugin-demo", component_type="plugin", status="installed"
    )
    state.save()

    assert stat.S_IMODE(state_file.stat().st_mode) == 0o640


def test_save_does_not_rewrite_identical_state(state_file: Path) -> None:
    """Совпадающее состояние не трогает файл: иначе mtime врёт про установку."""
    state = InstalledState.load(state_file)
    state.record_component(
        "demo", "1.0.0", key="plugin-demo", component_type="plugin", status="installed"
    )
    state.save()
    before = state_file.stat()

    again = InstalledState.load(state_file)
    again.save()

    after = state_file.stat()
    assert after.st_ino == before.st_ino, "файл заменён, хотя содержимое не менялось"


def test_save_does_not_overwrite_corrupted_state(state_file: Path) -> None:
    """Повреждённый файл не затирается «правильным»: сосед увидит ошибку, а не пустоту."""
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{ это не json", encoding="utf-8")
    state = InstalledState(state_file)
    state.record_component(
        "demo", "1.0.0", key="plugin-demo", component_type="plugin", status="installed"
    )

    with pytest.raises(StateError, match="JSON"):
        state.save()
    assert state_file.read_text(encoding="utf-8") == "{ это не json"


def test_save_waits_for_neighbour_and_then_writes(state_file: Path, tmp_path: Path) -> None:
    """Занятый лок — это «подождать», а не «сдаться»: короткий сосед не валит запись."""
    state_file.parent.mkdir(parents=True)
    ready = tmp_path / "locked"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER_TIMED_SCRIPT, str(state_file), str(ready)],
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for(ready)
    state = InstalledState(state_file)
    state.record_component(
        "demo", "1.0.0", key="plugin-demo", component_type="plugin", status="installed"
    )
    started = time.monotonic()
    state.save(timeout=10)
    elapsed = time.monotonic() - started

    _, stderr = holder.communicate(timeout=CHILD_TIMEOUT)
    assert holder.returncode == 0, f"держатель лока упал: {stderr}"
    assert elapsed >= 0.15, "запись не ждала соседа, а прошла мимо лока"
    saved = json.loads(state_file.read_text(encoding="utf-8"))["products"]
    assert "demo" in saved


# ── слияние с диском ────────────────────────────────────────────────────────


def _write_disk_state(path: Path, products: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "products": products}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_save_merges_products_written_by_neighbour(state_file: Path) -> None:
    """Продукт, записанный соседом после нашего чтения, остаётся в файле."""
    _write_disk_state(
        state_file,
        {"atlas": {"version": "2.0.0", "components": {"plugin-atlas": {"status": "installed"}}}},
    )
    state = InstalledState.load(state_file)
    # Пока мы думали, Go-CLI дописал свой продукт.
    _write_disk_state(
        state_file,
        {
            "atlas": {"version": "2.0.0", "components": {"plugin-atlas": {"status": "installed"}}},
            "go-only": {"version": "3.0.0", "components": {}},
        },
    )
    state.record_component(
        "demo", "1.0.0", key="plugin-demo", component_type="plugin", status="installed"
    )
    state.save()

    saved = json.loads(state_file.read_text(encoding="utf-8"))["products"]
    assert set(saved) == {"atlas", "demo", "go-only"}
    assert saved["atlas"]["components"]["plugin-atlas"]["status"] == "installed"


def test_dropped_product_is_not_resurrected_by_merge(state_file: Path) -> None:
    """``product remove`` не воскресает: слияние не возвращает снятый продукт."""
    _write_disk_state(
        state_file,
        {
            "atlas": {"version": "2.0.0", "components": {}},
            "demo": {"version": "1.0.0", "components": {}},
        },
    )
    state = InstalledState.load(state_file)
    state.drop_product("atlas")
    state.save()

    saved = json.loads(state_file.read_text(encoding="utf-8"))["products"]
    assert set(saved) == {"demo"}


def test_recorded_product_is_taken_back_from_dropped(state_file: Path) -> None:
    """Снятый и снова поставленный в том же процессе продукт пишется на диск."""
    _write_disk_state(state_file, {"atlas": {"version": "2.0.0", "components": {}}})
    state = InstalledState.load(state_file)
    state.drop_product("atlas")
    state.record_component(
        "atlas", "3.0.0", key="plugin-atlas", component_type="plugin", status="installed"
    )
    state.save()

    saved = json.loads(state_file.read_text(encoding="utf-8"))["products"]
    assert saved["atlas"]["version"] == "3.0.0"
    assert "plugin-atlas" in saved["atlas"]["components"]


# ── межпроцессная гонка ─────────────────────────────────────────────────────


def test_two_processes_writing_at_once_lose_nothing(state_file: Path, tmp_path: Path) -> None:
    """Два процесса читают пустое состояние и пишут одновременно — оба продукта целы."""
    state_file.parent.mkdir(parents=True)
    barrier = tmp_path / "barrier"
    barrier.mkdir()

    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER_SCRIPT, str(state_file), slug, str(barrier), "2"],
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for slug in ("first", "second")
    ]
    outputs = [process.communicate(timeout=CHILD_TIMEOUT) for process in processes]
    for process, (stdout, stderr) in zip(processes, outputs, strict=True):
        assert process.returncode == 0, f"писатель упал: {stdout}\n{stderr}"

    saved = json.loads(state_file.read_text(encoding="utf-8"))["products"]
    assert set(saved) == {"first", "second"}, "одна из одновременных записей потерялась"
    for slug in ("first", "second"):
        assert f"plugin-{slug}" in saved[slug]["components"]


def test_lock_is_visible_to_other_process(state_file: Path, tmp_path: Path) -> None:
    """Лок файла виден ДРУГОМУ процессу: пока держат, запись ждёт и сдаётся."""
    state_file.parent.mkdir(parents=True)
    ready = tmp_path / "locked"
    done = tmp_path / "release"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER_SCRIPT, str(state_file), str(ready), str(done)],
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(ready)
        with pytest.raises(StateError, match="блокировка"):
            InstalledState.load(state_file, timeout=0.2)

        state = InstalledState(state_file)
        state.record_component(
            "demo", "1.0.0", key="plugin-demo", component_type="plugin", status="installed"
        )
        with pytest.raises(StateError, match="блокировка"):
            state.save(timeout=0.2)
    finally:
        done.write_text("release", encoding="utf-8")
        stdout, stderr = holder.communicate(timeout=CHILD_TIMEOUT)
    assert holder.returncode == 0, f"держатель лока упал: {stdout}\n{stderr}"
