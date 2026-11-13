# itstep-mystat-mcp

Локальный MCP-сервер (stdio) для доступа к домашним заданиям ITStep MyStat:
синхронизация списка заданий, загрузка вложений, извлечение текста, поиск.

Ставится одной командой `uv tool install`, настраивается мастером `setup`:
логин и пароль вводятся в терминале, сервер сам прописывается в локальные
клиенты (OpenCode, Claude Code, Claude Desktop, Codex).

## Быстрый старт

```powershell
uv tool install itstep-mystat-mcp          # из индекса (когда опубликован)
# или из локальной папки проекта:
uv tool install .

itstep-mystat-mcp setup                    # логин/пароль + авто-регистрация в клиентах
```

`setup` задаёт три вопроса (если значения не переданы флагами) и делает всё сам:

1. спрашивает логин MyStat (`MYSTAT_LOGIN`);
2. спрашивает пароль (ввод скрыт, просит повторить);
3. проверяет пару реальным `POST /auth/login`;
4. пишет конфиг в `%APPDATA%\itstep-mystat-mcp\.env`;
5. регистрирует MCP-сервер в найденных клиентах.

После этого достаточно перезапустить клиент.

## Что умеет

| Инструмент | Назначение |
|---|---|
| `mystat_login` | принудительный login, замена сохранённого токена |
| `mystat_auth_status` | есть ли токен, когда получен, известен ли срок (токен не раскрывается) |
| `mystat_logout` | удалить токен из локального кеша |
| `homework_sync` | синхронизация списка + деталей + вложений |
| `homework_list` | список заданий из кеша (краткие сведения) |
| `homework_get` | одно задание: текст, вложения, статусы |
| `homework_search` | полнотекстовый поиск (FTS5) |
| `homework_read_file` | чтение вложения или извлечённого текста |
| `homework_cache_status` | статистика кеша |

## Команды CLI

```powershell
itstep-mystat-mcp setup     # мастер настройки (логин/пароль + клиенты)
itstep-mystat-mcp run       # MCP-сервер по stdio (это вызывают клиенты)
itstep-mystat-mcp status    # пути конфига/кеша, наличие кредов, токен, размер кеша
itstep-mystat-mcp sync      # разовая синхронизация из терминала
itstep-mystat-mcp login     # принудительный login, токен в кеш
itstep-mystat-mcp logout    # удалить токен
```

`itstep-mystat-mcp run` без аргументов — поведение по умолчанию (так его
запускают MCP-клиенты).

### Флаги `setup`

| Флаг | Смысл |
|---|---|
| `--login <login>` | логин без интерактивного вопроса |
| `--env-file <path>` | куда писать конфиг (по умолчанию `%APPDATA%\itstep-mystat-mcp\.env`) |
| `--cache-dir <path>` | каталог кеша (по умолчанию рядом с конфигом) |
| `--client auto\|all\|none\|<name>` | целевые клиенты; можно повторять. `auto` — найденные на машине (по умолчанию) |
| `--verify` / `--no-verify` | проверять ли креды реальным login (по умолчанию проверяет) |
| `-y`, `--yes` | не задавать вопросов: брать значения из флагов, `.env` или окружения |

Пароль можно передать через переменную окружения `MYSTAT_PASSWORD` — тогда
мастер не спрашивает его в интерактиве (удобно для скриптов и CI):

```powershell
$env:MYSTAT_PASSWORD = "..."      # останется в истории шелла — учитывайте это
itstep-mystat-mcp setup --login my_login --client none --no-verify
```

### Куда ставится конфиг и клиенты

| Клиент | Путь / способ |
|---|---|
| конфиг сервера | `%APPDATA%\itstep-mystat-mcp\.env` (кеш — `%APPDATA%\itstep-mystat-mcp\cache`) |
| OpenCode | `~/.config/opencode/opencode.jsonc` (или `opencode.json`) |
| Claude Code | `claude mcp add --scope user` (используется CLI, если он в `PATH`) |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| Codex | `~/.codex/config.toml` (`$CODEX_HOME` учитывается) |

Перед изменением существующего файла создаётся резервная копия
`<имя>.bak-YYYYMMDD-HHMMSS`. Если файл не парсится, он **не** перезаписывается —
вместо этого выводится сниппет для ручной вставки.

## Ручное подключение (если авто-регистрация не сработала)

OpenCode, `~/.config/opencode/opencode.jsonc`:

```jsonc
"mcp": {
  "itstep-mystat": {
    "type": "local",
    "command": ["C:\\Users\\<you>\\AppData\\Local\\uv\\tools\\itstep-mystat-mcp\\Scripts\\python.exe", "-m", "mystat"],
    "environment": { "PYTHONUTF8": "1" },
    "enabled": true,
    "timeout": 180000
  }
}
```

Claude Desktop / общий JSON-формат:

```json
{
  "mcpServers": {
    "itstep-mystat": {
      "command": "itstep-mystat-mcp",
      "args": ["run"],
      "env": { "PYTHONUTF8": "1" }
    }
  }
}
```

Codex, `~/.codex/config.toml`:

