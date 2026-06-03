---
description: Привести набор навыков проекта в соответствие .skills-hub/skills.toml
argument-hint: "[--no-prune]"
allowed-tools: Bash(skills-hub:*)
---

Синхронизируй набор навыков текущего проекта с манифестом:

```bash
skills-hub --json sync $ARGUMENTS
```

Команда создаёт ссылки из стора по `.skills-hub/skills.toml`, докачивает
отсутствующее, и (по умолчанию) удаляет наши ссылки, которых нет в манифесте
(`--no-prune` отключает). Распарси JSON: `linked` / `downloaded` / `pruned` /
`missing`. Если `missing` непустой — сообщи, какие навыки не удалось получить
(нет доступа / нет в каталоге).
