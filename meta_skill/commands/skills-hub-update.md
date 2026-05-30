---
description: Обновить установленные skills (global + project).
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-update

Запусти:

```bash
skills-hub --json update --all
```

В выводе будет массив объектов `{slug, skill_id, scope, from, to, updated}`
(`slug` может быть `null` у slug-less skill — тогда идентичность по
`skill_id`). Кратко перескажи, какие skills реально обновились (где
`updated=true`). Если все актуальны — скажи "всё актуально".
