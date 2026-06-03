# skills-hub meta-skill — алгоритм для AI-агента

Документ детально описывает шаги, которые AI-агент (Claude Code / Codex)
должен выполнять при работе с Skills Hub. SKILL.md — описание для
триггера/каталога; здесь — раскрытие шагов.

## Глоссарий

- **CLI** — команда `skills-hub`, ставится один раз через `scripts/install.py`.
- **Skill** — отдельный навык (например, `bitrix24`), живёт в собственном
  GitLab-репо, ставится в `~/.claude/skills/<id-или-slug>/` или
  `<project>/.claude/skills/<id-или-slug>/` (имя папки — slug, либо числовой
  id для slug-less skill, PK-миграция §3.E).
- **Hub** — backend (FastAPI), хранит metadata + RBAC + ratings/comments/
  tickets/collections/events.
- **Agent** — AI-coding-agent клиента (Claude Code, Codex). CLI авто-
  детектит по наличию `~/.claude` / `~/.codex`.

## Состояния и переходы

```
┌─────────────────┐
│ not installed   │ ── pip install skills-hub-cli ──▶ ┌─────────────────┐
└─────────────────┘                                    │ installed       │
                                                       │ logged out      │
                                                       └────────┬────────┘
                                                                │
            skills-hub login <invite-token>                     │
            (первый раз)                                        │
                                                                │
            skills-hub login --email --password                 │
            (повторно)                                          │
                                                                ▼
                                                       ┌─────────────────┐
                                                       │ logged in       │
                                                       │ permissions: [] │
                                                       └────────┬────────┘
                                                                │
                                                                ▼
                                            list / install / rate / comment /
                                            ticket / collections / events
```

## Шаг 0 — детектировать состояние

Всегда начинай с:

```bash
skills-hub --json status
```

Парсь JSON, смотри:
- `agent` — claude_code | codex.
- `user_email` — если null, нужно login.
- `installed_global` / `installed_project` — массивы установленных skills.

## Шаг 1 — login

### Invite-flow (первый вход)

Пользователь даёт invite-token (URL вида `https://hub.example.com/auth/invite/xxx`
или просто токен):

```bash
skills-hub login <token> --email <user@x.io> --name "<Имя>"
```

CLI вытащит slug из URL автоматически (regex), создаст user если новый,
сохранит access+refresh токены в OS keyring (Windows Credentials / macOS
Keychain / Linux Secret Service).

### Email + password (последующие)

```bash
skills-hub login --email <user@x.io> --password <pw>
# или интерактивно (без --password — спросит)
skills-hub login --email <user@x.io>
```

