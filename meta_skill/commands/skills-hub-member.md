---
description: Участники компании Skills Hub — пригласить, роль, lock, reset-password.
argument-hint: invite|remove|change-role|lock|unlock|reset-password [...]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-member

Обёртка над `skills-hub members` / `member *` / `roles`. `--company <id>`
опционален (default — активная компания из JWT).

## Посмотреть участников и роли (любой залогиненный)

```bash
skills-hub --json members [--q <строка>] [--page N] [--size N]
skills-hub --json roles                  # глобальный каталог ролей → выбрать role-id
```

Без admin-прав backend показывает в `members` только самого пользователя —
это норма, не ошибка.

## Пригласить (право `user.invite`)

```bash
skills-hub --json member invite --email <dev@x.io> --role-id <role_id> [--name "Имя"]
```

`role_id` бери из `skills-hub roles` (роли с пометкой «assignable by
company»; не-assignable требуют hub.admin). Display-name по умолчанию —
часть email до `@`. Отдай пользователю `invite_token` / `invite_url`.

## Управление (по правам)

```bash
skills-hub --json member change-role <user_id> <role_id>   # role.manage
skills-hub --json member remove <user_id>                  # user.remove (сессии отзываются)
skills-hub --json member lock <user_id> --reason "<...>"   # user.lock (self-lock запрещён)
skills-hub --json member unlock <user_id>                  # user.lock
skills-hub --json member reset-password <user_id>          # company.manage
```

⚠️ `reset-password`: одноразовый пароль показывается ОДИН раз; в `--json`
он приходит в stdout полем `temp_password` — передай его пользователю
напрямую и **не записывай в логи / файлы / историю**.
