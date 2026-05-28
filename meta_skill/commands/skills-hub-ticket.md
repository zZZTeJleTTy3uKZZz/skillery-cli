---
description: Открыть тикет тех-поддержки в Skills Hub.
argument-hint: "<subject>" [--skill slug] [--kind bug|feature|question|other] [--priority low|normal|high|urgent] [--body "..."]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-ticket

Аргументы: `$ARGUMENTS` — минимум `"<subject>"`, плюс опционально
`--skill <slug>`, `--kind bug|feature|question|other`, `--priority
low|normal|high|urgent`, `--body "<длинное описание>"`, `--screenshot
<path>`.

Если subject не задан — спроси у пользователя кратко (1 строка) и
длинное описание (для `--body`).

По умолчанию kind=`question`, priority=`normal`. Если в контексте была
ошибка скилла — kind=`bug` + добавь стек-трейс в body.

Запусти:

```bash
skills-hub --json ticket create $ARGUMENTS
```

Backend сам выберет assignee:
1. Если `--skill <slug>` указан → creator этого skill'а.
2. Иначе → company-admin твоей компании.
3. Иначе → null (висит в общей очереди hub-admin'у).

Сообщи пользователю `tkt_id` — по нему можно делать `ticket show` и
`ticket reply`. Если в скилле кто-то назначен — можно сразу сказать,
что ticket assigned ему.
