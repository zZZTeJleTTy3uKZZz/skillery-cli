"""Гейт публикации: артефакты не должны нести личные данные разработчика.

Зачем. Это уже случилось по-настоящему: в публичный PyPI уехали версии кита с
реальным email и домашним путём разработчика — они попали туда из ТЕСТОВЫХ
фикстур, которые никто не считал «настоящими данными». Удалить релиз с PyPI
нельзя (только yank, а окно удаления — 72 часа по PEP 763), поэтому цена
ошибки несимметрична: проверка стоит секунду, утечка необратима.

Ручная проверка перед каждым релизом не работает — её забывают ровно тогда,
когда релиз срочный. Поэтому она здесь, в CI, ДО шага публикации.

Что ловим (по классам, а не по конкретным строкам — иначе гейт защищает от
одного известного случая и слеп ко всем остальным):

1. **Windows-путь с числовым именем пользователя** — ``C:/Users/<цифры>/…``.
   Числовое имя — почти всегда реальный аккаунт (у выдуманных пишут ``user``).
2. **Ящик на публичном почтовом провайдере** (gmail, yandex, mail.ru, …) —
   именно так выглядела реальная утечка. Синтетику вроде ``x@y.io`` и ssh-URL
   ``git@github.com`` намеренно НЕ трогаем: гейт, который шумит на каждом
   тестовом адресе, перестают читать, и он теряет смысл.
3. **Юникс-домашний путь** ``/home/<name>`` и ``/Users/<name>`` c именем,
   не похожим на плейсхолдер.

Запуск::

    python scripts/check_dist_leaks.py dist/

Код возврата 1 = найдено, публикацию продолжать нельзя.
"""
from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path

#: Публичные почтовые провайдеры. Ловим ТОЛЬКО их, а не «любой email»: в тестах
#: полно синтетических адресов (``x@y.io``, ``o@e.com``) и ssh-URL
#: (``git@github.com``) — гейт, орущий на них, быстро научатся игнорировать, и
#: он перестанет ловить настоящее. Реальная утечка выглядела ровно так:
#: живой ящик на gmail в тестовой фикстуре.
_MAIL_PROVIDERS = (
    "gmail.com", "googlemail.com", "yandex.ru", "yandex.com", "ya.ru",
    "mail.ru", "bk.ru", "inbox.ru", "list.ru", "internet.ru",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "me.com", "yahoo.com", "proton.me", "protonmail.com",
    "rambler.ru", "aol.com", "gmx.com", "zoho.com",
)
#: Имена-плейсхолдеры в путях: их наличие означает «фикстура», а не живой аккаунт.
_SAFE_USER_NAMES = ("user", "testuser", "runner", "someone", "alice", "bob", "ci")

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "windows-путь с числовым именем пользователя",
        re.compile(r"[Cc]:[\\/]+Users[\\/]+\d{3,}", re.IGNORECASE),
    ),
    (
        "личный ящик на публичном почтовом провайдере",
        re.compile(
            r"\b[\w.+-]+@(?:" + "|".join(d.replace(".", r"\.") for d in _MAIL_PROVIDERS) + r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "домашний путь юникс",
        # ``(?<![:\w])`` отсекает хвост windows-пути: в ``C:/Users/10001`` перед  # leak-gate-allow
        # ``/Users`` стоит двоеточие, и это дело windows-правила — иначе одна
        # строка давала бы две жалобы подряд.
        re.compile(r"(?<![:\w])/(?:home|Users)/([A-Za-z0-9._-]{3,})"),
    ),
)

#: Маркер осознанного исключения. Ставится в ТОЙ ЖЕ строке и глушит гейт только
#: на ней. Нужен там, где «подозрительное» значение — суть теста: например,
#: фикстура денилист-гейта обязана содержать путь вида ``C:/Users/<цифры>``,
#: иначе тест не проверяет ровно то, ради чего написан. Пофайловый skip тут не
#: годится — он бы ослепил гейт на весь файл, включая будущие правки.
_ALLOW_MARKER = "leak-gate-allow"

#: Файлы, где совпадение — заведомо не утечка (списки исключений, метаданные
#: автора пакета клиент и так видит на странице PyPI).
_SKIP_SUFFIXES = (".gitignore", ".dockerignore", "METADATA", "PKG-INFO")


def _is_benign(rule: str, hit: str) -> bool:
    low = hit.lower()
    if rule.startswith("личный ящик"):
        # ssh-URL вида git@github.com сюда не попадает (github.com не провайдер),
        # но no-reply-адреса роботов формально живут на публичных доменах.
        return low.startswith(("noreply@", "no-reply@", "git@"))
    if rule.startswith("домашний"):
        return any(n in low for n in _SAFE_USER_NAMES)
    return False


def _iter_members(path: Path):
    """(имя, текст) по каждому текстовому файлу архива."""
    if path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                yield member.name, fh.read()
    elif path.suffix == ".whl":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                yield name, zf.read(name)


def scan(dist_dir: Path) -> list[str]:
    problems: list[str] = []
    archives = [p for p in dist_dir.iterdir() if p.suffix == ".whl" or p.name.endswith(".tar.gz")]
    if not archives:
        raise SystemExit(f"в {dist_dir} нет артефактов — нечего проверять")
    for archive in archives:
        for name, raw in _iter_members(archive):
            if name.endswith(_SKIP_SUFFIXES):
                continue
            text = raw.decode("utf-8", "ignore")
            # Построчно: allow-маркер действует ровно на свою строку, и в отчёт
            # попадает номер — иначе искать совпадение в файле на 5000 строк.
            for lineno, line in enumerate(text.splitlines(), 1):
                if _ALLOW_MARKER in line:
                    continue
                for rule, pattern in _RULES:
                    for match in pattern.finditer(line):
                        shown = match.group(0)
                        # Для правила с группой (домашний путь) судим по имени
                        # пользователя, для остальных — по совпадению целиком.
                        subject = match.group(1) if match.groups() else shown
                        if _is_benign(rule, subject):
                            continue
                        problems.append(
                            f"{archive.name} :: {name}:{lineno} :: {rule} :: {shown}"
                        )
    return sorted(set(problems))


def main() -> int:
    dist_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    problems = scan(dist_dir)
    if problems:
        print("НАЙДЕНЫ личные данные в артефактах — публикация остановлена:\n")
        for line in problems:
            print("  •", line)
        print(
            "\nЗамените реальные значения на плейсхолдеры "
            "(user@example.com, C:/Users/10001, /home/user)."  # leak-gate-allow
        )
        return 1
    print(f"артефакты в {dist_dir} чисты: личных данных не найдено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