```toml
[mcp_servers.itstep-mystat]
command = "itstep-mystat-mcp"
args = ["run"]
env = { PYTHONUTF8 = "1" }
```

Таймаут увеличен: первая синхронизация скачивает десятки файлов.

## Разработка (из исходников)

```powershell
uv sync
uv run python -m mystat status
uv run python -m mystat sync --no-attachments
```

`python server.py` оставлен как тонкая обёртка над `mystat.server:main`.

Сервер общается по stdio. `stdout` занят протоколом; все логи идут в `stderr`
и в `cache/logs/mystat.log`.

## Структура

```text
server.py              тонкая обёртка (совместимость)
mystat/__main__.py     python -m mystat
mystat/cli.py          setup / run / status / sync / login / logout
mystat/clients.py      регистрация MCP в opencode, claude*, codex
mystat/server.py       MCP-сервер (mcp 2.x MCPServer, stdio, 9 инструментов)
mystat/config.py       .env, пути, лимиты
mystat/cache.py        SQLite (WAL), миграции, FTS5
mystat/auth.py         login / refresh / хранение токена
mystat/api.py          единый HTTP-клиент, 401-recovery, ретраи
mystat/homework.py     нормализация, sync, read model
mystat/attachments.py  валидация URL, скачивание, имена, MIME
mystat/extraction.py   PDF/DOCX/PPTX/XLSX/TXT/HTML
mystat/search.py       безопасные FTS5-запросы
docs/api-findings.md   подтверждённый контракт API
scripts/               разведочные скрипты (не входят в поставку)
```

## Ключевые решения (по фактам API)

- Токен — JWT, живёт 30 дней; `expires_at` берётся из `exp`.
- `401` → `POST /auth/refresh` с текущим токеном; при неудаче — `POST /auth/login`.
  Один повтор запроса, не более.
- Текст задания есть **только** в `GET /od/homework/{id}` (`comment` в списке пуст).
- Детали приходят объектом с ключом `"0"`, а не массивом — нормализатор
  поддерживает оба варианта.
- Имя файла — из `Content-Disposition` (`filename*`, UTF-8); MIME проверяется
  по magic bytes; `ETag` у файлового хранилища недостоверен, поэтому изменения
  определяются по SHA-256.
- Скачиваются только `https://*.itstep.org/api/v1/files/...`; проверяется
  разрешённый IP (private/loopback/link-local запрещены), размер ограничен,
  редиректы проверяются.
- ZIP не распаковывается, OCR не выполняется (первая версия).

## Безопасность

- Логин и пароль хранятся только в локальном конфиге, который не попадает
  в репозиторий (`.env` в `.gitignore`).
- Пароль никогда не выводится: `setup` читает его скрытым вводом, `status`
  показывает только `configured` / `MISSING`.
- Токен никогда не возвращается MCP-инструментами и не пишется в логи;
  заголовок `Authorization` в лог не попадает.
- Регистрация клиентов не удаляет чужие записи: изменяется только ключ
  `itstep-mystat`.

## Тесты

Набор тестов есть локально, но не входит в репозиторий (см. `.gitignore`):

```powershell
.venv\Scripts\python.exe -m pytest -q
```

82 теста: авторизация и refresh, ретраи, нормализация, sync, пометка
исчезнувших заданий, безопасность URL и имён, скачивание, FTS5, CLI,
регистрация клиентов и кодирование значений `.env`. Тесты не ходят в сеть:
`httpx.MockTransport` и обезличенные фикстуры в `tests/fixtures/`.

## Формат `.env`

`setup` пишет значение без кавычек, когда это безопасно (логин, Windows-пути),
и в двойных кавычках с экранированием — когда внутри есть пробелы, `#`,
кавычки или обрамляющие пробелы:

```dotenv
MYSTAT_LOGIN=student_login
MYSTAT_PASSWORD="pa ss#1 \"x\""
CACHE_DIR=C:\Users\you\AppData\Roaming\itstep-mystat-mcp\cache
```

Читается файл с `interpolate=False`: `python-dotenv` подставляет `${VAR}`
даже внутри кавычек, и без этого пароль вида `${...}` молча превращался бы в
пустую строку. После записи значения сверяются обратным чтением — при
расхождении `setup` сообщает об ошибке, не печатая сами значения.

`--cache-dir` всегда сохраняется абсолютным: MCP-клиент запускает сервер из
произвольного рабочего каталога, относительный путь разрешился бы не там.

## Ограничения

- Первый запуск `homework_sync` требует действующих учётных данных
  (мастер `setup` или `.env`).
- Токен хранится в SQLite открытым текстом (как в ТЗ). Шифрование через
  Windows DPAPI можно добавить отдельным этапом.
- Если пароль не принимается, API отвечает одинаковым `401` и для неверного
  пароля, и для несуществующего логина — поэтому «неверный пароль» и
  «неизвестный логин» неразличимы.
- Проектный `.env` в папке разработки имеет приоритет над
  `%APPDATA%\itstep-mystat-mcp\.env`; `setup` предупреждает об этом.
