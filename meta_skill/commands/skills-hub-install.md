---
description: Поставить skill из Skills Hub. Аргумент — slug (например, bitrix24).
argument-hint: <slug> [--scope global|project]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-install

Аргумент: `$ARGUMENTS` (slug плюс опциональные флаги).

Если slug не передан — спроси у пользователя или подскажи `skills-hub list`.

Запусти:

```bash
skills-hub --json install $ARGUMENTS
```

Если backend ответит 403 / 404 / "VALIDATION" — объясни пользователю, что
у него нет прав на этот skill (попросить admin invite или skill.install
permission).

После успешной установки кратко перескажи: куда поставлен (`target_dir`),
какая версия, было ли это update.
