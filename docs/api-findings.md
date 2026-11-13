# MyStat API — подтверждённые факты (discovery)

**Дата:** 12.11.2026
**Источник:** реальные запросы к `https://mapi.itstep.org/v1/mystat` + JS-бандл веб-клиента `https://mystat.itstep.org/main-PTSNAXV7.js`.
**Статус:** всё ниже — наблюдённые факты, кроме явно помеченных INFERENCE.

Закрывает неизвестные из раздела 18 ТЗ.

---

## 1. Авторизация

**`POST /auth/login`**

```http
Content-Type: application/json
X-Language: uk

{"login": "<login>", "password": "<password>"}
```

- `200` → тело **plain text JWT** (не JSON). `Content-Type: text/html; charset=UTF-8`, длина 604.
- `401` → `Unauthorized: error_unauthorized` (одинаково для неверного логина и пароля).
- `422` → `Validation error. The Password is required` (без пароля) / `validation_custom_email_required_without` (без логина).
- Имена полей: только `login` + `password`. `username`, `email` не принимаются (`500`/`422`).
- `X-Language`, `Origin`, `Referer`, `User-Agent`, `X-Userid` на результат **не влияют**.

**JWT claims (payload):** `iss, iat, exp, nbf, jti, sub, id, id_user_storage, email, username, name, branch_id, project_id, lang, profile, module, branch_alias`.
`exp - iat = 2 592 000 c` = **30 дней** → `expires_at` можно заполнять из токена без верификации подписи.

**`POST /auth/refresh`** (в ТЗ помечен как неизвестный — теперь известно)

```http
Authorization: Bearer <access_token>
Content-Type: application/json

{}
```

- `200` → новый JWT тем же plain-text форматом. **Refresh token не нужен** — сам access token выступает основанием для refresh.
- `401` без заголовка → `Unauthorized: Token is empty`.

Веб-клиент после логина вызывает `/auth/me` (`accountService.getAccountForce$`), но для MCP-потока он не требуется: `list` и `homework/{id}` работают сразу с Bearer-токеном.

---

## 2. Список заданий

**`GET /od/homework/list?sort=-hw.time&status=3&limit=1000`**

Заголовки: `Authorization: Bearer <token>`, `X-Language: uk`. **`X-Userid` не требуется** (проверено: без него `200`).

Форма ответа:

```json
{ "data": [ ... 60 элементов ... ], "_meta": { "currentPage": 1, "totalPages": 1, "totalCount": 60 } }
```

Поля элемента списка:

| Поле | Тип | Пример / смысл |
|---|---|---|
| `id` | int | `502719` — ID задания |
| `id_spec` | int | ID предмета |
| `id_teach` | int | ID преподавателя |
| `id_group` | int | ID группы |
| `fio_teach` | str | ФИО преподавателя |
| `theme` | str | тема задания |
| `name_spec` | str | название предмета |
| `comment` | str | **всегда пустой в списке** (см. п.3) |
| `creation_time` / `completion_time` / `overdue_time` | str | `YYYY-MM-DD` |
| `file_path` | str \| null | URL основного вложения |
| `cover_image` | str \| null | URL картинки-обложки |
| `status` | int | наблюдалось только `3` при фильтре `status=3` |
| `homework_stud` | obj | данные сдачи: `id, file_path, mark, creation_time, stud_answer, auto_mark, status, photo_pas, fio_stud, related_homeworks, related_materials, unlock_expire, communication_history, is_retake` |
| `homework_comment` | obj | `text_comment, attachment, attachment_path, date_updated` |
| `is_autotest`, `autotest_status`, `autotest_result`, `ospr`, `default_deadline_days`, `coins_to_close_deadline`, `completion_days`, `is_academic_holiday` | mixed | служебные |

Заголовков `X-Pagination-*` сервер **не отдаёт** — пагинация только через `_meta`.

---

## 3. Детали задания

**`GET /od/homework/{id}`**

Форма ответа — **не массив**, а объект с ключом-строкой `"0"`:

```json
{ "0": { ...поля задания... }, "relatedHomeworks": [ ... ] }
```

Парсер обязан корректно принимать: объект с ключом `"0"`, массив из одного элемента и (на всякий случай) одиночный объект.

Поля внутри `"0"` совпадают с полями списка, **но `comment` здесь содержит реальный текст задания** (проверено на 10 заданиях: в списке пусто, в деталях 9–539 символов). Значит `homework_sync` обязан запрашивать детали — иначе текст задания теряется.

`relatedHomeworks[]`: `id_domzadstud, id_teach, id_stud, status, time, id_domzad, ospr, mark, nlenta, date_vizit, is_retake, auto_test_status, auto_test_response, unlock_expire, moved_from_inactive_teacher, created_at, updated_at, domZad{ id_domzad, material_link, id_teach, id_tgroups, id_spec, time, id_schedule, nlenta, date, ospr, dz_theme, deadline, is_autotest, public_material_uuid, spec{...} }`.

`updated_at` в `relatedHomeworks` — единственный кандидат на «время изменения», но относится к связанным сдачам, не к самому заданию. **Для самого задания поля изменения нет** → определение изменений по SHA-256 нормализованного JSON деталей (как в ТЗ).

---

## 4. Вложения

В JSON **нет** имени, MIME и размера файла — только URL.

| Поле | Смысл |
|---|---|
| `file_path` | основное вложение задания (есть у 53 из 60 заданий) |
| `cover_image` | картинка-обложка задания (не вложение) |
| `homework_stud.file_path` | файл, загруженный студентом (в наблюдённых данных `null`) |
| `homework_comment.attachment` / `attachment_path` | вложение к комментарию преподавателя |

Наблюдённые домены: `fs1.itstep.org`, `fs3.itstep.org`, `fsx1.itstep.org`.
Шаблон пути: `/api/v1/files/<opaque-token>`.

Скачивание **открытое, без авторизации**:

- `200`, `access-control-allow-origin: *`, `server: nginx`;
- `Content-Disposition: attachment; filename="DZ_2026_11_09.pdf"; filename*=utf-8''ДЗ_2026_11_09.pdf` → отсюда берём исходное имя (декодировать `filename*` по RFC 5987);
- `Content-Type` и `Content-Length` корректны (проверено: `application/pdf`, `...wordprocessingml.document`, `video/mp4`, `image/png`, `text/html`);
- `Last-Modified` присутствует не всегда;
- **`ETag` непригоден**: у разных файлов одинаковый sha1 пустой строки. Условные запросы по ETag использовать нельзя → только SHA-256 содержимого.

Вложение может быть **HTML-документом** (сгенерированная страница задания `Домашнє_завдання_...html`) — извлечение текста обязано это поддерживать.

---

## 5. Следствия для реализации

1. Токен хранить как plain text JWT + `expires_at` из `exp` (30 дней).
2. На `401`: сначала `POST /auth/refresh` с Bearer, при неудаче — `POST /auth/login`; один повтор исходного запроса.
3. `homework_sync` всегда тянет `homework/{id}` (в списке нет текста задания).
4. Allowlist файлов: HTTPS + host заканчивается на `.itstep.org` + path начинается с `/api/v1/files/`.
5. Имя файла: `Content-Disposition` → `filename*` (UTF-8) → `filename` → имя из URL → `attachment_<n>`.
6. MIME: доверять `Content-Type`, но проверять magic bytes (PK\x03\x04, %PDF, \x89PNG, ftyp, <!DOCTYPE).
7. Change detection: SHA-256 нормализованного JSON деталей и SHA-256 содержимого файла; ETag игнорировать.
8. `status=3` — единственное наблюдённое значение; вынести в конфиг.
