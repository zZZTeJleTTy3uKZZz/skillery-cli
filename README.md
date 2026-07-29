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

## Канон команд: `skillery <ресурс> <глагол>`

Команды сгруппированы по ресурсам (ресурс — в единственном числе):

| Ресурс | Про что | Глаголы |
|--------|---------|---------|
| `skill` | навыки | `list` `show` `suggest` `contributors` `install` `remove` `enable` `disable` `update` `installed` `sync` `pull` `push` `publish` `report` `new` |
| `auth` | аккаунт и сессия | `login` `logout` `whoami` `passwd` `register` `join` |
| `cli` | сам CLI | `status` `doctor` `logs` `config` `upgrade` |
| `store` | центральный стор | `list` `path` `gc` `migrate` |
| `device` | устройства | `list` |
| `collection` · `rating` · `comment` · `ticket` · `webhook` · `event` · `daemon` | — | `<группа> --help` |
| `member` · `role` · `permission` · `company` · `invite` · `admin` | администрирование | `<группа> --help` |

Вне групп остались `run` (обёртка запуска навыка — вшита в каждый `SKILL.md`),
`ask`, `web` и `onboard`: это разовые действия без ресурса-собрата.

**Старые плоские имена продолжают работать.** `skillery install …`,
`skillery members`, `skillery status` вызывают ровно те же функции, что и
новые формы — ни один существующий скрипт, `SKILL.md` или вызов из демона не
ломается. При вызове старого имени в **stderr** уходит подсказка с новой
формой; stdout не затрагивается, поэтому разбор вывода (в т.ч. `--json`)
остаётся валидным. Заглушить подсказку — `SKILLERY_NO_DEPRECATION_WARNINGS=1`.

## Quickstart

```bash
# Онбординг: зарегистрироваться самому либо принять инвайт-ссылку/токен.
skillery auth register
skillery auth join <ссылка-или-токен>
# либо, если уже есть учётка:
skillery auth login --email me@example.com --password ...

# Посмотреть доступные навыки (доступ фильтруется правами):
skillery skill list
skillery collection list             # кураторские подборки

# Навыки ставятся в центральный стор (~/.skillery/store) и линкуются junction/
# symlink. Дефолтный scope — project (текущая папка).
skillery skill install bitrix24                 # → стор + ссылка в ./.claude/skills
skillery skill install bitrix24 --scope global  # → стор + ссылка в ~/.claude/skills

# Автономно — без хаба, из локальной папки или git (логин не нужен):
skillery skill install --path ./my-skill
skillery skill install --from-git <url> --ref v1.0.0

# Динамический набор проекта:
skillery skill enable wb-api     # включить в текущем проекте
skillery skill disable wb-api    # выключить (стор цел)
skillery skill sync              # применить манифест проекта
skillery store list              # что в сторе

# Обновить установленные навыки:
skillery skill update --all
```

## Установка под всех агентов

CLI автодетектит установленного агента (Claude Code / Codex) по наличию
`~/.claude` или `~/.codex` и кладёт навыки в правильный каталог. Можно
форсировать конкретного агента или поставить сразу под всех:

```bash
skillery skill install bitrix24 --agent codex
skillery skill install bitrix24 --all-agents
```

## Оценки, комментарии, тикеты, коллекции

После логина (по правам) доступны:

```bash
# Оценить навык (1..5) и посмотреть сводку:
skillery rating set bitrix24 5
skillery rating summary bitrix24

# Комментарий (с необязательным скриншотом):
skillery comment add bitrix24 "работает в облаке" --screenshot ~/proof.png
skillery comment list bitrix24 --limit 25

# Тикеты в поддержку:
skillery ticket create "OAuth ломается" --skill bitrix24 --kind bug
skillery ticket list --status open

# Коллекции (просмотр из CLI):
skillery collection list --type dynamic
skillery collection show popular
```

## Автообновления навыка из git (webhook)

Чтобы новая версия навыка приезжала на хаб сама (push в репо → синк), у навыка
должен быть webhook. Команда делает обе половины: регистрирует его на хабе И
создаёт hook у провайдера локальным `gh` / `glab` (те же права, что у вас в
терминале — account-wide PAT не нужен).

```bash
# Включить автообновления (идемпотентно: повтор обновляет, а не дублирует hook):
skillery webhook register my-skill

# Состояние: настроен ли, какой провайдер, куда шлёт.
# --probe дополнительно спросит провайдера: есть ли hook и когда была доставка.
skillery webhook status my-skill --probe

# Отключить: снять запись на хабе и hook у провайдера.
skillery webhook revoke my-skill
```

Если `gh`/`glab` не установлен или не авторизован, команда не падает: она
печатает точные ручные шаги (URL приёмника, тип событий) и ОДИН раз — секрет
webhook'а для вставки в настройки репозитория. В `--json` секрет не выводится
никогда (только отпечаток `sha256:…`).

Монорепо (несколько навыков в одном репозитории) поддержано: у каждого навыка
СВОЙ hook — адрес приёмника один, но в нём стоит маркер навыка
(`…/webhooks/git/github?skillery_skill=<id>`). Секрет хаб выдаёт на каждый
навык отдельно, поэтому общий hook на репо ломал бы всех соседей: он хранит
один секрет, и доставки остальных навыков молча отбивались бы 401. Регистрация
навыка чужие hook'и не трогает — ни при `register`, ни при `revoke`.

Права: у GitHub нужен доступ admin к репо (scope `repo` у `gh` его покрывает),
у GitLab — роль Maintainer/Owner в проекте.

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
