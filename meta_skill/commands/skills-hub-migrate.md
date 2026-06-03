---
description: Перевести старые copy-установки навыков на модель стор+ссылка
argument-hint: "[--scope all|global|project] [--dry-run]"
allowed-tools: Bash(skills-hub:*)
---

Переведи существующие копии навыков в центральный стор со ссылками. Сначала
покажи план:

```bash
skills-hub --json migrate --dry-run $ARGUMENTS
```

Затем, если план верный, выполни без `--dry-run`:

```bash
skills-hub --json migrate $ARGUMENTS
```

Распарси JSON `reports.<scope>`: `migrated` (переведены), `skipped_foreign`
(чужие папки без _skill_meta.json — не тронуты), `skipped_linked` (уже ссылки),
`failed`. Чужие/ручные навыки и внешние симлинки не трогаются — это нормально.
