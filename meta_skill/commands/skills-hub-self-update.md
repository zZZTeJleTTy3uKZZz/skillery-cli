---
description: Обновить сам skills-hub CLI до последней версии (из исходников репозитория).
allowed-tools: Bash(pip *), Bash(pipx *), Bash(python *), Bash(skills-hub *)
---

# /skills-hub-self-update

Цель — поднять `skills-hub` CLI до свежей версии. Пакета на PyPI пока нет,
поэтому обновление идёт **из исходников монорепо** (папка `client/`).

1. Узнать текущую версию:

```bash
skills-hub --version
```

2. Обновить исходники и переустановить. Если репозиторий доступен локально —
   подтяни его и поставь из `client/` тем же способом, что и при установке
   (через bootstrap-скрипт):

```bash
# из корня репозитория Skills Hub
git pull
python client/meta_skill/scripts/install.py        # pipx/pip из client/

# либо вручную, если знаешь путь к client/
pipx install --force <repo>/client                  # pipx-окружение
pip install -U <repo>/client                        # текущий venv
```

3. Проверить, что версия поднялась:

```bash
skills-hub --version
```

Если поднялась — сообщи пользователю новую версию. Если CLI не найден в PATH —
подскажи, где он установлен (`pipx list` либо `python -m pip show
skills-hub-cli`).

> `pip install -U skills-hub-cli` (из PyPI) сейчас НЕ сработает — дистрибутив
> в PyPI/npm только планируется.
