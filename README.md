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

CLI **никогда не шлёт события синхронно** в основной команде: он только
**кладёт их в локальную очередь** `~/.skills-hub/events.queue.json` (silent —
сбой записи никогда не ломает команду). Отдельный **daemon** раз в минуту берёт
batch и POST'ит на `/events` бэкенда (E6). Для отладки можно протолкнуть очередь
вручную одним циклом: `skills-hub event flush`.

### Модель событий жизненного цикла (канон)

Материализация-в-стор и включение-в-проект — РАЗНЫЕ события (две независимые
оси аналитики):

| event_type | Когда |
| --- | --- |
| `skill.install` | навык впервые материализован в центральный стор |
| `skill.update` | контент навыка в сторе обновлён |
| `skill.enable` | создана **project-ссылка** (навык включён в проект) |
| `skill.disable` | project-ссылка снята (стор цел) |
| `skill.uninstall` | навык удалён из стора (`remove --purge` / global remove) |

`install <slug> --scope project` шлёт **два** события: `skill.install` (за
материализацию, если навык новый в сторе) + `skill.enable` (за project-линк).
`enable` уже материализованного навыка → только `skill.enable`. `sync` (массовый
re-link), `collection install --local`, `onboard --yes` → `skill.enable` на
каждый слинкованный навык. `disable` и `remove --scope project` (без `--purge`)
→ `skill.disable`. `skill.run` (чтение навыка агентом) — отложен.

Каждое `skill.install` / `skill.enable` / `skill.update` несёт в payload две оси:
- **`scope`** — `global` | `project` (куда направлен линк);
- **`source`** — `hub` | `local-path` | `git-url` (откуда навык: бэк-хаб /
  локальная папка `--path` / произвольный git `--from-git`; резерв `other-hub`).
  В re-link точках source читается из `_skill_meta.json` навыка в сторе.

> Раньше материализация и включение-в-проект оба слались как `skill.install`,
> а `disable`/`remove --project` — как `skill.uninstall`; теперь они разведены.

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

**Autorestart на всех платформах.** Unit-файлы поднимают демон заново при сбое
и при загрузке системы: macOS — `KeepAlive`; linux — `Restart=on-failure`;
Windows Task Scheduler — `<RestartOnFailure>` (интервал 1 мин, до 3 попыток) +
второй триггер `<BootTrigger>` (старт при загрузке, не только при логине).

**`daemon status`** при `alive=false` И непустой очереди выдаёт явное
предупреждение и машинно-читаемое поле (`warning` / `stalled_events`): «демон не
запущен, N событий не отправлены: skills-hub daemon start».

**Anonymous-fallback отправки.** `POST /events` принимает события анонимно. Если
токена нет или он протух, `event flush` и daemon шлют batch **без `Authorization`
(anonymous)**, а не падают `SESSION_EXPIRED` — протухшая сессия не должна глушить
телеметрию автономного CLI.

## Cross-agent

CLI автодетектит установленного агента (Claude Code / Codex) по наличию
`~/.claude` или `~/.codex` и кладёт навыки в правильный каталог. Можно
форсить:

```bash
skills-hub install bitrix24 --agent codex
```

## Что нового в v0.3 (P0 автономность + P1 паритет с Web)

- **P0 — автономный install без хаба**: `install --path ./skill` и
  `install --from-git <url> [--ref <tag>]` кладут навык в стор без логина и
  backend'а. Lifecycle локального стора (`enable` / `disable` / `remove` /
  `sync` / `migrate` / `store *`) теперь **always-on** — работает без login.
- **P1 — самостоятельный онбординг аккаунта**: `register` (без инвайта, юзер
  без компании) и `join <ссылка-или-токен>` (вступление в компанию по
  переиспользуемой ссылке `…/join/<token>`); обе работают до login и сразу
  сохраняют сессию. `join` у залогиненного вступает текущим аккаунтом и не
  меняет активную компанию (дальше `company switch`).
- **P1 — компании из CLI**: sub-app `company` — `list / show / create /
  edit / switch` (switch перевыпускает токены), `invite-links
  list / create / revoke` (create печатает готовый join-URL; токен виден один
  раз), granted-каталог `catalog list / grant / revoke` (`--collection` для
  коллекций; slug резолвится автоматически).
- **P1 — участники**: `members`, `roles`, `member invite / remove /
  change-role / lock / unlock / reset-password` (одноразовый пароль
  показывается один раз). Гейты зеркалят backend-права (`user.invite`,
  `role.manage`, …).
- **P1 — локальные коллекции**: `collection create-local / add-local /
  remove-local / list-local / install-local / delete-local` — личные наборы
  в `~/.skills-hub/collections.toml`, полностью оффлайн; `install-local`
  линкует из стора и докачивает недостающее из хаба при логине.
- **P1 — онбординг проекта**: `onboard [--yes]` — детект стека
  (python / nodejs / nextjs / react / docker / terraform / go / rust /
  claude-code) → подбор навыков из стора + хаба → `--yes` включает в проект.

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
  `daemon run/start/stop/status/install/uninstall`. Кладёт в локальную очередь
  (async, daemon отправляет batch'ем) `skill.install` / `skill.update` /
  `skill.enable` / `skill.disable` / `skill.uninstall` (+ `scope` и `source` в
  payload) → `/events` (E6 backend). Daemon autorestart на всех платформах;
  отправка с anonymous-fallback при протухшем токене.
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