В JSON-режиме `--password` обязателен (нет TTY для prompt'а).

## Шаг 2 — установить нужный skill

```bash
skills-hub --json list                          # доступные
skills-hub --json install <id-или-slug>         # global по дефолту
skills-hub --json install <id-или-slug> --scope project --project /path/to/proj
```

Дефолтный scope берётся из `cfg.default_install_scope` (по дефолту `project`).

После install в папке `<id-или-slug>/_skill_meta.json` лежит полный manifest +
commit_sha + scope + `skill_id` (identity для slug-less skill).

## Динамическое управление набором навыков проекта

Набор активных в проекте навыков фиксируется в `<project>/.skills-hub/skills.toml`
(коммитится в git). Алгоритм для агента:

1. **Войти в проект** → `skills-hub status --project .` (что уже включено).
2. **Включить нужные навыки** → `skills-hub enable <slug>` (для каждого). Создаёт
   ссылку из стора в `<project>/.claude/skills/<slug>` и пишет манифест.
3. **Воспроизвести набор на другой машине / после clone** → `skills-hub sync`
   (создаёт ссылки по манифесту, докачивая отсутствующее в сторе).
4. **Выключить ненужное** → `skills-hub disable <slug>` (ссылка снята, стор цел).
5. **Перенести старые копии** → `skills-hub migrate --dry-run` затем без флага.

Один навык в сторе используется во многих проектах — `enable`/`disable` дёшевы
(операции со ссылками, без перекачки).

## Шаг 3 — обновления

```bash
skills-hub --json update --all       # global + project
skills-hub --json update <slug>      # один skill в auto-scope
```

Auto-update: если в config `auto_update=true`, CLI сам тихо обновит
установленные skills при следующей команде (с throttling
`auto_update_cooldown_min`).

```bash
skills-hub config --auto-update            # включить
skills-hub config --no-auto-update         # выключить
skills-hub config --auto-update-cooldown-min 30
```

## Шаг 4 — оценки (E7)

```bash
skills-hub --json rate <slug> 5              # 1..5
skills-hub --json ratings <slug>             # средний + распределение
```

Backend применяет уникальность (user_id, skill_id) — повторный `rate`
обновляет существующую оценку.

## Шаг 5 — комментарии (E7)

```bash
# Создать
skills-hub comment <slug> "Отличный skill, работает в Bitrix24 cloud"
skills-hub comment <slug> "Не работает в self-hosted Bitrix" \
    --screenshot ~/Pictures/error-trace.png

# Ответить
skills-hub comment <slug> "У нас тоже" --parent cmt_abc123

# Прочитать threaded дерево
skills-hub --json comments <slug>

# Редактировать (только свой)
skills-hub comment-edit cmt_abc123 "Новый текст"

# Удалить (свой или hub-admin)
skills-hub comment-delete cmt_abc123
```

Скриншоты — multipart upload в `/skills/<slug>/comments` (до 5 МБ файл).
Backend хранит в `/uploads/` (MVP) или S3.

## Шаг 6 — тикеты тех-поддержки (E8)

```bash
# Создать (assignee выберется сам)
skills-hub --json ticket create "OAuth не работает" \
    --skill bitrix24 \
    --kind bug \
    --priority high \
    --body "Получаю 401 при /auth/redirect. См. screenshot." \
    --screenshot ~/Desktop/trace.png

# Список своих
skills-hub --json tickets --mine

# Список назначенных мне (для skill-creator)
skills-hub --json tickets --assigned-to-me

# Открытые в моей компании (для company-admin)
skills-hub --json tickets --status open

# Один тикет + thread
skills-hub --json ticket show tkt_xyz789

# Ответить
skills-hub ticket reply tkt_xyz789 "Попробуйте включить debug в .env"

# Сменить статус (assignee / hub-admin)
skills-hub ticket status tkt_xyz789 in_progress
skills-hub ticket status tkt_xyz789 resolved
```

Auto-assignee:
1. Если `--skill` указан и у skill'а есть creator → creator.
2. Иначе → любой company-admin твоей компании.
3. Иначе → null (висит в общей очереди для hub-admin).

## Шаг 7 — коллекции (E10)

Коллекция = кураторская подборка skills (Spotify playlist style):
- **static** — фиксированный список `[skill_slug, ...]`.
- **dynamic** — формируется по тегам (любой skill с `tag in [...]` входит).

```bash
skills-hub --json collections                 # все, к которым есть доступ
skills-hub --json collections --mine          # созданные мной
skills-hub --json collection show <slug>      # детали + развёрнутый список skills
skills-hub --json collection install <slug>   # массовый install всех skills
```

Полезно при onboarding нового клиента: `collection install bitrix-starter`
ставит сразу `bitrix24` + `bitrix-1c-partners-cli` + `bitrix24-stats`.

## Шаг 8 — event tracking + daemon (E23)

Скилл шлёт events в `/events/ingest` (E6) — backend хранит в Postgres
(будущий ClickHouse).

```bash
# Включить opt-in (по дефолту off)
skills-hub events opt-in

# Статус
skills-hub --json events status
# {
#   "opt_in": true,
#   "queue_size": 12,
#   "last_flush_at": "2026-05-28T10:15:00Z",
#   "daemon_running": true,
#   "daemon_pid": 12345
# }

# Принудительный flush
skills-hub events flush

# Daemon как long-running процесс
skills-hub daemon start
skills-hub daemon status
skills-hub daemon stop

# Autostart на ОС
skills-hub daemon install     # пишет launchd plist / systemd user unit / Task Scheduler XML
skills-hub daemon uninstall   # убирает
```

Что трекается автоматически:
- `skill.install` — slug, version, scope.
- `skill.update` — slug, from_version, to_version.
- `skill.uninstall` — slug.
- (Опционально) `skill.run` — если skill сам вызвал `skills-hub events
  track skill.run --slug <slug>`.

Events queue — JSON-файл `~/.skills-hub/events.queue.json`. Daemon
батчит и шлёт раз в 60 секунд (configurable). Если daemon не запущен —
CLI отправляет sync прямо в команде (`install` / `update` / `uninstall`),
чтобы не терять данные.

## Шаг 9 — bug-report / feedback

Исторически — `skills-hub report <slug> --kind bug --title ... --description ...`.
Под капотом теперь — `ticket create --skill <slug> --kind <kind>
--body <description>`. Команда `report` оставлена как legacy alias.

## Шаг 10 — Web UI handoff

Если пользователь хочет посмотреть всё в браузере:

```bash
skills-hub web              # выписывает короткоживущий code и открывает браузер
skills-hub web --no-browser # только URL — для удалённых сессий
```

Backend выдаёт `code`, привязанный к JWT текущей CLI-сессии. Web UI
обменивает его на свою cookie-сессию через `/auth/exchange/redeem`. Не
надо отдельно логиниться в Web.

## RBAC permissions, которые могут понадобиться

| Permission              | Что разрешает                                       |
| ----------------------- | --------------------------------------------------- |
| `skill.read`            | `list`, `show`, `ratings`, `comments`, `contributors` |
| `skill.install`         | `install`, `update`                                 |
| `skill.rate`            | `rate`                                              |
| `skill.comment`         | `comment*` команды                                  |
| `skill.publish`         | `publish` (creator)                                 |
| `skill.report_issue`    | legacy `report`                                     |
| `support.create`        | `ticket create`                                     |
| `support.read`          | `tickets`, `ticket show`                            |
| `support.respond`       | `ticket reply` (на тикеты, где я assignee)          |
| `support.manage`        | `ticket status`, `ticket assign`                    |
| `collection.read`       | `collections`, `collection show`, `collection install` |
| `collection.write`      | `collection create / delete / add-*`                |
| `events.send`           | автоматическая отправка events                      |
| `hub.admin`             | `admin sync-skill`                                  |
| `hub.company_create`    | `admin company-create`                              |
| `invite.manage`         | `admin invite`                                      |

CLI скрывает в `--help` все команды, на которые нет permission, — это
управляется build_app() при запуске.

## Полезные advanced-кейсы

### Сменить backend / профиль

`~/.skills-hub/config.toml` — `base_url` + `web_ui_url`. Для нескольких
сред:

```bash
skills-hub --profile staging list
# использует ~/.skills-hub/profiles/staging.toml
```

### Set-tokens (без backend login)

`skills-hub set-tokens <email> --access <jwt> --refresh <token>` —
скрытая команда для случаев, когда токены приходят в обход CLI (Web UI
скопировал пользователю, например).

### JSON output для скриптов

`--json` / `-J` глобальный флаг или `cfg.output_format = "json"`. Все
эмиттеры идут через `output.emit_data()` — структура стабильна.

### Smoke install meta-skill самого себя (dogfood)

```bash
skills-hub publish skills-hub --tag v0.2.0 --path ./meta_skill --dry-run
```

Покажет, что попадёт в bundle (manifest + files). После того, как
meta-skill зальют как обычный skill — клиенты смогут `install skills-hub`
прямо из Hub'а, без копирования вручную.
