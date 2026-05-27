---
name: skills-hub
description: |
  Bootstrap-навык Skills Hub: устанавливает `skills-hub` CLI у клиента,
  регистрирует агента по invite-токену, ставит и обновляет приватные
  навыки команды Дмитрия. Используй когда пользователь говорит:
  "поставь skills-hub", "залогинься в skills-hub", "обнови мои навыки
  hub", "skills-hub install <slug>", "что есть в каталоге skills-hub",
  "отправь bug-report на skill". EN triggers: install skills-hub,
  login to skills hub, update hub skills, list hub skills, report bug
  to skill creator. RU triggers: поставь skills-hub, залогинь меня
  в hub, обнови навыки, список навыков hub, отправь баг по навыку.
---

# Skills Hub bootstrap-skill

Этот навык — точка входа клиента в систему Skills Hub. После установки
ты (агент) умеешь:

1. **Установить CLI** — `pip install -e ./scripts/install.py` (или
   из приватного pip-репо `pip install skills-hub-cli`).
2. **Авторизовать** клиента через invite-токен, который владелец
   (Дмитрий) выдал в Telegram/email.
3. **Перечислить** доступные навыки и **установить** нужные.
4. **Обновлять** установленные навыки одной командой.
5. **Сообщать** о багах создателю навыка через `skills-hub report`.

## Quickstart для агента

```bash
# 1. Один раз — установить CLI
python ./scripts/install.py            # или pip install skills-hub-cli

# 2. Авторизоваться (токен пользователь даст в чате)
skills-hub login <invite-token-or-URL>

# 3. Посмотреть что доступно
skills-hub list

# 4. Установить нужный skill
skills-hub install bitrix24

# 5. Проверить статус
skills-hub status

# 6. Обновить все
skills-hub install <slug>      # повторный install = обновление
```

## Что делать при ошибке

Если skill после install не работает корректно у пользователя:

1. Проверь `skills-hub status` — какой агент, какие версии.
2. Прогони smoke-тесты которые у skill есть.
3. Если воспроизводимый bug — пошли `skills-hub report <slug> --kind bug
   --title "..." --description "<полный лог>"`. Это создаст issue
   у создателя навыка в Hub'е, он получит уведомление.

## Структура

См. также:
- [`scripts/install.py`](./scripts/install.py) — установщик CLI
- Базовый CLI: `skills-hub` (после установки доступен в PATH)
- Backend: `<base_url>/docs` — OpenAPI Swagger

## Ограничения MVP

- Update пока — это повторный install (без incremental diff).
- Backend знает только GitLab.com (приватная группа `dmitry-skills-hub`).
- Sandbox-проверка skill'а перед публикацией пока только в виде
  валидации frontmatter; глубокие smoke-tests — следующая итерация.
