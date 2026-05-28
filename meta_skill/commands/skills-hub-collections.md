---
description: Показать кураторские коллекции skills (Spotify-like подборки).
allowed-tools: Bash(skills-hub *)
---

# /skills-hub-collections

Запусти:

```bash
skills-hub --json collections
```

Покажи пользователю коротко: название, описание, тип (static / dynamic),
сколько skills внутри. Подскажи, что можно `skills-hub collection show
<slug>` для развёрнутого списка, и `skills-hub collection install <slug>`
для массовой установки всех skills из коллекции.

Если пользователь спросил, что есть в hub'е — предпочитай collections
перед плоским `list`, потому что они тематически сгруппированы.
