---
description: Поставить оценку skill'у (1-5 звёзд). Аргументы — id-или-slug и оценка.
argument-hint: <id-или-slug> <1-5>
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-rate

Аргументы: `$ARGUMENTS` — ожидается `<id-или-slug> <N>` где N от 1 до 5.

Если только id-или-slug → спроси оценку у пользователя. Если N вне 1..5 →
объясни и попроси повторить.

Запусти:

```bash
skills-hub --json rate $ARGUMENTS
```

Backend применит upsert (одна оценка на user × skill — повторный rate
обновит). Кратко сообщи новое значение и текущий средний балл по skill'у
(можно дополнительно вызвать `skills-hub --json rating-summary <id-или-slug>`).
