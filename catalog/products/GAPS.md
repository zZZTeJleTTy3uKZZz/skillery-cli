# Пробелы каталога продуктов

Сгенерировано `scripts/import_registries.py`; руками не правится.

- Продуктов: 31
- Компонентов: 40
- Пробелов: 40

## Артефакты без url/sha256

| Продукт | Компонент | Что именно | Почему |
| --- | --- | --- | --- |
| atlas | atlas | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| atlas | atlas-cli | linux/amd64/binary: не хватает sha256 | ссылка собрана по формату издателя (api.atlas.pragmati.ru/dl/<asset>), но sha256 берётся из release.json релиза — локально его нет |
| atlas | atlas-cli | linux/arm64/binary: не хватает sha256 | ссылка собрана по формату издателя (api.atlas.pragmati.ru/dl/<asset>), но sha256 берётся из release.json релиза — локально его нет |
| atlas | atlas-cli | macos/amd64/binary: не хватает sha256 | ссылка собрана по формату издателя (api.atlas.pragmati.ru/dl/<asset>), но sha256 берётся из release.json релиза — локально его нет |
| atlas | atlas-cli | macos/arm64/binary: не хватает sha256 | ссылка собрана по формату издателя (api.atlas.pragmati.ru/dl/<asset>), но sha256 берётся из release.json релиза — локально его нет |
| atlas | atlas-cli | windows/amd64/binary: не хватает sha256 | ссылка собрана по формату издателя (api.atlas.pragmati.ru/dl/<asset>), но sha256 берётся из release.json релиза — локально его нет |
| background-habits | background-habits | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| crew | crew-cli | any/any/binary: не хватает url, sha256 | релиза нет: CI crew ничего не публикует, бинарь ставится копированием собранного файла (ci/ и AGENTS.md репозитория crew) |
| crew-devices | crew-devices | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| crew-integration | crew-integration | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| delegation | delegation | linux/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| dictation | dictation-service | windows/amd64/installer: не хватает url, sha256 | установщик собирается локально, CI-релиза с sha256 нет (ADR: диктовке нужен CI-релиз установщика) |
| dictation | voice-input | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| dsh-interface-scale | dsh-interface-scale | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| dsh-russian-locale | dsh-russian-locale | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| dsh-window-controls | dsh-window-controls | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| herdr | herdr-cli | any/any/binary: не хватает url, sha256 | собственного релиза нет: herdr — форк herdrdev/herdr, ставится из локальной сборки; артефакты апстрима — чужой код, брать их нельзя |
| job-wait | job-wait | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| keyring | keyring-cli | any/any/binary: не хватает url, sha256 | релиза нет: ставится из рабочей копии gitlab.com/cifropro1/kits/keyring скриптом install.sh, версия — коммит, semver не объявлен |
| limits | limits | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| limits-cache | limits-cache | linux/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| notify | notify | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| scratchpad | scratchpad | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| sessions | sessions | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| sleep-to-background | sleep-to-background | linux/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| subagent-model | subagent-model | linux/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| turn-actions | turn-actions | linux/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| win-clipboard | win-clipboard | linux/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |
| wsl-bash | wsl-bash | windows/any/archive: не хватает url, sha256 | архив плагина не публикуется: dsh-plugins раздаёт плагины из локального дерева, релиза с sha256 нет |

## uv-tool без публичного индекса

| Продукт | Компонент | Что именно | Почему |
| --- | --- | --- | --- |
| b24 | b24-cli | bitrix24-content-cli | пакета bitrix24-content-cli на PyPI нет (404): ставится из локального каталога |
| beget | beget-cli | beget-cli | пакета beget-cli на PyPI нет (404): ставится из локального каталога |
| gateway | gateway-cli | gateway | имя gateway на PyPI занято чужим пакетом 2.0; наш gateway 0.2.0 публикуется только из локального каталога — нужен приватный индекс |
| gws | gws-cli | gws | имя gws на PyPI занято чужим пакетом 0.0.1; наш gws 0.1.0 не опубликован |
| max | max-cli | max-cli | имя max-cli на PyPI занято чужим пакетом 1.2.1; наш max-cli 0.2.0 не опубликован |
| transcribe | transcribe-cli | s-transcribe | пакета s-transcribe на PyPI нет (404): CLI ставится из локального каталога, нужен релиз в приватном индексе |

## Зависимости без продукта

| Продукт | Компонент | Что именно | Почему |
| --- | --- | --- | --- |
| atlas | atlas | atlas | требование указывает на сам продукт: отдельного компонента-цели нет |
| limits | limits | 9router | в каталоге нет продукта или компонента с таким slug |
| limits | limits | subs | в каталоге нет продукта или компонента с таким slug |

## Версии компонентов, не отражённые в манифесте

| Продукт | Компонент | Что именно | Почему |
| --- | --- | --- | --- |
| atlas | atlas | atlas=0.1.0 | в манифесте одна версия продукта; версия компонента осталась в реестре плагинов |
| dictation | voice-input | voice-input=1.4.1 | в манифесте одна версия продукта; версия компонента осталась в реестре плагинов |
