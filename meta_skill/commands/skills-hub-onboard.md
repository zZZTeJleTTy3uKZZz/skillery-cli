---
description: Онбординг проекта — детект стека, подбор навыков (стор + хаб), включение.
argument-hint: [--project <path>] [--yes] [--limit N]
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-onboard

Запусти из корня проекта (или передай `--project <path>`):

```bash
skills-hub --json onboard $ARGUMENTS
```

Без `--yes` команда ничего не ставит — только возвращает
`{signals, suggestions}`. Покажи пользователю компактную таблицу: `slug`,
`source` (local | hub | both), `signals` (чем заматчился: python / nodejs /
nextjs / react / docker / terraform / go / rust / claude-code), пометку
`already` («уже включён»).

Затем спроси подтверждение:

- согласен на всё →

```bash
skills-hub --json onboard --yes
```

  Распарси `applied`: `linked` (включены из стора, без сети), `installed`
  (докачаны из хаба), `already`, `skipped` (+`reason`). Сообщи итог.

- согласен точечно → `skills-hub --json enable <slug>` по каждому выбранному.

Нюансы: без логина работает только по локальному стору (это штатный режим,
не ошибка; навыки не из стора попадут в `skipped` с `reason=not_logged_in` —
предложи `skills-hub login`); `--limit N` — кандидатов с хаба на сигнал
(капится 20); повторный запуск идемпотентен (`already` не трогаются).
