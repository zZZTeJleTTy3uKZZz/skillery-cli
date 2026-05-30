---
description: Поставить skill из Skills Hub. Аргумент — id-или-slug (например, bitrix24 или 42).
argument-hint: <id-или-slug> [--scope global|project]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-install

Аргумент: `$ARGUMENTS` (id-или-slug плюс опциональные флаги).

Если id-или-slug не передан — спроси у пользователя или подскажи `skills-hub list`.

Запусти:

```bash
skills-hub --json install $ARGUMENTS
```

Если backend ответит 403 / 404 / "VALIDATION" — объясни пользователю, что
у него нет прав на этот skill (попросить admin invite или skill.install
permission).

После успешной установки кратко перескажи: куда поставлен (`target_dir`),
какая версия, было ли это update.
