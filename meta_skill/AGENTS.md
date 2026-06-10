# skills-hub meta-skill — алгоритм для AI-агента

Документ детально описывает шаги, которые AI-агент (Claude Code / Codex)
должен выполнять при работе с Skills Hub. SKILL.md — описание для
триггера/каталога; здесь — раскрытие шагов.

## Глоссарий

- **CLI** — команда `skills-hub`, ставится один раз через `scripts/install.py`
  (из исходников монорепо: pipx/pip install `client/`). Пакета на PyPI/npm
  пока НЕТ — `pip install skills-hub-cli` не сработает.
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
│ not installed   │ ─ python scripts/install.py ──▶ ┌─────────────────┐
└─────────────────┘   (pipx/pip из client/)          │ installed       │
                                                      │ logged out      │
                                                      └────────┬────────┘
                                                               │
            skills-hub login <invite-token>                    │
            (первый раз)                                       │
                                                               │
            skills-hub login --email --password                │
            (повторно)                                         │
                                                               ▼
                                                      ┌─────────────────┐
                                                      │ logged in       │
                                                      │ permissions: [] │
                                                      └────────┬────────┘
                                                               │
                                                               ▼
                                           list / install / rate / comment /
                                           ticket / collections / events

Примечание: в состоянии «logged out» работает не только login. Always-on
зона: `register` / `join` (самостоятельный онбординг аккаунта),
`install --path` / `install --from-git` (автономно, без хаба),
`enable` / `disable` / `remove` / `sync` / `migrate` / `store *`
(lifecycle локального стора), `collection *-local` (локальные коллекции),
`onboard` (по локальному стору; hub-поиск подключится после login).
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

### Нет аккаунта вовсе — register / join (P1, always-on)

Если у пользователя нет ни инвайта, ни пароля — он может зарегистрироваться
сам, ДО login (см. сценарий «Первый запуск без аккаунта» ниже):

```bash
# Без инвайта (юзер без компании, доступ к публичным навыкам):
skills-hub --json register --email <user@x.io> --password '<pw>' [--name "Имя"]

# По переиспользуемой пригласительной ссылке компании (URL или голый токен):
skills-hub --json join 'https://hub.example.com/join/<token>' \
    --email <user@x.io> --password '<pw>' [--name "Имя"]
```

Правила: пароль ≥ 8 символов; display-name по умолчанию — часть email до
`@` (`--name` переопределяет); в JSON-режиме `--email`/`--password`
обязательны флагами. Обе команды сохраняют сессию (токены в keyring) — отдельный
`login` после них не нужен.

## Шаг 2 — установить нужный skill

### Из хаба (по id-или-slug)

```bash
skills-hub --json list                          # доступные
skills-hub --json install <id-или-slug>         # scope по дефолту (см. ниже)
skills-hub --json install <id-или-slug> --scope global
skills-hub --json install <id-или-slug> --scope project --project /path/to/proj
```

Дефолтный scope берётся из `cfg.default_install_scope` (по дефолту `project`).

После install в папке `<id-или-slug>/_skill_meta.json` лежит полный manifest +
commit_sha + scope + `skill_id` (identity для slug-less skill).

### Автономно — без хаба (из папки / git)

`install` умеет ставить навык в стор **без backend и без логина**:

```bash
# Из локальной папки
skills-hub --json install --path ./my-skill
skills-hub --json install --path /abs/path/to/skill --scope global

# Из git-репозитория (--ref фиксирует тег/ветку/коммит; по умолчанию — default-ветка)
skills-hub --json install --from-git https://github.com/acme/some-skill
skills-hub --json install --from-git <url> --ref v1.0.0
```

Это позволяет положить **любой** навык в центральный стор и подключить его в
проект (через ссылку), даже если навыка нет в каталоге Skills Hub. Используй,
когда у пользователя есть готовая папка навыка или git-ссылка, а тянуть его из
хаба не нужно/нельзя.

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
skills-hub --json rating-summary <slug>      # средний + распределение
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

