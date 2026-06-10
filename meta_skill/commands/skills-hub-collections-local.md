---
description: Локальные коллекции навыков — личные наборы, оффлайн, без хаба и логина.
argument-hint: create-local|add-local|remove-local|list-local|install-local|delete-local [...]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-collections-local

Локальные коллекции — личные наборы слагов в
`~/.skills-hub/collections.toml`. Работают полностью оффлайн (без логина и
хаба), Web UI их не видит. Для серверных кураторских подборок —
`/skills-hub-collections`.

```bash
skills-hub collection create-local <name> [--title "Заголовок"]
skills-hub collection add-local <name> <skill-slug>      # слаг или числовой id
skills-hub collection remove-local <name> <skill-slug>
skills-hub --json collection list-local
skills-hub --json collection install-local <name> [--scope global|project] [--force]
skills-hub collection delete-local <name>
```

Подсказки:

- `add-local` принимает слаг, которого ещё нет в сторе (будет warning) —
  `install-local` докачает его из хаба, когда пользователь залогинен.
- `list-local` показывает, каких слагов нет в сторе (колонка «нет в сторе»).
- `install-local`: есть в сторе → линк (без сети); нет + залогинен → докачка
  из хаба; нет + не залогинен → `skipped` с подсказкой (команда не падает).
  Распарси JSON `{installed, linked, skipped}` и сообщи итог; если есть
  `skipped` из-за логина — предложи `skills-hub login`.
- `delete-local` / `remove-local` не трогают установленные навыки на диске.

Используй, когда пользователь хочет повторяемый личный набор навыков
(«мой стек») для новых машин и проектов без публикации коллекции в хаб.
