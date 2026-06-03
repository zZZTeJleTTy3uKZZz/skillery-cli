---
description: Включить навык в наборе текущего проекта (стор + ссылка + манифест)
argument-hint: <id-или-slug>
allowed-tools: Bash(skills-hub:*)
---

Включи навык `$ARGUMENTS` в набор текущего проекта:

```bash
skills-hub --json enable $ARGUMENTS
```

Команда материализует навык в центральный стор (если ещё не там), создаёт
junction/symlink в `./.claude/skills/<slug>` и записывает навык в
`.skills-hub/skills.toml`. Распарси JSON: поле `event` должно быть `enabled`,
в `skills[].linked` — `true` (ссылка) или `false` (copy-fallback). Сообщи
пользователю путь и тип монтирования.
