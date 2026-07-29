# AGENTS.md — skillery-cli

> Контекст для AI-ассистентов (Claude Code, ChatGPT, Cursor и т.п.), работающих
> над этим проектом.

## Что это

CLI Skillery (PyPI skillery-cli): установка навыков, демон, телеметрия

## Atlas

Проект зарегистрирован в Atlas-БД (Atlas). Карточка:

```sh
atlas projects get skillery-cli
```

Любые изменения метаданных (приоритет, статус, теги) — через atlas CLI:

- `atlas projects update skillery-cli --priority P0` — поменять приоритет
- `atlas project tag add skillery-cli -t domain:<slug>` — добавить тег
- `atlas projects move skillery-cli --to-type <type>` — конвертировать тип

## Тип / Статус (на момент создания)

- type=`service`, status=`active`, priority=`P2`

## Правила работы

- Все исходные тексты, документы, код проекта — в этом репо.
- Чувствительные данные (`.env`, токены, ключи) — игнорируются `.gitignore`.
- AI-ассистенту разрешено: читать, генерировать, редактировать в этом репо.

## Канонические команды

- `atlas projects get skillery-cli` — карточка проекта
- `atlas task list --project skillery-cli` — задачи проекта (когда W7
  волна будет реализована)

<!-- atlas:usage:start -->
## Управление проектом — через Atlas

Этот проект ведётся в Atlas (личная PM-система портфеля). Для задач/проектов/эпиков/бэкапов
используй CLI `atlas` и вызывай навык `atlas` — вся логика и роутинг внутри навыка.
<!-- atlas:usage:end -->
