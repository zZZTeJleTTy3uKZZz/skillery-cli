---
description: Показать кураторские коллекции skills (Spotify-like подборки).
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-collections

Запусти:

```bash
skills-hub --json collections list
```

Покажи пользователю коротко: название, описание, тип (static / dynamic),
сколько skills внутри. Подскажи, что можно `skills-hub collection show
<id-или-slug>` для развёрнутого списка, и `skills-hub collection install
<id-или-slug>` для массовой установки всех skills из коллекции (главный
онбординг-кейс — поставить всю подборку одной командой).

Если пользователь спросил, что есть в hub'е — предпочитай `collections list`
перед плоским `list`, потому что они тематически сгруппированы. А если он
хочет «поставь мне набор для X» — `collection install <id-или-slug>`.

Если пользователь хочет **личный** набор (оффлайн, без публикации в хаб) —
это локальные коллекции: `/skills-hub-collections-local`
(`collection create-local / add-local / install-local …`).
