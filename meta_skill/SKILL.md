---
name: skills-hub
description: |
  Bootstrap-навык Skills Hub: ставит `skills-hub` CLI у клиента, регистрирует
  агента по invite-токену ИЛИ самостоятельно (register без инвайта / join по
  пригласительной ссылке), устанавливает / обновляет приватные навыки команды
  Дмитрия, а также управляет оценками (E7), комментариями (E7), тикетами тех-
  поддержки (E8), коллекциями (E10) — включая локальные оффлайн-коллекции,
  компаниями и участниками из CLI (P1: company create / invite-links /
  catalog grant, member invite / change-role), онбордингом проекта (onboard:
  детект стека → подбор навыков) и event-tracking daemon (E23).
  Используй когда пользователь говорит: «поставь skills-hub», «залогинься
  в skills-hub», «зарегистрируйся в hub», «вступи в компанию по ссылке»,
  «обнови мои навыки hub», «skills-hub install <slug>», «что
  есть в каталоге skills-hub», «оцени skill X», «оставь коммент к skill»,
  «отправь тикет в поддержку», «покажи коллекции», «создай локальную
  коллекцию», «создай компанию из CLI», «пригласи участника в компанию»,
  «выдай компании навык», «подбери навыки под проект», «включи телеметрию
  skills-hub». EN triggers: install skills-hub, login to skills hub, register
  in skills hub, join a company by invite link, update hub skills, list hub
  skills, rate a hub skill, comment on a hub skill, open a support ticket,
  list hub collections, create a local skill collection, create a company
  from CLI, invite a member, grant a skill to a company, onboard a project,
  suggest skills for this project, start skills-hub daemon.
  RU triggers: поставь skills-hub, залогинь меня в hub, зарегистрируй меня в
  hub, вступи в компанию по ссылке, обнови навыки, список навыков hub, оцени
  навык, прокомментируй навык, тикет в поддержку, список коллекций hub,
  локальная коллекция навыков, создай компанию, пригласи участника, выдай
  навык компании, онбординг проекта, подбери навыки под стек, запусти
  телеметрию hub.
---

# Skills Hub bootstrap-skill

Точка входа AI-агента (Claude Code / Codex) в систему Skills Hub. После
установки агент умеет:

1. **Поставить CLI** — `python ./scripts/install.py` (ставит из исходников
   монорепо через pipx/pip; см. «Установка CLI» ниже).
2. **Авторизовать** клиента через invite-токен, email + password ИЛИ
   самостоятельно: `register` (без инвайта, юзер без компании) / `join
   <ссылка>` (вступление в компанию по переиспользуемой пригласительной
   ссылке) — обе команды работают до login.
3. **Перечислить** доступные навыки (RBAC) и **установить** нужные (global
   или per-project scope).
4. **Ставить навык автономно — без хаба** — из локальной папки
   (`install --path ./skill`) или из git (`install --from-git <url>`), что
   позволяет положить любой навык в стор без подключения к Skills Hub.
5. **Обновлять** установленные навыки (`update --all`, либо auto-update
   daemon-ом в фоне).
6. **Оценивать и комментировать** навыки (E7): 1–5 звёзд, threaded comments
   со скриншотами.
7. **Открывать тикеты тех-поддержки** (E8) — по навыку или общие.
8. **Просматривать и устанавливать коллекции** (E10) — кураторские подборки
   навыков (статические или динамические по тегам); `collection install`
   ставит всю подборку одной командой (главный онбординг-кейс).
9. **Вести локальные коллекции** (P1) — личные наборы слагов в
   `~/.skills-hub/collections.toml`; работают полностью **оффлайн**, без
   логина и хаба (`collection create / add / install … --local`).
10. **Управлять компанией из CLI** (P1) — `company create / edit / switch`,
    переиспользуемые пригласительные ссылки (`invite-links`), granted-каталог
    (`catalog grant`), участники (`member invite / change-role / lock …`).
