---
description: Показать статус skills-hub — что установлено, какой агент, логин.
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-status

Запусти:

```bash
skills-hub --json status
```

Распарсь JSON-ответ и кратко скажи пользователю:
1. Какой agent (claude_code / codex) и где skills-папка.
2. Залогинен ли (user_email).
3. Сколько навыков стоит (global + project).
4. Если что-то не так — подсказать `skills-hub login <invite-token>` или
   `skills-hub --help`.
