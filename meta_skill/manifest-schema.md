# Skill manifest — схема полей

Файл `_skill_meta.toml` (или фронтматтер `SKILL.md`) описывает skill при
публикации. Backend читает его в `skills-hub publish` и хранит в
`Skill.manifest` (JSON в БД).

## Обязательные поля

```toml
# _skill_meta.toml
description = "Краткое описание для каталога"
```

Если `_skill_meta.toml` отсутствует, backend пытается достать те же поля
из фронтматтера `SKILL.md`:

```yaml
---
name: my-skill
description: |
  Многострочное описание, которое попадёт в каталог.
  RU triggers: ..., ..., ...
  EN triggers: ..., ..., ...
---
```

## Опциональные поля

```toml
triggers = ["install", "поставь", "обнови"]
tags     = ["bitrix24", "crm", "rest-api"]

dependencies = [
    { slug = "bitrix24-base", min_version = "0.1.0" },
]
```

## E6 — тип навыка + CLI/MCP/runtime (MVP: флаги + манифест)

> Канон MVP: backend **читает и хранит** эти поля и проставляет денорм-флаги
> `has_cli` / `has_mcp` на навыке при publish. **Авто-установки CLI и
> авто-запуска MCP пока НЕТ** — это следующая волна. Сейчас это декларация.

```toml
# Тип навыка. Один из:
#   prompt        — чистый промпт-инструкция (только SKILL.md)
#   comprehensive — инструкция с кейсами/примерами/references
#   tooling       — несёт CLI и/или MCP-сервер
# Отсутствует ⇒ backend подставит дефолт при publish (prompt).
kind = "tooling"

# CLI-инструменты, которые несёт навык (tooling). Каждый — отдельная [[cli]].
# command_name — имя команды в PATH; entrypoint — module:callable или путь
# к скрипту (опционально, резолвится конвенцией при установке — будущая волна).
[[cli]]
command_name = "bx"
entrypoint   = "bx_cli.main:run"

[[cli]]
command_name = "bx-admin"

# MCP-серверы, которые несёт навык (tooling). Каждый — отдельная [[mcp]].
# transport — stdio | sse | http. config — произвольная таблица (url/env/args…).
[[mcp]]
server_name = "bitrix-mcp"
transport   = "stdio"

[[mcp]]
server_name = "bitrix-sse"
transport   = "sse"
config      = { url = "https://mcp.local/sse" }

# Runtime-зависимости ПАКЕТОВ (НЕ skill→skill — та секция выше `dependencies`).
# kind — pip | npm | system; spec — спецификация пакета.
runtime_dependencies = [
    { kind = "pip", spec = "httpx>=0.27" },
    { kind = "npm", spec = "@scope/cli" },
    { kind = "system", spec = "ffmpeg" },
]
```

Бэк-совместимость: отсутствие `kind` / `[[cli]]` / `[[mcp]]` /
`runtime_dependencies` ⇒ `kind` не задан (дефолт при publish), пустые списки,
флаги `has_cli=has_mcp=false`.

## E7 / E8 / E10 — опциональные поля (ROADMAP — publish их пока НЕ передаёт)

> ⚠️ **Внимание, skill-авторы:** поля ниже (`rating_enabled`,
> `comments_enabled`, `comments_allow_screenshots`, `support_tickets_enabled`,
> `support_assignee_email`, `support_kinds_allowed`) — **запланированная**
> функциональность. Текущая команда `skills-hub publish` собирает и отправляет
> только `version` / `description` / `triggers` / `tags` / `files` /
> `dependencies` / `preserved_paths`; перечисленные `*_enabled` /
> `support_*` ключи она **игнорирует** (на backend не уходят). Прописывать их
> сейчас бессмысленно — поведение определяется дефолтами `SystemConfig` (E9)
> до тех пор, пока publish не научится их передавать. Раздел оставлен как
> описание целевой схемы.

Backend трактует отсутствие этих полей как «использовать дефолт из
`SystemConfig`» (E9) — то есть skill-creator не обязан их прописывать.

