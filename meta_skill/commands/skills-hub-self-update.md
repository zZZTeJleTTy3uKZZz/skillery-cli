---
description: Обновить сам skills-hub CLI до последней версии (pip / pipx).
allowed-tools: Bash(pip *), Bash(pipx *), Bash(skills-hub *)
---

# /skills-hub-self-update

Цель — поднять `skills-hub` CLI до свежей версии. Шаги:

1. Определить, как CLI поставлен:
   - `pipx list 2>/dev/null | grep -q skills-hub-cli` — pipx;
   - иначе — обычный pip в текущем env'е.

2. Обновить:

```bash
# pipx flow
pipx upgrade skills-hub-cli

# pip flow (если в активном venv'е)
pip install --upgrade skills-hub-cli
```

3. Проверить версию:

```bash
skills-hub --version 2>/dev/null || pip show skills-hub-cli | grep Version
```

Если поднялся — сообщи пользователю новую версию. Если не нашёлся в PATH —
объясни, где он установлен (см. `python -m pip show skills-hub-cli`
или `pipx list`).
