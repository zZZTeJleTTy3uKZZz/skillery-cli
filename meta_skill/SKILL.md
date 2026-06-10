---
name: skills-hub
description: |
  Bootstrap-навык Skills Hub: ставит `skills-hub` CLI у клиента, регистрирует
  агента по invite-токену, устанавливает / обновляет приватные навыки команды
  Дмитрия, а также управляет оценками (E7), комментариями (E7), тикетами тех-
  поддержки (E8), коллекциями (E10) и event-tracking daemon (E23).
  Используй когда пользователь говорит: «поставь skills-hub», «залогинься
  в skills-hub», «обнови мои навыки hub», «skills-hub install <slug>», «что
  есть в каталоге skills-hub», «оцени skill X», «оставь коммент к skill»,
  «отправь тикет в поддержку», «покажи коллекции», «включи телеметрию
  skills-hub». EN triggers: install skills-hub, login to skills hub, update
  hub skills, list hub skills, rate a hub skill, comment on a hub skill,
  open a support ticket, list hub collections, start skills-hub daemon.
  RU triggers: поставь skills-hub, залогинь меня в hub, обнови навыки,
  список навыков hub, оцени навык, прокомментируй навык, тикет в поддержку,
  список коллекций hub, запусти телеметрию hub.
---

# Skills Hub bootstrap-skill

Точка входа AI-агента (Claude Code / Codex) в систему Skills Hub. После
установки агент умеет:

1. **Поставить CLI** — `python ./scripts/install.py` (ставит из исходников
   монорепо через pipx/pip; см. «Установка CLI» ниже).
2. **Авторизовать** клиента через invite-токен ИЛИ через email + password.
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
9. **Запускать event-tracking daemon** (E23) — фон-агент шлёт `skill.install`,
   `skill.update`, `skill.run` в `/events` (opt-in).
10. **Сообщать о багах** через `skills-hub report` (исторический shortcut
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
| `skills-hub passwd`         | Сменить (или установить) пароль                  |
| `skills-hub config`         | Output format, auto-update, default install scope |
| `skills-hub web`            | Открыть Web UI с автоматической авторизацией     |

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

### Оценки и комментарии — E7 (`skill.rate`, `comment.post`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub rate <id-или-slug> <1-5>`                  | Поставить / обновить свою оценку 1..5     |
| `skills-hub rating-summary <id-или-slug>`              | Средний балл + распределение (если есть право `skill.read`) |
| `skills-hub comment <id-или-slug> "<body>" [--screenshot path] [--parent <cmt_id>]` | Запостить коммент / ответ |
| `skills-hub comments <id-или-slug> [--limit N] [--cursor X]` | Список комментариев skill'а          |
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
| `skills-hub ticket status <tkt_id> <open\|in_progress\|resolved\|closed\|reopened>` | Сменить статус (assignee / hub-admin) |

> **Планируется** (пока НЕ реализовано): `ticket assign <tkt_id> <user_id>`
> (назначение ответственного вручную) и фильтры `tickets list --mine /
> --assigned-to-me`. Сейчас scope листинга определяется backend'ом по роли,
> а assignee выбирается автоматически.

`skills-hub report` (legacy) → внутри транслируется в `ticket create
--skill <id-или-slug> --kind bug`.

### Коллекции — E10 (`skill.read`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub collections list [--type static\|dynamic] [--company C] [--owner O] [--no-global]` | Список коллекций (static + dynamic) |
| `skills-hub collection show <id-или-slug>`             | Детали + развёрнутый список skills        |
| `skills-hub collection install <id-или-slug>`          | Поставить все skills из коллекции одной командой (главный онбординг-кейс) |

> Создание/редактирование коллекций (`create` / `add-skill` / `add-tag` /
> `delete`) из CLI **не реализовано** — это делается в Web UI. CLI работает с
> коллекциями в режиме read-only + `collection install`.

### Event tracking + daemon — E23 (`events.send`)

| Команда                                      | Назначение                                  |
| -------------------------------------------- | ------------------------------------------- |
| `skills-hub event track <type> [--resource-type T] [--resource-id ID] [--payload JSON] [--metadata JSON]` | Положить произвольный event в локальную очередь |
| `skills-hub event queue [--show] [--clear]`  | Инспектировать (или очистить) локальную очередь |
| `skills-hub event flush`                     | Синхронно отправить накопленный батч (один цикл sender'а) |
| `skills-hub daemon run [--interval N]`       | Foreground-цикл (для systemd / launchd / schtasks) |
| `skills-hub daemon start [--interval N]`     | Запустить фон-процесс (collector + sender)  |
| `skills-hub daemon stop`                     | Остановить (SIGTERM по PID-файлу)           |
| `skills-hub daemon status`                   | PID / последний цикл / queue size           |
| `skills-hub daemon install [--platform ...]` | Поставить как autostart (launchd / systemd user / Task Scheduler) |
| `skills-hub daemon uninstall [--platform ...]` | Убрать autostart-юнит                      |

Подкоманды — sub-app `event` (в единственном числе): `event track / queue /
flush`. Daemon кладёт events в `~/.skills-hub/events.queue.json` и шлёт батчами
на `POST /events`. Без daemon'а CLI всё равно регистрирует события синхронно
для команд `install`/`update`/`uninstall` (silent fail — телеметрия никогда не
ломает основную команду). Реальная отправка на backend требует права
`events.send`.

### Публикация (только `skill.publish`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub publish <id-или-slug> --tag v0.1.0 [--dry-run]` | Опубликовать новую версию из локальной папки (slug при создании задаёт hub-admin) |

### Админка (только hub-admin / company-admin)

| Команда                                       | Назначение                                       |
| --------------------------------------------- | ------------------------------------------------ |
| `skills-hub admin sync-skill <id-или-slug>`   | Подтянуть новые GitLab tags                      |
| `skills-hub admin company-create <slug> ...`  | Создать новую компанию + invite owner'у (slug компании задаёт hub-admin) |
| `skills-hub admin invite --company-id ... --role-id ...` | Выдать invite member'у                |

`skills-hub --help` после login показывает только те команды, на которые у
пользователя есть permission в JWT — это управляется backend'ом в момент
выдачи токенов.

## Алгоритм для AI-агента

1. **Первый запуск в сессии** — `skills-hub status`. Если `logged_in=false` →
   попросить у пользователя invite-token или email+password (через
   `skills-hub login`).
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
   есть», сначала `skills-hub collections list` (списки тематически
   сгруппированы), а уже потом `skills-hub list` (плоский). Для быстрого
   старта новому клиенту — `skills-hub collection install <id-или-slug>`
   ставит сразу всю подборку.

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
