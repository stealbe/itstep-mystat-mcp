# itstep-mystat-mcp

Локальный MCP-сервер (stdio) для доступа к домашним заданиям ITStep MyStat:
синхронизация списка заданий, загрузка вложений, извлечение текста, поиск.

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

## Установка

```powershell
cd C:\Users\parad\itstep-mystat-mcp
uv sync
Copy-Item .env.example .env   # затем вписать логин и пароль
```

`.env`:

```dotenv
MYSTAT_LOGIN=your_login
MYSTAT_PASSWORD=your_password
```

Допускаются также имена `LOGIN` / `PASSWORD`.

## Запуск вручную

```powershell
.venv\Scripts\python.exe server.py
```

Сервер общается по stdio. `stdout` занят протоколом; все логи идут в `stderr`
и в `cache/logs/mystat.log`.

## Подключение к OpenCode

`~/.config/opencode/opencode.jsonc`:

```jsonc
"mcp": {
  "itstep-mystat": {
    "type": "local",
    "command": [
      "C:\\Users\\parad\\itstep-mystat-mcp\\.venv\\Scripts\\python.exe",
      "C:\\Users\\parad\\itstep-mystat-mcp\\server.py"
    ],
    "environment": { "PYTHONUTF8": "1" },
    "enabled": true,
    "timeout": 180000
  }
}
```

Таймаут увеличен: первая синхронизация скачивает десятки файлов.

## Структура

```text
server.py              MCP-сервер (mcp 2.x MCPServer, stdio)
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

## Тесты

```powershell
.venv\Scripts\python.exe -m pytest -q
```

50 тестов: авторизация и refresh, ретраи, нормализация, sync, пометка
исчезнувших заданий, безопасность URL и имён, скачивание, FTS5.
Тесты не ходят в сеть: `httpx.MockTransport` и обезличенные фикстуры
в `tests/fixtures/`.

## Ограничения

- Первый запуск `homework_sync` требует действующих учётных данных в `.env`.
- Токен хранится в SQLite открытым текстом (как в ТЗ). Шифрование через
  Windows DPAPI можно добавить отдельным этапом.
- Если пароль не принимается, API отвечает одинаковым `401` и для неверного
  пароля, и для несуществующего логина.
