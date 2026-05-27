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
# Получить invite-токен от админа Hub'а (через Telegram, например).
skills-hub login <invite-token-or-url>

# Посмотреть доступные навыки
skills-hub list

# Установить навык в ~/.claude/skills/ (или ~/.codex/skills/)
skills-hub install bitrix24

# Обновить все установленные навыки (incremental)
skills-hub update --all

# Отправить bug-report
skills-hub report bitrix24 --kind bug --title "install падает" --description "..."
```

## Cross-agent

CLI автодетектит установленного агента (Claude Code / Codex) по наличию
`~/.claude` или `~/.codex` и кладёт навыки в правильный каталог. Можно
форсить:

```bash
skills-hub install bitrix24 --agent codex
```

## Конфиг

`~/.skills-hub/config.toml` — base_url, текущий слой. Токены — в OS
keyring (Windows Credentials, macOS Keychain, Linux Secret Service).

## Связано

- [Skills Hub backend](../backend/) — серверная часть.
- [meta-skill bootstrap](./meta_skill/) — навык-обёртка, ставит этот CLI
  и регистрирует агента.