11. **Онбордить проект** — `skills-hub onboard`: детект стека по
    маркер-файлам → подбор навыков из стора и хаба → `--yes` включает их в
    проект.
12. **Запускать event-tracking daemon** (E23) — фон-агент батчем шлёт события
    жизненного цикла (`skill.install`/`update`/`enable`/`disable`/`uninstall`,
    + `scope` и `source` в payload) в `/events`; autorestart на всех платформах.
13. **Сообщать о багах** через `skills-hub report` (исторический shortcut
    к тикетам).

## Установка CLI

Пакет `skills-hub-cli` пока **не опубликован** в PyPI/npm (это roadmap).
Основной путь сейчас — установка из исходников репозитория:

```bash
# Из корня монорепо (рекомендуемый способ — изолированно через pipx):
python ./scripts/install.py            # ставит client/ через pipx (или pip как fallback)

# Эквивалент вручную:
pipx install <repo>/client             # изолированное окружение
pip install -e <repo>/client           # editable-режим в текущий venv
```

`scripts/install.py` ставит CLI из соседней папки `client/` монорепо. Если
запустить его в отрыве от репо (папки `client/` рядом нет) — он напечатает
внятную ошибку с инструкцией, а не упадёт молча.

> PyPI / npm дистрибутивы — **планируется**. До их выхода `pip install
> skills-hub-cli` работать НЕ будет.

## Quickstart для агента

```bash
# 1. Один раз — установить CLI (из исходников монорепо)
python ./scripts/install.py

# 2. Авторизоваться (одно из двух)
skills-hub login <invite-token-or-URL>          # первый вход — invite
skills-hub login --email me@x.io --password ... # последующие — пароль

# 3. Посмотреть, что доступно (RBAC фильтрует автоматически)
skills-hub list

# 4. Установить нужный skill (global = всем сессиям, project = только текущему cwd)
skills-hub install bitrix24                      # default scope из config
skills-hub install bitrix24 --scope project      # только в cwd/.claude/skills/

# 4b. Автономно — без хаба, из локальной папки или git
skills-hub install --path ./my-skill             # положить навык из папки в стор
skills-hub install --from-git <url> --ref v1.0.0 # из git-репозитория

# 5. Проверить статус
skills-hub status                                # global + project + agent + login

# 6. Обновить все
skills-hub update --all                          # global + project одной командой
```

## Модель установки: центральный стор + ссылки

Навык материализуется **один раз** в центральный стор
(`~/.skills-hub/store/<slug>/`, путь настраивается `SKILLS_HUB_STORE_DIR`).
В каждый scope CLI кладёт **junction (Windows) / symlink (mac/linux)** на стор —
не копию. Это даёт:
- один контент на диске, много ссылок (global + любые проекты);
- `update` обновляет стор → все проекты сразу видят новую версию;
- динамическое управление набором проекта без перекачки.

**Scope по умолчанию теперь `project`** (`install <slug>` без `--scope` включает
навык в текущий проект и пишет `.skills-hub/skills.toml`). Для глобальной
установки: `install <slug> --scope global`.

Если ссылку создать нельзя (нет прав/ФС не поддерживает) — CLI делает копию и
предупреждает (`📄 copy` в `status`); динамическое управление для такого навыка
ограничено.

### Автономная установка — без хаба

`install` умеет ставить навык **в обход Skills Hub**, прямо в стор, без логина
и без обращения к backend:

- `skills-hub install --path ./skill` — взять навык из локальной папки;
- `skills-hub install --from-git <url> [--ref <tag>]` — склонировать навык из
  git (по умолчанию default-ветка, `--ref` фиксирует тег/коммит/ветку).

Это позволяет положить **любой** навык в центральный стор и подключить его в
проект (через ссылку) без подключения к хабу — удобно для локальной разработки
навыков и для навыков, которых нет в каталоге.

## Команды по группам (доступность зависит от RBAC permissions в JWT)

### Auth + статус (always-on)

