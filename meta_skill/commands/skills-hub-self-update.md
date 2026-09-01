---
description: Обновить сам CLI до последней версии с PyPI.
allowed-tools: Bash(uv *), Bash(pipx *), Bash(pip *), Bash(skillery *)
---

# /skills-hub-self-update

Цель — поднять CLI `skillery` до свежей версии. Пакет опубликован на PyPI под
именем `skillery-cli`; обновление умеет сам CLI.

1. Узнать текущую версию:

```bash
skillery --version
```

2. Обновить:

```bash
skillery cli upgrade            # сам определит uv tool / pipx / pip
skillery cli upgrade --check    # только сверить версию, без установки
```

Если CLI по какой-то причине не запускается, тот же результат вручную —
менеджером, которым он поставлен:

```bash
uv tool install --upgrade skillery-cli
pipx upgrade skillery-cli
pip install -U skillery-cli
```

3. Проверить, что версия поднялась:

```bash
skillery --version
```

Если поднялась — сообщи пользователю новую версию. Если CLI не найден в PATH —
подскажи, где он установлен (`uv tool list`, `pipx list` либо `python -m pip
show skillery-cli`).

> Прежнее имя команды `skills-hub` больше не работает — оно лишь подсказывает
> перейти на `skillery`. Исходники живут в репозитории `skillery-cli`; каталога
> `client/` монорепо, на который ссылалась прежняя версия этой команды, больше
> нет.
