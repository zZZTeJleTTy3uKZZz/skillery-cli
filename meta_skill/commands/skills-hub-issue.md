---
description: Legacy alias — отправить bug-report по skill'у (теперь через тикет-систему).
argument-hint: <slug> "<title>" "<description>"
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-issue

**Legacy команда.** Сейчас под капотом это `ticket create --kind bug`,
но имя сохранено для обратной совместимости со старыми ссылками.

Аргументы: `$ARGUMENTS` — ожидается `<slug> "<title>" "<description>"`.

Спрашивай у пользователя, если чего-то не хватает: какой skill сломан,
какая ошибка (заголовок), какие шаги повторить (тело).

Запусти:

```bash
skills-hub --json report $ARGUMENTS
# или эквивалентный вариант через тикеты:
# skills-hub --json ticket create "<title>" --skill <slug> --kind bug --body "<description>"
```

Сообщи пользователю `issue_id` / `ticket_id` — по нему создатель skill'а
получит уведомление.