| Команда                     | Назначение                                       |
| --------------------------- | ------------------------------------------------ |
| `skills-hub login`          | Invite-token или email+password flow             |
| `skills-hub logout`         | Очистить локальные токены и permissions          |
| `skills-hub whoami`         | Кто я + permissions + роли                       |
| `skills-hub status`         | Что установлено (global + project) + agent + login |
| `skills-hub passwd`         | Сменить (или установить) пароль (требует login)  |
| `skills-hub config`         | Output format, auto-update, default install scope |
| `skills-hub web`            | Открыть Web UI с автоматической авторизацией     |

### Аккаунт и вступление — P1 (always-on, работают ДО login)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub register --email E --password P [--name N]` | Самостоятельная регистрация **без инвайта**: создаёт юзера без компании (доступ к публичным навыкам) + локальную сессию. Пароль ≥ 8 символов. Display-name по умолчанию — часть email до `@`, `--name` переопределяет |
| `skills-hub join <токен-или-URL> [--email E --password P [--name N]]` | Вступить в компанию по переиспользуемой пригласительной ссылке. Принимает и готовый URL (`…/join/<token>`), и голый токен |

Семантика `join` зависит от состояния сессии:

- **Залогинен** → вступление ТЕКУЩИМ аккаунтом (`--email`/`--password`
  игнорируются). Чтобы вступить вторым аккаунтом — сначала
  `skills-hub logout`.
- `join` у залогиненного **НЕ меняет активную компанию** в токене — после
  вступления переключись: `skills-hub company switch <id>`.
- **Не залогинен** → регистрация по ссылке: обязательны `--email` и
  `--password` (≥ 8 символов; +опц. `--name`) → создаётся юзер + membership +
  локальная сессия.

