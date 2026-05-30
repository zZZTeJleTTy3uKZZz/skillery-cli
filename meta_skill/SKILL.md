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

1. **Поставить CLI** — `pip install skills-hub-cli` (или `python ./scripts/install.py`).
2. **Авторизовать** клиента через invite-токен ИЛИ через email + password.
3. **Перечислить** доступные навыки (RBAC) и **установить** нужные (global
   или per-project scope).
4. **Обновлять** установленные навыки (`update --all`, либо auto-update
   daemon-ом в фоне).
5. **Оценивать и комментировать** навыки (E7): 1–5 звёзд, threaded comments
   со скриншотами.
6. **Открывать тикеты тех-поддержки** (E8) — по навыку или общие.
7. **Просматривать коллекции** (E10) — кураторские подборки навыков
   (статические или динамические по тегам).
8. **Запускать event-tracking daemon** (E23) — фон-агент шлёт `skill.install`,
   `skill.update`, `skill.run` в `/events/ingest` (opt-in).
9. **Сообщать о багах** через `skills-hub report` (исторический shortcut
   к тикетам).

## Quickstart для агента

```bash
# 1. Один раз — установить CLI
python ./scripts/install.py            # или: pip install skills-hub-cli

# 2. Авторизоваться (одно из двух)
skills-hub login <invite-token-or-URL>          # первый вход — invite
skills-hub login --email me@x.io --password ... # последующие — пароль

# 3. Посмотреть, что доступно (RBAC фильтрует автоматически)
skills-hub list

# 4. Установить нужный skill (global = всем сессиям, project = только текущему cwd)
skills-hub install bitrix24                      # default scope из config
skills-hub install bitrix24 --scope project      # только в cwd/.claude/skills/

# 5. Проверить статус
skills-hub status                                # global + project + agent + login

# 6. Обновить все
skills-hub update --all                          # global + project одной командой
```

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
| `skills-hub install <id-или-slug> [--scope ...] [--force]` | Поставить (global или project)        |
| `skills-hub update [<id-или-slug>] [--all] [--scope ...]` | Обновить установленные                 |

### Оценки и комментарии — E7 (`skill.rate`, `skill.comment`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub rate <id-или-slug> <1-5>`                  | Поставить / обновить свою оценку 1..5     |
| `skills-hub ratings <id-или-slug>`                     | Средний балл + распределение (если есть право `skill.read`) |
| `skills-hub comment <id-или-slug> "<body>" [--screenshot path] [--parent <cmt_id>]` | Запостить коммент / ответ |
| `skills-hub comments <id-или-slug>`                    | Дерево комментариев skill'а               |
| `skills-hub comment-edit <cmt_id> "<new body>"`        | Редактировать свой коммент                |
| `skills-hub comment-delete <cmt_id>`                   | Soft-delete (автор или hub-admin)         |
| `skills-hub contributors <id-или-slug>`                | Авторы и количество коммитов (из git)     |

### Тикеты тех-поддержки — E8 (`support.create`, `support.read`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub ticket create "<subject>" [--skill <id-или-slug>] [--kind bug\|feature\|question\|other] [--priority low\|normal\|high\|urgent] [--body "..."] [--screenshot path]` | Завести тикет; assignee выбирается авто (creator skill'а → company-admin → none) |
| `skills-hub tickets [--status ...] [--mine] [--assigned-to-me]` | Список тикетов в твоём scope (RBAC: hub-admin → все; company-admin → company; skill-creator → свои назначения; user → свои создания) |
| `skills-hub ticket show <tkt_id>`                      | Тикет + thread сообщений                  |
| `skills-hub ticket reply <tkt_id> "<body>" [--screenshot path]` | Добавить ответ в thread             |
| `skills-hub ticket status <tkt_id> <open\|in_progress\|resolved\|closed\|reopened>` | Сменить статус (assignee / hub-admin) |
| `skills-hub ticket assign <tkt_id> <user_id>`          | Назначить ответственного (admin only)     |

`skills-hub report` (legacy) → внутри транслируется в `ticket create
--skill <id-или-slug> --kind bug`.

### Коллекции — E10 (`collection.read`)

| Команда                                                | Назначение                                |
| ------------------------------------------------------ | ----------------------------------------- |
| `skills-hub collections [--mine] [--global]`           | Список коллекций (static + dynamic)       |
| `skills-hub collection show <id-или-slug>`             | Детали + развёрнутый список skills        |
| `skills-hub collection install <id-или-slug>`          | Поставить все skills из коллекции одной командой |

С правом `collection.write` доступны также `collection create`, `collection
add-skill`, `collection add-tag`, `collection delete`.

### Event tracking + daemon — E23 (`events.send`, opt-in)

| Команда                              | Назначение                                  |
| ------------------------------------ | ------------------------------------------- |
| `skills-hub events status`           | Что в локальной очереди, когда последний flush |
| `skills-hub events flush`            | Один раз отправить накопленный батч         |
| `skills-hub events opt-in / opt-out` | Включить / выключить телеметрию             |
| `skills-hub daemon start`            | Запустить фон-процесс (collector + sender)  |
| `skills-hub daemon stop`             | Остановить                                  |
| `skills-hub daemon status`           | PID / uptime / queue size                   |
| `skills-hub daemon install`          | Поставить как autostart (launchd / systemd user / Task Scheduler) |
| `skills-hub daemon uninstall`        | Убрать autostart                            |

Daemon кладёт events в `~/.skills-hub/events.queue.json` и шлёт батчами на
`POST /events/ingest`. По умолчанию opt-in выключен — пользователь должен
явно согласиться. Без daemon'а CLI всё равно регистрирует события синхронно
для команд `install`/`update`/`uninstall`.

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
   который не установлен, выполни `skills-hub install <id-или-slug>`.
3. **Регистрация события** — при `install` / `update` / `uninstall` CLI сам
   отправит `skill.*` event. Дополнительно при ручном run рекомендуется
   `skills-hub events track skill.run --slug <id-или-slug>` (если daemon
   `opt_in=true`).
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
   есть», сначала `skills-hub collections` (списки тематически
   сгруппированы), а уже потом `skills-hub list` (плоский).

## Manifest skill.json — новые опциональные поля

В `_skill_meta.toml` или фронтматтере `SKILL.md` появились опциональные
ключи (backend читает их при `publish`):

```toml
# _skill_meta.toml
description = "..."
triggers = ["...", "..."]
tags = ["bitrix24", "crm"]

# Новые поля (опционально, default — наследуется от системного конфига):
rating_enabled = true            # разрешить пользователям ставить оценку
comments_enabled = true          # разрешить публичные комменты
support_tickets_enabled = true   # принимать тикеты через `skills-hub ticket create`

# Шаблоны интеграции с тикетами:
support_assignee_email = "creator@x.io"  # дефолтный assignee (если backend не знает creator'а)
support_kinds_allowed = ["bug", "feature", "question"]  # подмножество от полного списка
```

Эти поля не ломают совместимость: если их нет — поведение по умолчанию из
SystemConfig (E9).

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
