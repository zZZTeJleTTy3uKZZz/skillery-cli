---
description: Оставить комментарий к skill'у. Аргументы — id-или-slug и тело.
argument-hint: <id-или-slug> "<body>" [--screenshot path] [--parent cmt_id]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-comment

Аргументы: `$ARGUMENTS` — ожидается `<id-или-slug> "<body>"` плюс опционально
`--screenshot <path>` для прикрепления изображения и `--parent <cmt_id>`
для ответа в thread.

Если id-или-slug или body не переданы — спроси.

Запусти:

```bash
skills-hub --json comment $ARGUMENTS
```

После создания скажи пользователю `comment_id` (`cmt_*`) — он понадобится
для `comment-edit` или `comment-delete`. Если есть creator skill'а — он
получит уведомление (через `/events/ingest` → notification pipeline).
