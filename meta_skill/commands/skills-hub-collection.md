---
description: Коллекции навыков — единый sub-app `collection`: серверные (хаб, default) + локальные (--local, оффлайн).
argument-hint: list|show|install|create|add|remove|delete [...] [--local]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-collection

Единый sub-app `collection` с 7 глаголами: `list / show / install / create /
add / remove / delete`. Все 7 видны в `--help` всегда. Флаг **`--local`**
переключает источник:

- **без `--local`** — **серверная** коллекция хаба (кураторская подборка,
  Spotify-like; gate по правам JWT: `list`/`show` = `skill.read`,
  `install` = `skill.install`). Без права — ошибка `NOT_AVAILABLE` с
  подсказкой добавить `--local`;
- **с `--local`** — **локальная** коллекция в `~/.skills-hub/collections.toml`
  (точнее `<config_dir>/collections.toml`: уважает `SKILLS_HUB_CONFIG_DIR` и
  `--profile`). Полностью оффлайн, без логина и хаба; Web UI её не видит.

`create / add / remove / delete` существуют **только локально** → `--local`
**обязателен**. Без него — ошибка `USE_LOCAL_FLAG` («серверные коллекции
создаются/редактируются в Web UI»). Серверный CRUD в CLI не реализован.

## Серверные коллекции (хаб) — default, read-only + install

```bash
skills-hub --json collection list                      # все доступные (static + dynamic)
skills-hub --json collection list --type dynamic        # фильтр по типу (static|dynamic)
skills-hub --json collection list --company <id>        # фильтр по компании
skills-hub --json collection list --owner <id>          # фильтр по владельцу
skills-hub --json collection list --no-global           # без global-коллекций (default: включены)
skills-hub --json collection show <id-или-slug>         # детали + развёрнутый список skills
skills-hub --json collection install <id-или-slug>      # массовый install всех skills
```

`collection install <id-или-slug>` ставит всю подборку одной командой —
главный онбординг-кейс (например `collection install bitrix-starter`). Флаги
как у `install`: `--scope global|project`, `--project P`, `--channel C`
(default `published`), `--force`, `--agent A`.

Если пользователь спрашивает «что у вас есть» — предпочитай `collection list`
перед плоским `skills-hub list`: коллекции тематически сгруппированы.

## Локальные коллекции (--local) — оффлайн, без хаба и логина

```bash
skills-hub collection create my-stack --local --title "Мой стек"
skills-hub collection add my-stack bitrix24 --local       # слаг или числовой id
skills-hub collection add my-stack wb-api --local         # нет в сторе → warning, но добавится
skills-hub --json collection list --local                 # имя / размер / чего нет в сторе
skills-hub --json collection show my-stack --local        # одна локальная коллекция: состав + чего нет в сторе
skills-hub --json collection install my-stack --local     # установить весь набор
skills-hub collection remove my-stack wb-api --local      # убрать из коллекции (диск цел)
skills-hub collection delete my-stack --local             # удалить коллекцию (навыки на диске целы)
```

Подсказки:

- `add … --local` принимает слаг, которого ещё нет в сторе (будет warning) —
  `install … --local` докачает его из хаба, когда пользователь залогинен.
- `list --local` показывает, каких слагов нет в сторе (колонка «нет в сторе»).
- `show <name> --local` показывает одну локальную коллекцию: состав и чего
  нет в сторе.
- `install … --local` для каждого слага: есть в сторе → линк (без сети); нет
  + залогинен → докачка из хаба; нет + не залогинен → `skipped` с подсказкой
  (команда не падает). Распарси JSON `{installed, linked, skipped}` и сообщи
  итог; если есть `skipped` из-за логина — предложи `skills-hub login`.
- `delete … --local` / `remove … --local` не трогают установленные навыки на
  диске.

Используй локальные коллекции, когда пользователь хочет повторяемый личный
набор навыков («мой стек») для новых машин и проектов без публикации
коллекции в хаб.