В `--json`-режиме `--email`/`--password` обязательны флагами (интерактивного
prompt'а нет).

### Каталог и установка (`skill.read`, `skill.install`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub list`                                      | Доступные скилы (RBAC)                    |
| `skills-hub list --installed [--scope all\|global\|project]` | Что уже стоит                       |
| `skills-hub show <id-или-slug>`                        | Детали (включая versions / tags / repo)   |
| `skills-hub install <id-или-slug> [--scope ...] [--force]` | Поставить из хаба (global или project) |
| `skills-hub install --path ./skill [--scope ...]`      | Автономно: поставить навык из локальной папки (без хаба) |
| `skills-hub install --from-git <url> [--ref <tag>] [--scope ...]` | Автономно: поставить навык из git (без хаба) |
| `skills-hub update [<id-или-slug>] [--all] [--scope ...]` | Обновить установленные                 |
| `skills-hub enable <id-или-slug> [--project P]`        | Включить навык в наборе проекта (стор + ссылка + манифест) |
| `skills-hub disable <id-или-slug> [--project P]`       | Выключить из набора проекта (снять ссылку + манифест; стор цел) |
| `skills-hub sync [--project P] [--no-prune]`           | Привести project scope к `.skills-hub/skills.toml` |
| `skills-hub migrate [--scope all\|global\|project] [--dry-run]` | Перевести старые copy-установки в стор+ссылки |
| `skills-hub store list / path / gc`                    | Содержимое стора / путь / сборка мусора |
| `skills-hub remove <id-или-slug> [--purge]`            | Снять ссылку (с `--purge` — и из стора) |

> **Always-on:** `install` (автономные `--path` / `--from-git`), `enable`,
> `disable`, `remove`, `sync`, `migrate`, `store *` работают с локальным
> стором **без login** — сеть нужна только hub-веткам (sync-докачка,
> hub-enable), которые сами попросят авторизацию. Гейты `skill.read` /
> `skill.install` касаются hub-операций: `list`, `show`, `install` из хаба,
> `update`.

### Оценки и комментарии — E7 (`skill.rate`, `comment.post`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub rate <id-или-slug> <1-5>`                  | Поставить / обновить свою оценку 1..5     |
| `skills-hub rating-summary <id-или-slug>`              | Средний балл + распределение (если есть право `skill.read`) |
| `skills-hub comment <id-или-slug> "<body>" [--screenshot path] [--parent <cmt_id>]` | Запостить коммент / ответ |
| `skills-hub comments <id-или-slug> [--page N] [--size N]` | Список комментариев skill'а          |
| `skills-hub comment-edit <cmt_id> "<new body>"`        | Редактировать свой коммент                |
| `skills-hub comment-delete <cmt_id>`                   | Soft-delete (автор или hub-admin)         |
| `skills-hub contributors <id-или-slug>`                | Авторы и количество коммитов (из git)     |

### Тикеты тех-поддержки — E8 (`ticket.create`, `ticket.read`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub ticket create "<subject>" [--skill <id-или-slug>] [--kind bug\|feature\|question\|other] [--priority low\|normal\|high\|urgent] [--body "..."]` | Завести тикет; assignee выбирается авто (creator skill'а → company-admin → none) |
| `skills-hub tickets list [--status ...] [--kind ...] [--priority ...] [--skill <id-или-slug>] [--page N] [--page-size N]` | Список тикетов в твоём scope (RBAC: hub-admin → все; company-admin → company; skill-creator → свои назначения; user → свои создания) |
| `skills-hub ticket show <tkt_id>`                      | Тикет + thread сообщений                  |
| `skills-hub ticket reply <tkt_id> "<body>" [--screenshot path]` | Добавить ответ в thread             |
| `skills-hub ticket status <tkt_id> <new\|in_progress\|scheduled\|done\|rejected>` | Сменить статус (assignee / hub-admin) |

> **Планируется** (пока НЕ реализовано): `ticket assign <tkt_id> <user_id>`
> (назначение ответственного вручную) и фильтры `tickets list --mine /
> --assigned-to-me`. Сейчас scope листинга определяется backend'ом по роли,
> а assignee выбирается автоматически.

`skills-hub report` (legacy) → внутри транслируется в `ticket create
--skill <id-или-slug> --kind bug`.

### Коллекции — E10 + P1 (единый sub-app `collection`)

Единый sub-app `collection` с 7 глаголами: `list / show / install / create /
add / remove / delete` (все always-on в `--help`). Флаг **`--local`**
переключает источник: без него — **серверная** коллекция хаба (gate по правам
JWT), с ним — **локальная** оффлайн-коллекция в `collections.toml`. Plural
`collections` убран.

#### Серверные (хаб) — default, read-only + install (`skill.read` / `skill.install`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub collection list [--type static\|dynamic] [--company C] [--owner O] [--no-global]` | Список серверных коллекций (static + dynamic). Gate `skill.read` |
| `skills-hub collection show <id-или-slug>`             | Детали + развёрнутый список skills. Gate `skill.read` |
| `skills-hub collection install <id-или-slug>`          | Поставить все skills из коллекции одной командой (главный онбординг-кейс). Gate `skill.install` |

> Серверный режим гейтится в рантайме: без `skill.read` — `list`/`show`
> отвечают `NOT_AVAILABLE` с подсказкой добавить `--local`; `install` без
> `skill.install` — то же. Создание/редактирование **серверных** коллекций
> (`create` / `add` / `remove` / `delete`) из CLI **не реализовано** — это
> делается в Web UI; в CLI эти глаголы работают только локально (см. ниже).

#### Локальные (`--local`) — оффлайн, без хаба и логина (always-on)

Личные наборы слагов в `~/.skills-hub/collections.toml` (точнее —
`<config_dir>/collections.toml`: уважает `SKILLS_HUB_CONFIG_DIR` и
`--profile`). Не требуют ни логина, ни сети — Web UI их не видит. Для
`create / add / remove / delete` флаг `--local` **обязателен** (без него —
ошибка `USE_LOCAL_FLAG`).

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub collection create <name> --local [--title T]` | Создать локальную коллекцию (имя: буквы/цифры/`-`/`_`; `--title` — человекочитаемый заголовок) |
| `skills-hub collection add <name> <skill-slug> --local`  | Добавить навык. Слага нет в сторе → warning, но слаг добавится — `install … --local` докачает его из хаба при логине |
| `skills-hub collection remove <name> <skill-slug> --local` | Убрать навык из коллекции (диск/стор не трогаются) |
| `skills-hub collection list --local`                   | Список: имя, размер, какие слаги отсутствуют в сторе |
| `skills-hub collection show <name> --local`            | Одна локальная коллекция: состав + чего нет в сторе |
| `skills-hub collection install <name> --local [--scope global\|project] [--project P] [--channel C] [--force] [--agent A]` | Установить коллекцию (см. ниже) |
| `skills-hub collection delete <name> --local`          | Удалить коллекцию (установленные навыки на диске не трогаются) |

`install … --local` для каждого слага коллекции:

- **есть в сторе** → локальный линк в scope (как `enable`, БЕЗ сети);
- **нет в сторе + залогинен** → докачка из хаба (`--channel` задаёт канал);
- **нет в сторе + НЕ залогинен** → skip с подсказкой — команда не падает.

Итог в `--json`: `{installed: [...], linked: [...], skipped: [...]}`.

### Онбординг проекта — P1 (always-on)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub onboard [--project P] [--yes] [--limit N] [--agent A]` | Детект стека проекта → подбор навыков (стор + хаб) → `--yes` включает их в проект |

Конвейер:

1. **Детект сигналов** по маркер-файлам корня проекта: `python`
   (pyproject.toml / requirements.txt), `nodejs` (+`nextjs`/`react` по
   dependencies в package.json), `docker` (Dockerfile / docker-compose*),
   `terraform` (*.tf), `go` (go.mod), `rust` (Cargo.toml), `claude-code`
   (папка .claude/).
2. **Кандидаты**: локальный стор (матч сигнала по tags / slug / description)
   + bounded hub-поиск (`GET /skills?q=…`, только если залогинен; `--limit`
   кандидатов на сигнал, капится 20).
3. **Без `--yes`** — только таблица предложений (`--json` отдаёт
   `{signals, suggestions}`); уже включённые в проект помечаются `already`
   и повторно не трогаются (идемпотентность).
4. **`--yes`** включает каждый не-`already` кандидат: есть в сторе → линк +
   манифест проекта; нет в сторе → докачка из хаба (нужен login, иначе skip).

Без логина команда штатно работает только по локальному стору — это не
ошибка, hub-ветка просто не включается.

### Event tracking + daemon — E23 (`events.send`)

| Команда                                      | Назначение                                  |
| -------------------------------------------- | ------------------------------------------- |
| `skills-hub event track <type> [--resource-type T] [--resource-id ID] [--payload JSON] [--metadata JSON]` | Положить произвольный event в локальную очередь |
| `skills-hub event queue [--show] [--clear]`  | Инспектировать (или очистить) локальную очередь |
| `skills-hub event flush`                     | Синхронно отправить накопленный батч (один цикл sender'а) |
| `skills-hub daemon run [--interval N]`       | Foreground-цикл (для systemd / launchd / schtasks) |
| `skills-hub daemon start [--interval N]`     | Запустить фон-процесс (collector + sender)  |
| `skills-hub daemon stop`                     | Остановить (SIGTERM по PID-файлу)           |
| `skills-hub daemon status`                   | PID / последний цикл / queue size (+ предупреждение, если демон мёртв и очередь непуста) |
| `skills-hub daemon install [--platform ...]` | Поставить как autostart (launchd / systemd user / Task Scheduler) с autorestart + boot-trigger |
| `skills-hub daemon uninstall [--platform ...]` | Убрать autostart-юнит                      |

Подкоманды — sub-app `event` (в единственном числе): `event track / queue /
flush`. CLI **никогда не шлёт события синхронно** в основной команде
(`install`/`enable`/`update`/`disable`/`uninstall`): он только **кладёт их в
локальную очередь** `~/.skills-hub/events.queue.json` (silent fail — телеметрия
никогда не ломает основную команду). Отправку батчем на `POST /events` делает
**daemon** (или `event flush` вручную для отладки). Отправка анонимна
(anonymous-fallback при отсутствии/протухании токена); реальный приём backend
гейтит правом `events.send` для именованного актора.

**Модель событий (канон):** материализация-в-стор ≠ включение-в-проект.
`skill.install` — навык материализован в стор; `skill.update` — контент в сторе
обновлён; **`skill.enable`** — создана project-ссылка (включён в проект);
**`skill.disable`** — ссылка снята (стор цел); `skill.uninstall` — удалён из
стора (`remove --purge` / global). `install --scope project` шлёт ДВА события
(install + enable). Каждое install/enable/update несёт в payload `scope`
(`global`|`project`) и `source` (`hub`|`local-path`|`git-url`). `skill.run`
(чтение агентом) — отложен.

### Публикация (только `skill.publish`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub publish <id-или-slug> --tag v0.1.0 [--dry-run]` | Опубликовать новую версию из локальной папки (slug при создании задаёт hub-admin) |

### Компании — P1 (sub-app `company`, гейты по правам)

`--company` в подкомандах опционален — по умолчанию берётся активная
компания из JWT (нет ни флага, ни company_id в токене → внятная ошибка
`NO_COMPANY`).

| Команда                                                | Право                  | Назначение |
| ------------------------------------------------------ | ---------------------- | ----------- |
| `skills-hub company show <id>`                         | любой залогиненный     | Детали компании (бэк режет tenant-изоляцией) |
| `skills-hub company switch <id>`                       | любой залогиненный (нужен membership) | Переключить активную компанию: **перевыпускает токены** — новая пара сохраняется автоматически, набор команд в `--help` может измениться |
| `skills-hub company list [--q S] [--page N] [--size N]` | `hub.admin`           | Список компаний (server-side пагинация) |
| `skills-hub company create --name N --owner-email E --owner-name O [--slug S]` | `hub.company_create` | Создать компанию + invite owner'у (печатает owner-invite token и URL). `--slug` требует `hub.slug_manage`; без него — slug-less компания |
| `skills-hub company edit <id> [--name N] [--owner-id U]` | `company.manage`      | Изменить компанию (merge-patch) |
| `skills-hub company invite-links list [--company ID]`  | `company.manage` \| `role.manage` | Список переиспользуемых пригласительных ссылок |
| `skills-hub company invite-links create [--kind member\|manager] [--max-uses N] [--expires-in-days 1..365] [--company ID]` | `company.manage` \| `role.manage` | Создать ссылку — печатает **ГОТОВЫЙ join-URL** (`<web-ui>/join/<token>`); токен показывается **ОДИН раз**. `--kind manager` — только владелец компании |
| `skills-hub company invite-links revoke <link_id> [--company ID]` | `company.manage` \| `role.manage` | Отозвать ссылку |
| `skills-hub company catalog list [--company ID]`       | `catalog.manage` \| `catalog.view_all` | Granted-каталог компании: навыки + коллекции + effective skills |
| `skills-hub company catalog grant <id-или-slug> [--collection] [--company ID]` | `catalog.manage` | Выдать компании навык (или коллекцию при `--collection`). Ref = slug или id — slug резолвится автоматически |
| `skills-hub company catalog revoke <id-или-slug> [--collection] [--company ID]` | `catalog.manage` | Отозвать навык / коллекцию |

### Участники — P1 (`members` / `member` / `roles`)

| Команда                                                | Право                  | Назначение |
| ------------------------------------------------------ | ---------------------- | ----------- |
| `skills-hub members [--company ID] [--q S] [--page N] [--size N]` | любой залогиненный | Участники компании (серверная пагинация). Без admin-прав бэк показывает только вас; hub-admin без `--company` видит всех пользователей хаба |
| `skills-hub roles`                                     | любой залогиненный     | Глобальный каталог ролей — для выбора `role-id` |
| `skills-hub member invite --role-id R [--email E] [--name N] [--company ID]` | `user.invite` \| `invite.manage` | Пригласить участника (печатает invite-token + URL). ЕДИНСТВЕННАЯ реализация выдачи инвайта (#2267). `--email` опционален: задан — приглашённый сразу виден в `member list` (статус invited), опущен — выдаётся голая токен-ссылка. Display-name по умолчанию — часть email до `@`. `--company-id` — синоним `--company` |
| `skills-hub member remove <user_id> [--company ID]`    | `user.remove`          | Убрать из компании (сессии удалённого отзываются) |
| `skills-hub member change-role <user_id> <role_id> [--company ID]` | `role.manage` | Сменить роль. Не-assignable роль для company-admin отклоняется backend'ом (422) |
| `skills-hub member lock <user_id> [--reason R]`        | `user.lock`            | Заблокировать вход (сессии отзываются; self-lock запрещён) |
| `skills-hub member unlock <user_id>`                   | `user.lock`            | Снять блокировку |
| `skills-hub member reset-password <user_id>`           | `company.manage`       | Одноразовый пароль: показывается **ОДИН раз** (backend хранит лишь хэш). В `--json` пароль уходит в stdout — **агенту: не логировать**. Сессии пользователя отзываются |

### Административные действия (по правам, а не по имени группы)

Группа `admin` РАСФОРМИРОВАНА (#2267): действие живёт в группе своей
сущности, доступ решают права. Прежние имена продолжают работать скрытыми
устаревшими алиасами (с предупреждением в stderr) — но в новых скриптах
используйте канон.

| Команда                                              | Право                        | Назначение                                   |
| ----------------------------------------------------- | ---------------------------- | -------------------------------------------- |
| `skills-hub skill sync-versions <id-или-slug>`        | `hub.admin`                  | Подтянуть новые git-теги навыка как версии    |
| `skills-hub skill yank <id-или-slug> <semver>`        | `skill.manage` \| `hub.admin` | Снять версию из latest/install (`--unyank` — вернуть) |
| `skills-hub company create --name ... --owner-email ...` | `hub.company_create`      | Создать компанию + invite owner'у             |

Устаревшие имена (работают, из справки скрыты): `admin sync-skill` →
`skill sync-versions`, `admin yank` → `skill yank`, `admin company-create` →
`company create`, `admin invite` → `member invite` (см. таблицу участников выше).

`skills-hub --help` после login показывает только те команды, на которые у
пользователя есть permission в JWT — это управляется backend'ом в момент
выдачи токенов. **Always-on** (видны и работают без login): `login`,
`register`, `join`, `status`, `logout`, `whoami`, `config`, `web`,
`install` (автономные источники), `enable`, `disable`, `remove`, `sync`,
`migrate`, `store *`, `collection` (все глаголы видны; серверный режим
гейтится в рантайме, `--local` всегда оффлайн), `onboard`.

## Алгоритм для AI-агента

1. **Первый запуск в сессии** — `skills-hub status`. Если `logged_in=false` →
   попросить у пользователя invite-token или email+password (через
   `skills-hub login`). Если аккаунта нет вовсе: есть пригласительная ссылка
   компании → `skills-hub join <ссылка> --email … --password …`; нет ссылки →
   `skills-hub register --email … --password …` (без инвайта; детали и
   сценарии — в AGENTS.md).
2. **Установка нужного навыка** — если пользователь упомянул id-или-slug,
   который не установлен, выполни `skills-hub install <id-или-slug>`. Если
   навыка нет в каталоге, но есть его папка/git — поставь автономно:
   `skills-hub install --path ./skill` или `skills-hub install --from-git
   <url> [--ref <tag>]` (логин не нужен).
3. **Регистрация события** — при `install` / `update` / `uninstall` CLI сам
   отправит `skill.*` event. Дополнительно при ручном run можно
   `skills-hub event track skill.run --resource-type skill --resource-id
   <id-или-slug>`.
4. **Bug / feedback flow:**
   - Маленький баг → `skills-hub comment <id-или-slug> "<repro + лог>"`.
   - Систематический → `skills-hub ticket create "<subject>" --skill <id-или-slug>
     --kind bug --body "<полный лог>"`.
   - Идея фичи → `--kind feature`.
5. **Оценка после первого продуктивного использования** — мягко предложить
   `skills-hub rate <id-или-slug> <1-5>`. Не настаивать.
6. **Обновления** — если в config включён `auto_update=true`, CLI сам
   тихонько обновит при следующей команде. Иначе подсказать `skills-hub
   update --all`.
7. **Коллекции как onboarding** — если пользователь спрашивает «что у вас
   есть», сначала `skills-hub collection list` (серверные коллекции
   тематически сгруппированы), а уже потом `skills-hub list` (плоский). Для
   быстрого старта новому клиенту — `skills-hub collection install
   <id-или-slug>` ставит сразу всю подборку.
8. **Новый проект** — `skills-hub onboard` в корне проекта: покажи
   пользователю таблицу предложений (сигналы стека + кандидаты из стора и
   хаба), после подтверждения — `skills-hub onboard --yes`. Точечно вместо
   `--yes` — `skills-hub enable <slug>` по выбранным.
9. **Свой повторяемый набор** — оформи как локальную коллекцию:
   `collection create <name> --local`, `collection add <name> <slug> --local`,
   затем на любой машине `collection install <name> --local` (оффлайн из
   стора; недостающее докачается из хаба при логине).

## Manifest skill.json — опциональные поля

В `_skill_meta.toml` или фронтматтере `SKILL.md` поддерживаются поля
`description` / `triggers` / `tags` / `dependencies` (их `publish` собирает и
отправляет на backend).

```toml
# _skill_meta.toml
description = "..."
triggers = ["...", "..."]
tags = ["bitrix24", "crm"]
```

> **Roadmap (пока НЕ работает):** поля `rating_enabled` / `comments_enabled` /
> `support_tickets_enabled` / `support_assignee_email` / `support_kinds_allowed`
> описаны в [`manifest-schema.md`](./manifest-schema.md), но текущая команда
> `publish` их **не передаёт** на backend — это запланированная функциональность.
> Не полагайся на них в навыках, пока publish не начнёт их отправлять.

## Что делать при ошибке

1. `skills-hub status` — какой agent, какие версии стоят.
2. `skills-hub whoami` — какие permissions выданы (если команда не видна
   в `--help`, скорее всего нет права).
3. `skills-hub --json <cmd> ...` — машиночитаемый вывод для парсинга
   логов агента.
4. Если skill после install не работает у пользователя:
   - Прогони smoke-тесты skill'а (если есть).
   - Если воспроизводимый bug — `skills-hub ticket create "..." --skill
     <id-или-slug> --kind bug --screenshot trace.png`.

## Структура

См. также:
- [`scripts/install.py`](./scripts/install.py) — установщик CLI
- [`commands/`](./commands/) — slash-commands для Claude Code (опциональные)
- [`AGENTS.md`](./AGENTS.md) — техн. инструкции для агента (что делать
  по шагам)
- [`manifest-schema.md`](./manifest-schema.md) — описание полей manifest
- Базовый CLI: `skills-hub` (после установки доступен в PATH)
- Backend: `<base_url>/docs` — OpenAPI Swagger

## Ограничения текущей итерации

- Auto-update daemon — opt-in, по дефолту выключен (приватность важнее
  свежести).
- Event tracking — batched HTTP, не realtime WebSocket.
- Скриншоты к комментам / тикетам — multipart upload до 5 МБ на файл.
- Backend знает только GitLab.com (приватная группа `dmitry-skills-hub`).
- Коллекции `dynamic` пересчитываются на стороне backend при каждом запросе
  (нет кеша).