# Список тикетов в твоём scope (RBAC определяет видимость на backend)
skills-hub --json tickets list
skills-hub --json tickets list --status open          # фильтр по статусу
skills-hub --json tickets list --skill bitrix24 --kind bug --page 1 --page-size 50

# Один тикет + thread
skills-hub --json ticket show tkt_xyz789

# Ответить
skills-hub ticket reply tkt_xyz789 "Попробуйте включить debug в .env"

# Сменить статус (assignee / hub-admin)
skills-hub ticket status tkt_xyz789 in_progress
skills-hub ticket status tkt_xyz789 done
```

Команда листинга — `tickets list` (под-app `tickets`). Видимость задаётся
backend'ом по роли: hub-admin → все, company-admin → company, skill-creator →
свои назначения, user → свои создания. Клиентских фильтров `--mine` /
`--assigned-to-me` **нет** (это roadmap) — scope определяется сервером.
Назначить тикет вручную (`ticket assign`) тоже пока нельзя.

Auto-assignee:
1. Если `--skill` указан и у skill'а есть creator → creator.
2. Иначе → любой company-admin твоей компании.
3. Иначе → null (висит в общей очереди для hub-admin).

## Шаг 7 — коллекции (E10)

Коллекция = кураторская подборка skills (Spotify playlist style):
- **static** — фиксированный список `[skill_slug, ...]`.
- **dynamic** — формируется по тегам (любой skill с `tag in [...]` входит).

```bash
skills-hub --json collections list                  # все, к которым есть доступ
skills-hub --json collections list --type dynamic    # фильтр по типу
skills-hub --json collections list --company <id>    # фильтр по компании
skills-hub --json collection show <slug>             # детали + развёрнутый список skills
skills-hub --json collection install <slug>          # массовый install всех skills
```

Полезно при onboarding нового клиента: `collection install bitrix-starter`
ставит сразу `bitrix24` + `bitrix-1c-partners-cli` + `bitrix24-stats`.

Создание/редактирование **серверных** коллекций из CLI не поддержано —
только Web UI.

### Локальные коллекции (P1) — оффлайн, без хаба и логина

Личные наборы слагов в `<config_dir>/collections.toml` (по умолчанию
`~/.skills-hub/collections.toml`; уважает `SKILLS_HUB_CONFIG_DIR` и
`--profile`). Web UI их не видит, сеть не нужна:

```bash
skills-hub collection create-local my-stack --title "Мой стек"
skills-hub collection add-local my-stack bitrix24       # слаг или числовой id
skills-hub collection add-local my-stack wb-api         # нет в сторе → warning, но добавится
skills-hub --json collection list-local                 # имя / размер / чего нет в сторе
skills-hub --json collection install-local my-stack     # установить весь набор
skills-hub collection remove-local my-stack wb-api      # убрать из коллекции (диск цел)
skills-hub collection delete-local my-stack             # удалить коллекцию (навыки на диске целы)
```

`install-local` для каждого слага: есть в сторе → линк в scope (как
`enable`, БЕЗ сети); нет в сторе и залогинен → докачка из хаба
(`--channel`, default `published`); нет и НЕ залогинен → skip с подсказкой —
команда не падает. JSON-итог: `{installed, linked, skipped}` (+`hint`, если
пропуски из-за отсутствия логина). Флаги как у `install`: `--scope
global|project`, `--project P`, `--force`, `--agent A`.

Используй локальные коллекции, когда пользователь хочет повторяемый личный
набор навыков для новых машин/проектов без публикации коллекции в хаб.

## Шаг 8 — event tracking + daemon (E23)

Скилл шлёт events в `/events` (E6) — backend хранит в Postgres
(будущий ClickHouse). Sub-app называется `event` (в единственном числе).

```bash
# Положить произвольный event в очередь
skills-hub event track skill.run --resource-type skill \
  --resource-id <id-или-slug> --payload '{"duration_ms": 1234}'

# Инспектировать / очистить локальную очередь
skills-hub --json event queue --show
skills-hub event queue --clear

# Принудительный flush (синхронно, один цикл sender'а)
skills-hub --json event flush

