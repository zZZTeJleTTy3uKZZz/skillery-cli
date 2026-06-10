# skills-hub-cli

CLI клиент для Skills Hub — приватного маркетплейса AI-skills.

## Установка

Пакет в PyPI/npm пока **не опубликован** (планируется). Установка — из
исходников этого репозитория (`client/`):

```bash
# Изолированно через pipx (рекомендуется), запустив из корня client/:
pipx install .
# editable-режим в текущий venv:
pip install -e .
# либо через bootstrap-скрипт meta-skill (pipx/pip автоматически):
python meta_skill/scripts/install.py
```

> `pip install skills-hub-cli` (из PyPI) пока НЕ работает — дистрибутив только
> планируется.

## Quickstart

```bash
# Получить invite-токен от админа Hub'а (Telegram, email, ...).
skills-hub login <invite-token-or-url>
# или, если уже есть пароль:
skills-hub login --email me@x.io --password ...

# Посмотреть доступные навыки (RBAC фильтрует)
skills-hub list
skills-hub collections                   # кураторские подборки (E10)

# Навыки ставятся в центральный стор (~/.skills-hub/store) и линкуются junction/
# symlink. Дефолтный scope — project (текущая папка).
skills-hub install bitrix24                 # → стор + ссылка в ./.claude/skills + манифест
skills-hub install bitrix24 --scope global  # → стор + ссылка в ~/.claude/skills

# Автономно — без хаба, из локальной папки или git (логин не нужен)
skills-hub install --path ./my-skill
skills-hub install --from-git <url> --ref v1.0.0

# Динамический набор проекта
skills-hub enable wb-api          # включить в текущем проекте
skills-hub disable wb-api         # выключить (стор цел)
skills-hub sync                   # применить .skills-hub/skills.toml
skills-hub migrate --dry-run      # перенести старые копии в стор
skills-hub store list             # что в сторе

# Обновить установленные навыки
skills-hub update --all

# Оценки и комментарии (E7)
skills-hub rate bitrix24 5
skills-hub comment bitrix24 "Работает в облаке, в коробке падает"
skills-hub comments bitrix24

# Тикет тех-поддержки (E8)
skills-hub ticket create "OAuth ломается" --skill bitrix24 --kind bug --priority high
skills-hub tickets list --status open
skills-hub ticket show tkt_abc123

# Event tracking + daemon (E23)
skills-hub event queue --show      # что в локальной очереди
skills-hub daemon start            # background process (batch sender)
skills-hub daemon install          # autostart (launchd / systemd user / Task Scheduler)
```

## E23 — Ratings / комменты / тикеты / коллекции

После login дополнительно доступны (по permissions):

```bash
# Оценить скилл (1..5) — требует skill.rate
skills-hub rate bitrix24 5
skills-hub rating-summary bitrix24

# Прокомментировать (требует comment.post) — multipart upload скриншотов
skills-hub comment bitrix24 "крутой скилл" --screenshot ~/Pictures/proof.png
skills-hub comments bitrix24 --limit 25

# Контрибьюторы из git history
skills-hub contributors bitrix24 --refresh

# Тикеты в техподдержку (требует ticket.create / ticket.read)
skills-hub ticket create "OAuth flow ломается" --skill bitrix24 --kind bug
skills-hub tickets list --status open
skills-hub ticket show tkt_abc

# Коллекции (read-only из CLI; CRUD в Web UI)
skills-hub collections list --type dynamic
skills-hub collection show popular
```

## E23 — Event tracking + daemon

CLI пишет события (`skill.install` / `skill.update` / `skill.run` /
произвольные через `event track`) в локальную очередь
`~/.skills-hub/events.queue.json`. Отдельный daemon раз в минуту берёт
batch и POST'ит на `/events` бэкенда (E6).

```bash
# Ручное добавление event'а
skills-hub event track skill.run --resource-type skill \
  --resource-id my-skill --payload '{"duration_ms": 1234}'

# Inspect очереди
skills-hub event queue --show

# Синхронная отправка (один цикл sender'а — для отладки)
skills-hub event flush

# Daemon — long-running batch sender
skills-hub daemon start          # detached background
skills-hub daemon status         # alive + last_cycle + queue size
skills-hub daemon stop           # SIGTERM по PID-файлу

# Autostart unit-file (генерация без sudo; user сам активирует)
skills-hub daemon install        # автодетект macos / linux / windows
skills-hub daemon install --platform linux
```

`skills-hub daemon install` НЕ модифицирует system-wide settings — он
только пишет unit-файл в user-scope и печатает инструкцию (`launchctl
load`, `systemctl --user enable`, `schtasks /Create /XML`). Это
безопасно для CI / shared dev-окружений.

## Cross-agent

CLI автодетектит установленного агента (Claude Code / Codex) по наличию
`~/.claude` или `~/.codex` и кладёт навыки в правильный каталог. Можно
форсить:

```bash
skills-hub install bitrix24 --agent codex
```

## Что нового в v0.2 (E1..E26 marathon)

- **E7 — оценки и комментарии**: `rate`, `rating-summary`, `comment`,
  `comments`, `comment-edit`, `comment-delete`, `contributors`.
  Threaded replies + screenshots (multipart upload до 5 МБ файл).
- **E8 — тикеты тех-поддержки**: `ticket create / show / reply / status`,
  `tickets list`. Scope-aware listing (hub-admin → всё, company-admin
  → company, skill-creator → свои, user → свои). `ticket assign` — планируется.
- **E10 — коллекции**: `collections list`, `collection show`, `collection
  install` (массовая установка). Static + dynamic (по тегам).
- **E23 — event tracking + daemon**: `event track / queue / flush`,
  `daemon run/start/stop/status/install/uninstall`. Шлёт `skill.install`,
  `skill.update`, `skill.run` в `/events` (E6 backend).
- Stripe-style API conventions (`/skills`, `/support/tickets`,
  `/collections`, ID prefixes `rat_`, `cmt_`, `tkt_`, `tmsg_`, `col_`,
  `evt_`).
- Web UI handoff: `skills-hub web` открывает браузер уже залогиненным.

## Конфиг

`~/.skills-hub/config.toml`:

```toml
base_url   = "https://hub.example.com"
web_ui_url = "https://hub.example.com/app"
output_format = "text"           # text | json
auto_update = false              # true → тихо обновляет skills при следующей команде
auto_update_cooldown_min = 30
# store_dir = "~/.skills-hub/store"   # центральный стор (env: SKILLS_HUB_STORE_DIR)
default_install_scope = "project"      # дефолт сменён с global на project
# default_project_dir = "..."    # если scope=project, куда по дефолту
```

Токены (access + refresh JWT) хранятся в **OS keyring**:
- Windows — Credential Manager;
- macOS — Keychain;
- Linux — Secret Service (GNOME Keyring, KWallet, …).

## Profiles

`~/.skills-hub/profiles/staging.toml` + `--profile staging` — отдельный
backend / отдельные токены для другой среды.

## Связано

- [Skills Hub backend](../backend/) — серверная часть, OpenAPI на
  `<base_url>/docs`.
- [meta-skill bootstrap](./meta_skill/) — навык-обёртка, ставит CLI и
  регистрирует агента; содержит `SKILL.md`, `AGENTS.md`, `commands/` для
  Claude Code, `manifest-schema.md` для skill-creator'ов.
- [Web UI](../web/) — Next.js + shadcn, для просмотра каталога глазами;
  логин из CLI через `skills-hub web` (без повторного ввода пароля).
