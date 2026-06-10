# Slash-commands skills-hub (для Claude Code)

Этот каталог содержит markdown-обёртки для slash-commands. Если у тебя
Claude Code новее `2025-03` — положи эти файлы в
`~/.claude/commands/` (для глобальных) или
`<project>/.claude/commands/` (для проектных), и они станут доступны
как `/skills-hub-rate`, `/skills-hub-ticket`, `/skills-hub-issue` и т.д.

Под капотом каждая обёртка просто вызывает `skills-hub <subcommand>` с
правильно проброшенными аргументами и форматирует вывод для агента.

## Список

| Команда                        | Что делает                                       |
| ------------------------------ | ------------------------------------------------ |
| `/skills-hub-status`           | `skills-hub status` — что установлено + login    |
| `/skills-hub-install <id-или-slug>` | `skills-hub install <id-или-slug>` (+ автономно `--path` / `--from-git`) |
| `/skills-hub-update`           | `skills-hub update --all`                        |
| `/skills-hub-enable <id-или-slug>` | `skills-hub enable <id-или-slug>` — включить навык в набор проекта |
| `/skills-hub-sync`             | `skills-hub sync` — привести проект к `.skills-hub/skills.toml` |
| `/skills-hub-migrate`          | `skills-hub migrate` — перевести copy-установки в стор+ссылки |
| `/skills-hub-rate <id-или-slug> <N>` | `skills-hub rate <id-или-slug> <N>` (1..5) |
| `/skills-hub-comment <id-или-slug>` | `skills-hub comment <id-или-slug>` (тело берётся из ввода) |
| `/skills-hub-ticket <subj>`    | Создать тикет тех-поддержки                      |
| `/skills-hub-issue <descr>`    | Legacy alias: `report --kind bug`                |
| `/skills-hub-collections`      | `skills-hub collections list` — список (+ подсказка `collection install`) |
| `/skills-hub-collections-local` | `skills-hub collection *-local` — локальные коллекции (оффлайн, без хаба) |
| `/skills-hub-onboard`          | `skills-hub onboard` — детект стека проекта → подбор навыков → `--yes` |
| `/skills-hub-company`          | `skills-hub company *` — создать компанию, invite-links, granted-каталог |
| `/skills-hub-member`           | `skills-hub member *` / `members` / `roles` — участники компании |
| `/skills-hub-self-update`      | Перепоставить CLI из исходников (pipx/pip из `client/`) |

## Установка

```bash
# Скопировать обёртки в Claude Code global commands
mkdir -p ~/.claude/commands
cp ~/.claude/skills/skills-hub/commands/*.md ~/.claude/commands/
# или вручную выбрать те, что нужны
```

В Codex путь — `~/.codex/commands/`.