# Daemon как long-running процесс
skills-hub daemon run            # foreground (для systemd/launchd/schtasks)
skills-hub daemon start          # detached background
skills-hub daemon status
skills-hub daemon stop

# Autostart на ОС
skills-hub daemon install     # пишет launchd plist / systemd user unit / Task Scheduler XML
skills-hub daemon uninstall   # убирает autostart-юнит
```

Команды управления телеметрией opt-in/opt-out **нет** — трекинг install /
update / uninstall происходит автоматически (silent fail, никогда не ломает
команду). Реальная отправка batch'а на backend гейтится правом `events.send`.

Что трекается автоматически:
- `skill.install` — slug, version, scope.
- `skill.update` — slug, from_version, to_version.
- `skill.uninstall` — slug.
- (Опционально) `skill.run` — если skill сам вызвал `skills-hub event
  track skill.run --resource-type skill --resource-id <id-или-slug>`.

Events queue — JSON-файл `~/.skills-hub/events.queue.json`. Daemon
батчит и шлёт раз в 60 секунд (configurable через `--interval`). Если daemon не
запущен — CLI отправляет sync прямо в команде (`install` / `update` /
`uninstall`), чтобы не терять данные.

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

## Сценарии P1 — аккаунт, компания, проект

Три типовых конвейера поверх шагов выше. Везде используй `--json` и парсь
поле `event`.

### Сценарий 1 — первый запуск без аккаунта

```bash
skills-hub --json status                       # logged_in=false → аккаунта/сессии нет
```

**Есть пригласительная ссылка компании** (`…/join/<token>` или голый токен):

```bash
skills-hub --json join 'https://hub.example.com/join/<token>' \
    --email user@x.io --password '<pw ≥8>' --name "Имя"
# event=joined, method=register_with_link → юзер + membership + сессия
```

**Ссылки нет** — регистрация без компании (доступ к публичным навыкам):

```bash
skills-hub --json register --email user@x.io --password '<pw ≥8>'
# event=registered → сессия сохранена; в компанию можно вступить позже: join <ссылка>
```

Нюансы:

- Пользователь **уже залогинен** и даёт ссылку → `join <ссылка>` вступает
  ТЕКУЩИМ аккаунтом (`--email`/`--password` игнорируются). Нужен второй
  аккаунт → сначала `skills-hub logout`.
- `join` у залогиненного **не меняет активную компанию** в токене. Чтобы
  работать в новой компании: `skills-hub company switch <company_id>`
  (перевыпустит токены; новая пара сохранится автоматически, набор команд
  в `--help` может измениться).
- Display-name по умолчанию — часть email до `@`; пароль ≥ 8 символов.

### Сценарий 2 — корпоративный онбординг (компания → ссылки → люди → каталог)

```bash
# 1. Создать компанию + invite owner'у (право hub.company_create;
#    --slug требует hub.slug_manage — без него slug-less компания)
skills-hub --json company create --name "Acme" \
    --owner-email owner@acme.io --owner-name "Owner"
# → owner_invite_token + owner_invite_url — передай владельцу (вход через login <token>)

# 2. Владелец: переиспользуемая join-ссылка для всей команды
#    (право company.manage | role.manage; kind=manager — только владелец)
skills-hub --json company invite-links create \
    --kind member --max-uses 50 --expires-in-days 30
# → join_url ГОТОВЫЙ (<web-ui>/join/<token>) — токен показывается ОДИН раз,
#   сразу передай команде; список/отзыв: invite-links list / revoke <link_id>

# 3. Адресный инвайт конкретного участника (право user.invite)
skills-hub --json roles                                  # выбрать role-id
skills-hub --json member invite --email dev@acme.io --role-id <role_id>
# → invite_token + invite_url; приглашённый сразу виден в `members` (invited)

