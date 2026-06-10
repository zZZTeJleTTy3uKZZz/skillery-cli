---
description: Поставить skill — из Skills Hub (id-или-slug) ИЛИ автономно из локальной папки / git.
argument-hint: <id-или-slug> | --path ./skill | --from-git <url> [--ref <tag>] [--scope global|project]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-install

Аргумент: `$ARGUMENTS`. Три режима:

1. **Из хаба** — `<id-или-slug>` (например, `bitrix24` или `42`), опционально
   `--scope global|project`. Требует логина и права `skill.install`.
2. **Автономно из папки** — `--path ./skill` (или абсолютный путь). Кладёт
   навык из локальной директории в стор **без хаба и без логина**.
3. **Автономно из git** — `--from-git <url> [--ref <tag>]`. Клонирует навык из
   git (по умолчанию default-ветка; `--ref` фиксирует тег/ветку/коммит), тоже
   **без хаба**.

Если ни id-или-slug, ни `--path`, ни `--from-git` не переданы — спроси у
пользователя или подскажи `skills-hub list` (для хаба).

Запусти:

```bash
skills-hub --json install $ARGUMENTS
```

Если это установка из хаба и backend ответил 403 / 404 / "VALIDATION" —
объясни, что у пользователя нет прав на этот skill (попросить admin invite или
`skill.install` permission). Для `--path` / `--from-git` логин не нужен — если
там ошибка, причина обычно в пути/URL/ref.

После успешной установки кратко перескажи: куда поставлен (`target_dir`),
какая версия, было ли это update.
