# skillery-cli

CLI-клиент для **Skillery** — маркетплейса AI-навыков (skills) для агентов
(Claude Code, Codex и совместимых). Ставит навыки в центральный стор, линкует
их в проект или глобально, синхронизирует и обновляет.

## Установка

```bash
# Изолированно через pipx (рекомендуется):
pipx install skillery-cli

# либо в текущее окружение:
pip install skillery-cli
```

После установки доступна команда `skillery`:

```bash
skillery --help
```

> Прежнее имя команды `skills-hub` оставлено как совместимость и лишь
> подсказывает перейти на `skillery`.

## Quickstart

```bash
# Онбординг: зарегистрироваться самому либо принять инвайт-ссылку/токен.
skillery register
skillery join <ссылка-или-токен>
# либо, если уже есть учётка:
skillery login --email me@example.com --password ...

# Посмотреть доступные навыки (доступ фильтруется правами):
skillery list
skillery collections                 # кураторские подборки

# Навыки ставятся в центральный стор (~/.skillery/store) и линкуются junction/
# symlink. Дефолтный scope — project (текущая папка).
skillery install bitrix24                 # → стор + ссылка в ./.claude/skills
skillery install bitrix24 --scope global  # → стор + ссылка в ~/.claude/skills

# Автономно — без хаба, из локальной папки или git (логин не нужен):
skillery install --path ./my-skill
skillery install --from-git <url> --ref v1.0.0

# Динамический набор проекта:
skillery enable wb-api           # включить в текущем проекте
skillery disable wb-api          # выключить (стор цел)
skillery sync                    # применить манифест проекта
skillery store list              # что в сторе

# Обновить установленные навыки:
skillery update --all
```

## Установка под всех агентов

CLI автодетектит установленного агента (Claude Code / Codex) по наличию
`~/.claude` или `~/.codex` и кладёт навыки в правильный каталог. Можно
форсировать конкретного агента или поставить сразу под всех:

```bash
skillery install bitrix24 --agent codex
skillery install bitrix24 --all-agents
```

## Оценки, комментарии, тикеты, коллекции

После логина (по правам) доступны:

```bash
# Оценить навык (1..5) и посмотреть сводку:
skillery rate bitrix24 5
skillery rating-summary bitrix24

# Комментарий (с необязательным скриншотом):
skillery comment bitrix24 "работает в облаке" --screenshot ~/proof.png
skillery comments bitrix24 --limit 25

# Тикеты в поддержку:
skillery ticket create "OAuth ломается" --skill bitrix24 --kind bug
skillery tickets list --status open

# Коллекции (просмотр из CLI):
skillery collections list --type dynamic
skillery collection show popular
```

## Онбординг проекта

```bash
# Детект стека (python / nodejs / nextjs / react / docker / terraform / go /
# rust / claude-code) → подбор навыков → включение в проект.
skillery onboard --yes
```

## Event tracking + daemon

CLI не шлёт события синхронно: он кладёт их в локальную очередь
(`~/.skillery/events.queue.json`) — сбой записи никогда не ломает команду.
Отдельный демон периодически берёт batch и отправляет его на бэкенд.

```bash
skillery event queue --show      # что в локальной очереди
skillery daemon start            # запустить фоновый sender
skillery daemon status           # alive + последний цикл + размер очереди
skillery daemon install          # сгенерировать autostart-юнит (launchd / systemd / Task Scheduler)
```

`daemon install` не трогает system-wide настройки — он лишь пишет unit-файл в
user-scope и печатает инструкцию по активации. Это безопасно для CI и
разделяемых окружений. Отправка событий работает и анонимно: протухшая сессия
не глушит телеметрию автономного CLI.

## Конфиг

`~/.skillery/config.toml`:

```toml
base_url   = "https://hub.example.com"
web_ui_url = "https://hub.example.com/app"
output_format = "text"                # text | json
auto_update = false                   # true → тихо обновляет навыки при следующей команде
default_install_scope = "project"     # project | global
# store_dir = "~/.skillery/store"     # центральный стор (env: SKILLS_HUB_STORE_DIR)
```

Токены (access + refresh) хранятся в **OS keyring**:

- Windows — Credential Manager;
- macOS — Keychain;
- Linux — Secret Service (GNOME Keyring, KWallet, …).

Установки со старым каталогом `~/.skills-hub` подхватываются автоматически и
мигрируются в `~/.skillery` при первой записи.

## Профили

`~/.skillery/profiles/staging.toml` + `--profile staging` — отдельный backend и
отдельные токены для другой среды.

## Вывод: text | json

По умолчанию команды печатают человекочитаемый текст. Флаг `--json` (или
`output_format = "json"` в конфиге) переключает вывод в JSON Lines — удобно для
скриптов и агентов: результат идёт в stdout, сообщения и ошибки — в stderr.

## Лицензия

MIT — см. [LICENSE](LICENSE).