# 4. Выдать компании навыки / коллекции (право catalog.manage)
skills-hub --json company catalog grant bitrix24          # ref = slug или id
skills-hub --json company catalog grant starter --collection
skills-hub --json company catalog list                    # навыки + коллекции + effective
```

Дальше по жизни: `members --q <строка>` (поиск), `member change-role
<user_id> <role_id>` (право role.manage), `member lock/unlock <user_id>`
(user.lock), `member remove <user_id>` (user.remove), `member
reset-password <user_id>` (company.manage) — одноразовый пароль показывается
**ОДИН раз**; в `--json` он приходит в stdout (`temp_password`) — передай
пользователю и **не логируй**. `--company <id>` везде опционален (default —
активная компания из JWT).

### Сценарий 3 — новый проект (onboard → подтверждение → enable)

```bash
cd /path/to/project
skills-hub --json onboard            # детект стека → {signals, suggestions}
```

1. Покажи пользователю таблицу предложений: `slug`, `source`
   (local | hub | both), `signals` (чем заматчился), пометка `already`
   (уже включён в проект). Сигналы детектятся по маркер-файлам: python /
   nodejs (+nextjs, react) / docker / terraform / go / rust / claude-code.
2. Спроси подтверждение. Согласен на всё:

```bash
skills-hub --json onboard --yes      # включить каждый не-already кандидат
# applied: {linked (из стора), installed (докачаны из хаба), already, skipped}
```

3. Согласен точечно — включай выбранные навыки по одному:

```bash
skills-hub --json enable <slug>
```

Нюансы: без логина onboard работает только по локальному стору (hub-кандидаты
и докачка появятся после login — навыки не из стора попадут в `skipped` с
`reason=not_logged_in`); `--limit N` — сколько кандидатов тянуть с хаба на
сигнал (капится 20); повторный запуск идемпотентен (`already`).

## RBAC permissions, которые могут понадобиться

| Permission              | Что разрешает                                       |
| ----------------------- | --------------------------------------------------- |
| `skill.read`            | `list`, `show`, `comments`, `contributors`, `rating-summary`, `collections list` / `collection show` |
| `skill.install`         | `install` из хаба, `update`, `collection install`   |
| `skill.rate`            | `rate`, `rating-summary`                            |
| `comment.post`          | `comment` (создать / ответить)                      |
| `comment.edit_own`      | `comment-edit`                                      |
| `comment.delete_own`    | `comment-delete`                                    |
| `skill.publish`         | `publish` (creator)                                 |
| `skill.report_issue`    | legacy `report`                                     |
| `ticket.create`         | `ticket create`, `ticket reply`                     |
| `ticket.read`           | `tickets list`, `ticket show`                       |
| `ticket.update_status`  | `ticket status`                                     |
| `events.send`           | реальная отправка events на backend (daemon/flush)  |
| `user.invite`           | `member invite`                                     |
| `user.remove`           | `member remove`                                     |
| `role.manage`           | `member change-role`; также открывает `company invite-links *` |
| `user.lock`             | `member lock` / `member unlock`                     |
| `company.manage`        | `company edit`, `member reset-password`; также открывает `company invite-links *` |
| `catalog.manage`        | `company catalog list / grant / revoke`             |
| `catalog.view_all`      | `company catalog list` (read-only)                  |
| `hub.admin`             | `company list`, `admin sync-skill` (+hub-admin bypass: проходит все permission-гейты CLI) |
| `hub.company_create`    | `company create`, legacy `admin company-create`     |
| `invite.manage`         | `admin invite`                                      |

> **Always-on** (без логина и без прав): `register`, `join`,
> `install --path` / `--from-git`, `enable` / `disable` / `remove` / `sync` /
> `migrate` / `store *`, `collection *-local`, `onboard`. **Любой
> залогиненный** (без отдельного права): `members`, `roles`, `company show`,
> `company switch`, `passwd` — backend сам сужает выдачу tenant-изоляцией
> (member без admin-прав в `members` видит только себя). Коллекции и
> rating-summary видны при `skill.read` (отдельного `collection.*` permission
> нет). Команды `ticket assign` и серверные `collection create/delete/add-*`
> из CLI **не реализованы** (локальные `collection *-local` — реализованы).

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