```toml
# === Оценки (E7) ===
rating_enabled = true            # bool, default true (SystemConfig.ratings_default_enabled)
                                  # false → backend вернёт 403 на POST /skills/<slug>/ratings

# === Комментарии (E7) ===
comments_enabled = true          # bool, default true
                                  # false → POST /skills/<slug>/comments отключён
                                  #         GET всё ещё показывает существующие
comments_allow_screenshots = true # bool, default true
                                  # false → multipart-загрузка отвергается

# === Тех-поддержка (E8) ===
support_tickets_enabled = true   # bool, default true
                                  # false → POST /support/tickets с этим skill_id отклоняется

# Кому по дефолту назначаем тикеты (если backend не знает creator email)
support_assignee_email = "creator@example.com"   # string, optional

# Какие kinds разрешены пользователям через UI/CLI
support_kinds_allowed = ["bug", "feature", "question"]  # subset от {bug, feature, question, other}

# === Коллекции (E10) ===
# Не настраивается на уровне skill — это атрибут коллекции, не навыка.
# Skill попадает в dynamic-коллекцию автоматически, если у него есть
# нужные tags.
```

## Зарезервированные поля

Backend ставит автоматически — пользователь их не указывает:

| Поле                | Откуда                                          |
| ------------------- | ----------------------------------------------- |
| `version`           | передаётся в `--tag v0.1.0`                     |
| `files`             | сканируется автоматически (path + sha256 + size) |
| `preserved_paths`   | дефолт `["_local/", "browser_profiles/"]`       |
| `commit_sha`        | git rev-parse HEAD (при publish)                |

## Пример полного `_skill_meta.toml`

```toml
description = "Bitrix24 REST API wrapper + CRM helpers"

triggers = [
    "Bitrix24",
    "Битрикс24",
    "deal create",
    "сделка",
]

tags = ["bitrix24", "crm", "rest-api"]

dependencies = [
    { slug = "bitrix24-base", min_version = "0.1.0" },
]

# Целевые (ROADMAP) поля — publish их пока НЕ передаёт (см. раздел выше).
rating_enabled = true
comments_enabled = true
comments_allow_screenshots = true
support_tickets_enabled = true
support_assignee_email = "skills@cifro.pro"
support_kinds_allowed = ["bug", "feature", "question"]
```

## Совместимость

- Существующие skills без новых полей продолжают работать — backend
  использует дефолты из `SystemConfig`.
- Изменение `*_enabled` влияет только на новые операции; existing ratings/
  comments/tickets остаются доступны на read.
- При `publish` без явных полей backend ничего не записывает в manifest —
  поведение полностью дефолтное.

## JSON Schema (минимальная)

```json
{
  "$schema": "https://json-schema.org/draft-07/schema",
  "title": "SkillManifest",
  "type": "object",
  "required": ["version", "description"],
  "properties": {
    "version": {"type": "string"},
    "description": {"type": "string"},
    "triggers": {"type": "array", "items": {"type": "string"}},
    "tags": {"type": "array", "items": {"type": "string"}},
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["path", "sha256", "size"],
        "properties": {
          "path": {"type": "string"},
          "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
          "size": {"type": "integer", "minimum": 0}
        }
      }
    },
    "dependencies": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["slug"],
        "properties": {
          "slug": {"type": "string"},
          "min_version": {"type": "string"}
        }
      }
    },
    "preserved_paths": {"type": "array", "items": {"type": "string"}},
    "kind": {"enum": ["prompt", "comprehensive", "tooling"]},
    "cli": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["command_name"],
        "properties": {
          "command_name": {"type": "string"},
          "entrypoint": {"type": "string"}
        }
      }
    },
    "mcp": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["server_name", "transport"],
        "properties": {
          "server_name": {"type": "string"},
          "transport": {"enum": ["stdio", "sse", "http"]},
          "config": {"type": "object"}
        }
      }
    },
    "runtime_dependencies": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["kind", "spec"],
        "properties": {
          "kind": {"enum": ["pip", "npm", "system"]},
          "spec": {"type": "string"}
        }
      }
    },
    "rating_enabled": {"type": "boolean"},
    "comments_enabled": {"type": "boolean"},
    "comments_allow_screenshots": {"type": "boolean"},
    "support_tickets_enabled": {"type": "boolean"},
    "support_assignee_email": {"type": "string", "format": "email"},
    "support_kinds_allowed": {
      "type": "array",
      "items": {"enum": ["bug", "feature", "question", "other"]}
    }
  }
}
```
