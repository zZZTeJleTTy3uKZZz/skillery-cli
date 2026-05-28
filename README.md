# skills-hub-cli

CLI клиент для Skills Hub — приватного маркетплейса AI-skills.

## Установка

```bash
pip install skills-hub-cli
# или из локального исходника
pip install -e .
```

## Quickstart

```bash
# Получить invite-токен от админа Hub'а (Telegram, email, ...).
skills-hub login <invite-token-or-url>
# или, если уже есть пароль:
skills-hub login --email me@x.io --password ...

# Посмотреть доступные навыки (RBAC фильтрует)
skills-hub list
skills-hub collections                   # кураторские подборки (E10)

# Установить навык (по умолчанию в ~/.claude/skills/, можно в .codex/)
skills-hub install bitrix24
skills-hub install bitrix24 --scope project  # только в текущий cwd/.claude/skills/

# Обновить установленные навыки
skills-hub update --all

# Оценки и комментарии (E7)
skills-hub rate bitrix24 5
skills-hub comment bitrix24 "Работает в облаке, в коробке падает"
skills-hub comments bitrix24

# Тикет тех-поддержки (E8)
skills-hub ticket create "OAuth ломается" --skill bitrix24 --kind bug --priority high
skills-hub tickets --mine
skills-hub ticket show tkt_abc123

# Event tracking + daemon (E23, opt-in)
skills-hub events opt-in
skills-hub daemon start            # background process
skills-hub daemon install          # autostart (launchd / systemd user / Task Scheduler)
```

## Cross-agent

CLI автодетектит установленного агента (Claude Code / Codex) по наличию
`~/.claude` или `~/.codex` и кладёт навыки в правильный каталог. Можно
форсить:

```bash
skills-hub install bitrix24 --agent codex
```

## Что нового в v0.2 (E1..E26 marathon)

- **E7 — оценки и комментарии**: `rate`, `comment`, `comments`,
  `comment-edit`, `comment-delete`, `contributors`, `ratings`.
  Threaded replies + screenshots (multipart upload до 5 МБ файл).
- **E8 — тикеты тех-поддержки**: `ticket create / show / reply / status /
  assign`, `tickets`. Scope-aware listing (hub-admin → всё, company-admin
  → company, skill-creator → свои, user → свои).
- **E10 — коллекции**: `collections`, `collection show`, `collection
  install` (массовая установка). Static + dynamic (по тегам).
- **E23 — event tracking + daemon**: `events status/flush/opt-in/opt-out`,
  `daemon start/stop/status/install/uninstall`. Шлёт `skill.install`,
  `skill.update`, `skill.run` в `/events/ingest` (E6 backend).
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
default_install_scope = "global" # global | project
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
