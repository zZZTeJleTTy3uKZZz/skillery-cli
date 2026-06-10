---
description: Управление компанией Skills Hub — создать, invite-links, granted-каталог, switch.
argument-hint: create|invite-links|catalog|show|switch [...]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-company

Обёртка над sub-app `skills-hub company *`. Определи по `$ARGUMENTS` (или по
смыслу запроса пользователя), какая операция нужна, и выполни. `--company
<id>` везде опционален — по умолчанию активная компания из JWT.

## Создать компанию (право `hub.company_create`)

```bash
skills-hub --json company create --name "<Название>" \
    --owner-email <owner@x.io> --owner-name "<Имя владельца>"
```

`--slug <slug>` — только при праве `hub.slug_manage` (иначе slug-less).
Из ответа отдай пользователю `owner_invite_token` и `owner_invite_url` —
владелец входит через `skills-hub login <token>`.

## Пригласительные ссылки (право `company.manage` | `role.manage`)

```bash
# Создать переиспользуемую join-ссылку для команды
skills-hub --json company invite-links create \
    --kind member --max-uses 50 --expires-in-days 30
```

В ответе `join_url` — ГОТОВАЯ ссылка (`<web-ui>/join/<token>`); токен
показывается ОДИН раз — сразу передай пользователю. `--kind manager` может
создать только владелец компании. Управление:

```bash
skills-hub --json company invite-links list
skills-hub --json company invite-links revoke <link_id>
```

## Granted-каталог (просмотр: `catalog.manage` | `catalog.view_all`; правки: `catalog.manage`)

```bash
skills-hub --json company catalog list                      # навыки + коллекции + effective
skills-hub --json company catalog grant <id-или-slug>      # выдать навык (slug резолвится сам)
skills-hub --json company catalog grant <id-или-slug> --collection   # выдать коллекцию
skills-hub --json company catalog revoke <id-или-slug> [--collection]
```

## Прочее

```bash
skills-hub --json company show <id>            # детали (любой залогиненный)
skills-hub --json company switch <id>          # сменить активную компанию
skills-hub --json company edit <id> --name "<Новое>"   # право company.manage
skills-hub --json company list --q <строка>    # только hub.admin
```

После `company switch` токены перевыпущены (сохраняются автоматически) —
набор доступных команд мог измениться, при сомнении `skills-hub --help`.
