"""Локальная сторона лиза способности (#1490) — состояние на диске и гейт запуска.

Контракт: ``skillery/docs/dev/capability-lease-contract.md`` (принят 2026-08-06,
решения владельца §12: TTL 24 ч, ``grace = 0``, гейт **точечный**).

ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО ЗДЕСЬ НЕТ. Модуль **в сеть не ходит вообще** — ни одного
импорта транспорта. Это не стилистика: ЦКП #1490 требует, чтобы «при живой сети
и действующем праве запуск не ходил в сеть и не замедлялся», и единственный
надёжный способ это гарантировать — не иметь такой возможности в коде, который
зовёт ``run``. Сетевую половину (обновление набора лизов, JWKS, монотонный пол)
ведёт демон в :mod:`skillery_cli.core.lease_sync`.

Проверка подписи/срока/субъекта здесь **не реализуется**: она ровно одна на CLI
и на gateway и живёт в ките ``s-leasekit`` (инвариант §10.3 контракта). Здесь —
только то, чего кит знать не может: где у CLI лежит учётка, какие способности
на этой машине вообще требуют лиз, и что значит «запустить навык».

────────────────────────────────────────────────────────────────────────────
ДВА ФАЙЛА СОСТОЯНИЯ (оба в каталоге конфига, рядом с ``tokens.toml``)

* ``leases.json`` — сами лизы + монотонный пол времени. Формат и дисциплина
  «пол только растёт» — в ките (``LeaseStore``), второй копии тут нет.
* ``capabilities.json`` — **реестр требований**: какие способности требуют лиз,
  какой навык их несёт, и смещение часов хаба. Пишет его демон, читает — гейт.

ПОЧЕМУ РЕЕСТР ТРЕБОВАНИЙ ОТДЕЛЬНО ОТ ЛИЗОВ, И ПОЧЕМУ ОН «ЛИПКИЙ». Без него
гейт был бы построен на наличии лиза («лиза нет — значит и не надо»), а это
fail-open наизнанку: удаление ``leases.json`` снимало бы проверку со всего.
Реестр отвечает на другой вопрос — «требуется ли здесь лиз вообще», — и потому
строка из него **не удаляется**, когда способность пропала из
``GET /me/capabilities``. Пропала она ровно потому, что право отозвано; выкинув
требование вслед за правом, мы бы своими руками открыли доступ в момент отзыва.
Строка уходит только вместе с навыком-носителем (снятие) — см.
:func:`forget_skill`.

ПРАВИЛО ГЕЙТА НА УРОВНЕ НАВЫКА. ``skillery run`` запускает НАВЫК (slug), а лиз
выдаётся на СПОСОБНОСТЬ. Соответствия «команда навыка → способность» в системе
нет ни на одной стороне (манифест объявляет ``[[capabilities]]`` и ``[[cli]]``
независимо), поэтому правило выбрано по §6.3 контракта, который разбирает
ровно этот случай: «отзыв ``grok_transcriber`` при живом гранте на ``grok_ask``
не снимает ``grok-chat``». Отсюда:

* у навыка нет способностей с ``requires_lease`` → запуск идёт как раньше,
  гейта нет вовсе (решение §12.В — гейт точечный);
* есть хотя бы одна → нужен **хотя бы один действующий лиз** среди них.
  Требовать лиз на КАЖДУЮ значило бы ломать работающую способность из-за
  соседней, которую пользователю никогда не выдавали, — прямо запрещено §6.3.

Расхождение с постановкой #1490 (там «run читает локальный лиз», единственное
число) отмечено в отчёте: слаг и способность — не одно и то же, и выбор правила
пришлось делать здесь.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leasekit import LeaseDenied, LeaseStore, LeaseVerdict, lease_gate

_LOG = logging.getLogger(__name__)

#: Порог обновления лиза — ДОЛЯ TTL, а не абсолютные сутки.
#:
#: Постановка #1490 говорит «обновлять лизы, истекающие в ближайшие сутки». При
#: TTL 24 ч (решение владельца §12.А) под этот критерий попадают ВСЕ лизы
#: ВСЕГДА: каждое устройство перевыпускало бы весь свой набор каждые 180 секунд
#: такта — 480 походов в сутки на машину, помноженные на парк. Правило контракта
#: (§3, врезка) — доля TTL: обновляем, когда осталось меньше ⅓ срока (при TTL
#: 24 ч это 8 ч), то есть ≈3 похода в сутки. Арифметику исправил владелец
#: комментарием к задаче 2026-08-06.
LEASE_REFRESH_FRACTION = 1.0 / 3.0

#: Имя файла реестра требований (лизы лежат рядом, имя даёт кит).
REQUIREMENTS_FILENAME = "capabilities.json"
REQUIREMENTS_VER = 1

_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600, как tokens.toml и leases.json


# ---------------------------------------------------------------------------
#  Где лежит состояние и кто субъект
# ---------------------------------------------------------------------------
def state_dir() -> Path:
    """Каталог состояния лизов — тот же, где ``tokens.toml``.

    Профиль учитывается (``--profile`` даёт свой каталог), и это осознанно: лиз
    привязан к субъекту, а профиль — это и есть другая учётка. Общий на профили
    файл означал бы ``SUBJECT_MISMATCH`` при каждом переключении.
    """
    from skillery_cli.config import _default_config_dir

    return _default_config_dir()


def lease_store() -> LeaseStore:
    """Хранилище лизов кита, наведённое на каталог состояния."""
    return LeaseStore(state_dir())


def current_subject(access_token: str | None = None) -> str | None:
    """``"user:<id>"`` текущей учётки — то, с чем сверяется ``sub`` лиза.

    Берётся из claim ``sub`` access-токена (backend кладёт туда ``str(user_id)``,
    ``jose_issuer.py:144``), а лиз выписывается на ``f"user:{user_id}"``
    (``application/capability/lease.py:196``). Подпись токена здесь НЕ
    проверяется и проверять её незачем: подделав себе ``sub``, злоумышленник
    получил бы ``SUBJECT_MISMATCH`` на лизе, который подписан хабом, — то есть
    отказ, а не доступ.

    **Истёкший токен годится.** Офлайн-машина живёт по действующему лизу, и
    требовать свежую сессию для локальной проверки значило бы убить ровно тот
    сценарий, ради которого лиз существует.
    """
    from skillery_cli.config import ClientConfig, decode_jwt_claims, load_tokens

    token = access_token
    if not token:
        cfg = ClientConfig.load()
        if not cfg.user_email:
            return None
        try:
            token, _ = load_tokens(cfg.user_email)
        except Exception as exc:  # noqa: BLE001 — keyring/файл могут быть недоступны
            _LOG.debug("токен для субъекта лиза не прочитан: %s", exc)
            return None
    if not token:
        return None
    sub = decode_jwt_claims(token).get("sub")
    if sub is None or str(sub).strip() == "":
        return None
    return f"user:{sub}"


# ---------------------------------------------------------------------------
#  Реестр требований
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """Одна строка реестра: способность, её носитель и нужен ли ей лиз."""

    name: str
    requires_lease: bool = False
    capability_id: str = ""
    skill_id: str = ""
    skill: str = ""
    """Slug навыка-носителя, если он известен (резолвится по ``/me/installs``)."""

    def to_json(self) -> dict[str, Any]:
        return {
            "requires_lease": bool(self.requires_lease),
            "capability_id": self.capability_id,
            "skill_id": self.skill_id,
            "skill": self.skill,
        }


@dataclass(slots=True)
class RequirementsIndex:
    """``capabilities.json`` — реестр требований + смещение часов хаба.

    Файл читается гейтом на КАЖДОМ ``skillery run``, поэтому он маленький и
    плоский: разбор одного json без сети и без импорта транспорта.
    """

    root: Path
    hub_offset: int = 0
    items: dict[str, CapabilityRequirement] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if self.items is None:
            self.items = {}

    @property
    def path(self) -> Path:
        return self.root / REQUIREMENTS_FILENAME

    # --- чтение ---------------------------------------------------------
    @classmethod
    def load(cls, root: Path | None = None) -> RequirementsIndex:
        """Прочитать реестр. Битый/отсутствующий файл = пустой реестр.

        Почему порча реестра — НЕ отказ (в отличие от порчи ``leases.json``,
        где кит справедливо требует fail-closed): реестр отвечает на вопрос
        «нужен ли здесь гейт», и трактовать его порчу как «гейт нужен везде»
        значило бы одним битым файлом остановить все навыки на машине.
        Направление ошибки выбрано в сторону работоспособности сознательно —
        гарантию даёт истечение лиза, а не этот кэш.
        """
        idx = cls(root if root is not None else state_dir())
        path = idx.path
        if not path.exists():
            return idx
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            _LOG.warning("реестр способностей %s не прочитан: %s", path, exc)
            return idx
        if not isinstance(payload, dict):
            return idx
        raw_offset = payload.get("hub_offset", 0)
        idx.hub_offset = int(raw_offset) if isinstance(raw_offset, int) else 0
        raw = payload.get("capabilities")
        if isinstance(raw, dict):
            for name, row in raw.items():
                if not isinstance(name, str) or not isinstance(row, dict):
                    continue
                idx.items[name] = CapabilityRequirement(
                    name=name,
                    requires_lease=bool(row.get("requires_lease")),
                    capability_id=str(row.get("capability_id") or ""),
                    skill_id=str(row.get("skill_id") or ""),
                    skill=str(row.get("skill") or ""),
                )
        return idx

    # --- запрос ---------------------------------------------------------
    def gated_for_skill(self, slug: str, skill_id: str | None = None) -> tuple[str, ...]:
        """Способности навыка, которые требуют лиз (имена, отсортированы).

        Матч по slug ИЛИ по числовому ``skill_id``: slug известен всегда (это
        аргумент ``run``), а ``skill_id`` — не всегда (реестр мог не успеть
        резолвить его через ``/me/installs``). Совпадения по любому из двух
        достаточно; требовать оба значило бы терять гейт на машине, где
        резолв не прошёл.
        """
        wanted_id = str(skill_id or "").strip()
        out = [
            name
            for name, row in self.items.items()
            if row.requires_lease
            and ((slug and row.skill == slug) or (wanted_id and row.skill_id == wanted_id))
        ]
        return tuple(sorted(out))

    def lease_wanted(self) -> tuple[str, ...]:
        """Все способности реестра — набор для ``PUT /me/leases`` (см. lease_sync)."""
        return tuple(sorted(self.items))

    # --- запись ---------------------------------------------------------
    def merge(self, rows: list[CapabilityRequirement]) -> RequirementsIndex:
        """Влить свежие строки из ``/me/capabilities``. Отсутствующие — НЕ трогать.

        Липкость (см. модульный docstring): способность исчезает из ответа
        именно тогда, когда право отозвано. Удалив требование вслед за правом,
        гейт снимался бы ровно в момент отзыва — то есть работал бы наоборот.
        """
        for row in rows:
            old = self.items.get(row.name)
            merged = row
            if old is not None:
                # Ответ может не нести slug (его резолвит вызывающий по installs)
                # — не затираем уже известное пустым.
                merged = CapabilityRequirement(
                    name=row.name,
                    requires_lease=row.requires_lease,
                    capability_id=row.capability_id or old.capability_id,
                    skill_id=row.skill_id or old.skill_id,
                    skill=row.skill or old.skill,
                )
            self.items[row.name] = merged
        return self

    def forget_skill(self, slug: str, skill_id: str | None = None) -> int:
        """Убрать требования навыка (вызывается при СНЯТИИ навыка). Вернёт число строк.

        Единственная законная причина забыть требование: файлов навыка на
        машине больше нет, исполнять нечего. Отзыв права такой причиной не
        является (§6.3: лиз — про «можно исполнять», снятие — про «должны ли
        лежать файлы»).
        """
        wanted_id = str(skill_id or "").strip()
        doomed = [
            name
            for name, row in self.items.items()
            if (slug and row.skill == slug) or (wanted_id and row.skill_id == wanted_id)
        ]
        for name in doomed:
            self.items.pop(name, None)
        return len(doomed)

    def save(self) -> None:
        """Атомарная запись с правами 600 — как ``tokens.toml``."""
        payload = {
            "ver": REQUIREMENTS_VER,
            "hub_offset": int(self.hub_offset),
            "capabilities": {n: r.to_json() for n, r in sorted(self.items.items())},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.root), prefix=".caps-", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(tmp, _FILE_MODE)
            except OSError:  # экзотические ФС / Windows
                pass
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
#  Гейт запуска
# ---------------------------------------------------------------------------
#: Приоритет вердикта в тексте отказа, когда способностей несколько.
#:
#: Пользователю показываем САМУЮ содержательную причину: «срок истёк» и «часы
#: врут» объясняют ситуацию, а «нет доступа» — это то, что видно и так. Без
#: порядка текст зависел бы от порядка ключей в json.
_VERDICT_RANK: dict[LeaseVerdict, int] = {
    LeaseVerdict.CLOCK_SUSPECT: 90,
    LeaseVerdict.EXPIRED: 80,
    LeaseVerdict.SUBJECT_MISMATCH: 70,
    LeaseVerdict.UNSUPPORTED_VER: 60,
    LeaseVerdict.SIGNATURE_INVALID: 50,
    LeaseVerdict.UNKNOWN_KID: 50,
    LeaseVerdict.CAPABILITY_MISMATCH: 40,
    LeaseVerdict.ISSUER_MISMATCH: 40,
    LeaseVerdict.MALFORMED: 40,
    LeaseVerdict.NOT_YET_VALID: 30,
    LeaseVerdict.MISSING: 10,
}

#: Текст, когда лиз требуется, а учётки на машине нет вовсе. Отдельный от
#: китовых: кит объясняет отказ ПРОВЕРКИ, а здесь проверять ещё нечего —
#: подсказка обязана вести к входу, а не к переустановке способности.
_NO_SUBJECT_MESSAGE = (
    "Нет доступа к «{cap}»: на этом устройстве нет учётной записи. "
    "Выполните `skillery login`"
)


def check_skill_run(slug: str, *, skill_id: str | None = None) -> LeaseDenied | None:
    """Решить, можно ли запускать навык. ``None`` = можно.

    В сеть НЕ ходит — ни при каком исходе (ЦКП #1490). Возвращает отказ, а не
    поднимает: вызывающий (``run``) обязан обернуть его в свою доменную ошибку
    с кодом, а поднимать китовое исключение сквозь typer нельзя.

    Исходы:

    * у навыка нет гейтуемых способностей → ``None`` (гейт точечный, §12.В);
    * учётки на устройстве нет → отказ ``MISSING`` с подсказкой ``login``;
    * есть действующий лиз хотя бы на одну → ``None`` (см. §6.3 и docstring
      модуля);
    * ни одного действующего → отказ с самой содержательной причиной.
    """
    index = RequirementsIndex.load()
    gated = index.gated_for_skill(slug, skill_id)
    if not gated:
        # Навык без платных способностей. Ни файлов не читаем, ни ключей — путь
        # обычного навыка обязан остаться ровно таким, каким был.
        return None

    subject = current_subject()
    if subject is None:
        return LeaseDenied(
            LeaseVerdict.MISSING,
            _NO_SUBJECT_MESSAGE.format(cap=gated[0]),
            capability=gated[0],
        )

    from skillery_cli.core.identity import device_uid

    store = lease_store()
    device_id = device_uid()
    worst: LeaseDenied | None = None
    for capability in gated:
        try:
            verified = lease_gate(
                capability,
                store=store,
                subject=subject,
                device_id=device_id,
                hub_offset=index.hub_offset,
                # ``issuer`` не проверяем: ожидаемый ``iss`` — это
                # ``public_base_url`` ХАБА, а CLI знает только собственный
                # ``base_url`` (у прода это разные хосты: api.* и hub.*).
                # Сверять с догадкой значило бы ложно отказывать. Привязку к
                # хабу и так даёт подпись: ключи взяты из его же JWKS.
                issuer=None,
            )
        except LeaseDenied as denied:
            if worst is None or _VERDICT_RANK.get(denied.verdict, 0) > _VERDICT_RANK.get(
                worst.verdict, 0
            ):
                worst = denied
            continue
        for warning in verified.warnings:
            _LOG.warning(
                "лиз «%s»: %s", capability, warning.value,
                extra={"context": {"capability": capability, "warning": warning.value}},
            )
        return None
    return worst


__all__ = [
    "LEASE_REFRESH_FRACTION",
    "REQUIREMENTS_FILENAME",
    "CapabilityRequirement",
    "RequirementsIndex",
    "check_skill_run",
    "current_subject",
    "lease_store",
    "state_dir",
]
