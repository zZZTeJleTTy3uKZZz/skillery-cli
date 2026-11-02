# ADR-0001: Продукты и компоненты — Skillery как реестр всех наших продуктов

- Статус: proposed
- Задача Atlas: #5981 (эпик `skillery-products`)
- Дата: 2026-11-02

## Контекст

Skillery сейчас — реестр навыков (`skills`, `skill_versions`, `_skill_meta.toml`).
Остальные наши продукты ставятся каждый по-своему: плагины DeepSeek Harness —
`dsh-plugins/registry.json` + `install.ps1`; плагины Claude Code —
`dsh-plugins/.claude-plugin/marketplace.json` + `dshplugins claude sync`; CLI —
uv tool / свой `install.sh` / артефакты GitLab go-release / `atlas` с `/dl/`;
диктовка — Inno Setup `Dictation-Setup-<ver>.exe`. На новом устройстве нет
одной команды «поставь всё моё».

## Решение

### Модель

```
Product (slug, title, owner company, access_level=private по умолчанию)
 └─ Component (product_id, key, type, depends_on[component keys | product slugs])
     └─ ComponentRelease (component_id, semver, channel, yanked, created_at)
         └─ ReleaseArtifact (release_id, os, arch, kind, url|skill_ref, sha256, size, install_json)
```

- `Component.type` ∈ `skill | cli | mcp | plugin-dsh | plugin-claude | service`.
- `os` ∈ `windows | linux | macos | any`; `arch` ∈ `amd64 | arm64 | any`.
- `kind` артефакта (как доставить байты):
  - `skill-ref` — ссылка на существующий `skills.id` + semver (навык ставится
    существующим путём; модель навыков НЕ меняется);
  - `uv-tool` — `install_json.package` (+ `index_url`, `extras`);
  - `binary` — архив/бинарь по `url` с обязательным `sha256`, `install_json.bin`
    (имя бинаря в архиве);
  - `archive` — папка плагина (zip/tar.gz) по `url` + `sha256`;
  - `installer` — установщик ОС (`.exe`/`.msi`/`.deb`), `install_json.args`
    (тихая установка), `install_json.detect` (как проверить установленную версию);
  - `mcp-spec` — без байтов: `install_json` = `{transport, command|url, args, env_keys}`
    (значения секретов не хранятся, только имена переменных).
- Права: `access_grants.entity_type` расширяется значением `product`; по умолчанию
  продукт виден только участникам компании-владельца (сейчас — наша команда).
- Назначение: `product_assignments (product_id, target_type user|device|company,
  target_id, channel, pinned_version NULL)`. `skillery sync` ставит всё
  назначенное пользователю, его устройству и компании.

### Контракт установки (backend → CLI)

`GET /products/{id}/install-plan?os=&arch=&channel=` и
`GET /me/products/install-plan?os=&arch=&device_id=` возвращают:

```json
{
  "products": [{
    "slug": "dictation", "version": "1.10.0",
    "components": [{
      "key": "dictation-service", "type": "service", "version": "1.10.0",
      "artifact": {"kind": "installer", "os": "windows", "arch": "amd64",
                    "url": "...", "sha256": "...", "size": 123,
                    "install": {"args": ["/VERYSILENT"], "detect": {"file": "%LOCALAPPDATA%\\Programs\\Dictation\\dictation.exe"}}},
      "depends_on": []
    }],
    "skipped": [{"key": "...", "reason": "no artifact for linux/amd64"}]
  }]
}
```

Компоненты в плане уже упорядочены топологически; компонент без артефакта под
ОС попадает в `skipped`, а не в ошибку.

### Установщики в CLI (`skillery-cli`)

Реестр обработчиков по `Component.type` (интерфейс: `plan → install → verify →
remove`, идемпотентно, со своим состоянием в `~/.skillery/products.json`):

| type | обработчик |
|------|------------|
| skill | существующий `_install_chain` (без изменений поведения) |
| cli | `uv tool install` / распаковка `binary` в `~/.local/bin` (Windows: `%LOCALAPPDATA%\skillery\bin` + PATH) |
| mcp | регистрация в конфиге агента (Claude: `claude mcp add --scope user`; прочие — через skillkit `mcp_config()`) |
| plugin-claude | распаковка в `~/.claude/dsh-plugins/<name>` + `CLAUDE_CODE_PLUGIN_DIRS` в `~/.claude/settings.json` (как `dshplugins claude sync`) |
| plugin-dsh | распаковка в `~/.dsh/plugins/<name>` + строка в `cordis.patch.yml` (логика из `dsh-plugins/install.ps1`) |
| service | скачать установщик, проверить sha256, тихо запустить, проверить `detect` |

Скачанное всегда проверяется по `sha256` до распаковки/запуска.

### Хранение артефактов

Байты не хранятся в БД. `url` указывает на релизы GitLab (generic packages /
release assets из ci-platform `go-release.yml`) или PyPI; закрытые ссылки
отдаются backend'ом через свой прокси-эндпойнт с проверкой прав
(`GET /artifacts/{id}/download`) — решается в задаче хранения.

### Совместимость

- Таблицы и API навыков не меняются; `skill`-компонент ссылается на `skills.id`.
- `skillery install <slug>`: если slug — продукт, ставится продукт; иначе —
  прежняя установка навыка. Конфликт slug продукт/навык запрещён на уровне API
  (продукт, обёртывающий навык, обычно имеет тот же slug — тогда побеждает продукт,
  а навык ставится как его компонент — поведение для пользователя то же).
- Старые CLI не знают про продукты и продолжают ставить навыки.

## Последствия

- Миграция существующих реестров — отдельными импортёрами в манифест
  `product.toml` (формат ниже) и `skillery product publish`.
- Диктовке нужен CI-релиз установщика с sha256 (сейчас сборка локальная).
- Go CLI без релизов (crew, keyring) требуют релизного CI.

### Манифест продукта `product.toml`

```toml
slug = "dictation"
title = "Диктовка"
version = "1.10.0"

[[components]]
key = "dictation-service"
type = "service"
[[components.artifacts]]
os = "windows"
arch = "amd64"
kind = "installer"
url = "https://gitlab.com/.../Dictation-Setup-1.10.0.exe"
sha256 = "..."
install = { args = ["/VERYSILENT", "/SUPPRESSMSGBOXES"], detect = { file = "%LOCALAPPDATA%\\Programs\\Dictation\\dictation.exe" } }

[[components]]
key = "voice-input"
type = "plugin-dsh"
depends_on = ["dictation-service"]
[[components.artifacts]]
os = "windows"
arch = "any"
kind = "archive"
url = "..."
sha256 = "..."
```
