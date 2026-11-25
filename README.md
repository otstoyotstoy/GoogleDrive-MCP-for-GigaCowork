# Google Drive MCP for GigaCowork

MCP-коннектор (Model Context Protocol) к Google Drive для GigaCowork и других AI-платформ. Через него AI-ассистент ищет, читает, загружает и редактирует файлы, Google Документы и Google Таблицы клиента.

Работает по **HTTP** (`streamable-http` / `sse`) на сервере клиента и по **stdio** (Claude Desktop).

> Версия 2.0. Главный принцип: коннектор **не может ничего необратимо удалить, затереть или открыть посторонним** без явного разрешения в настройках и подтверждения пользователя. Всё это покрыто автотестами (`tests/`).

---

## Возможности (29 инструментов)

| Группа | Инструменты |
|---|---|
| Обзор и поиск | `get_drive_info`, `list_files`, `search_files` (в т.ч. «Доступные мне»), `get_file_info` |
| Чтение | `read_file` — текст из Google Docs/Sheets/Slides, .docx, .xlsx, .pptx, .pdf, .txt, .csv, .json, .md; `download_file` — файл в base64 |
| Создание | `upload_file`, `create_folder`, `copy_file`, `docs_create`, `sheets_create`, `sheets_add_sheet` |
| Изменение | `rename_file`, `move_file`, `update_file_content`*, `restore_file` |
| Google Docs | `docs_replace_text`, `docs_replace_many` (заполнение шаблонов), `docs_insert_text`, `docs_append_text` |
| Google Sheets | `sheets_get_info`, `sheets_read`, `sheets_write`*, `sheets_append`, `sheets_clear`* |
| Удаление и доступ | `delete_file`*, `share_file`*, `list_permissions`, `remove_permission`* |

\* — опасные действия: требуют подтверждения пользователя (см. ниже).

---

## Безопасность: что защищено

| Риск | Как защищено |
|---|---|
| Безвозвратное удаление | Запрещено по умолчанию. Даже если включить — только для файлов, уже лежащих в корзине, и с подтверждением. |
| Удаление файла | Только в корзину (обратимо 30 дней, `restore_file`), только после подтверждения пользователя. |
| Удаление папки со всем содержимым | Запрещено по умолчанию. Если включить — модель сначала показывает, сколько файлов внутри. |
| Удаление корневой рабочей папки | Запрещено всегда. |
| Перезапись Google Документа/Таблицы целиком (стёрла бы листы, формулы, форматирование) | Запрещена всегда — только точечные `docs_*` / `sheets_*`. |
| Перезапись обычного файла (docx, pdf…) | Подтверждение + предыдущая версия закрепляется в истории версий Google **навсегда**. |
| Затирание заполненных ячеек таблицы | Запись в пустые ячейки — сразу; поверх данных — только с подтверждением, старые значения и формулы сохраняются в журнал. `sheets_append` никогда не затирает. |
| Очистка таблицы | Только конкретного диапазона (не целого листа), с подтверждением и копией в журнал. |
| Сломанные формулы (разделитель `,` вместо `;` в русской локали) | После записи коннектор сам проверяет ячейки и сообщает модели об ошибках формул. |
| Удаление/замена текста в документе | Пустая замена (= удаление) и замена >20 мест — только с подтверждением; слишком короткие фрагменты (1 символ) отклоняются. Защита от одновременной правки (revision check). |
| Утечка: доступ «всем по ссылке» | Запрещён по умолчанию. |
| Передача владения файлом | Запрещена всегда. |
| Выдача / отзыв доступа | С подтверждением; доступ владельца отозвать нельзя. |
| Доступ ко всему диску | Можно жёстко ограничить папками: `GDRIVE_ALLOWED_FOLDER_IDS`. |
| Посторонние запросы к коннектору | Без `MCP_AUTH_TOKEN` (мин. 24 символа) сервер по HTTP **не запускается**. Сравнение токена — защищённое от подбора по времени. |
| Сбои при параллельных запросах | Отдельный клиент Google API на каждый поток. |
| Временные ошибки Google (429, 5xx), обрывы | Автоповтор с паузой — только для операций, повтор которых ничего не задвоит; таймауты 60 с. |
| Истёк/отозван доступ к Google | Понятная ошибка со ссылкой на переподключение, без падения сервера. |
| Неожиданные изменения при обновлении библиотек | Версии зафиксированы (`requirements.lock`). |

**Как работает подтверждение.** Опасный инструмент без `confirm=true` ничего не делает и возвращает `needs_confirmation` с описанием действия. Модель обязана пересказать это пользователю и повторить вызов с `confirm=true` только после его согласия. Платформы, понимающие MCP-аннотации, дополнительно видят у этих инструментов пометку `destructiveHint`.

**Режим «только чтение»:** `GDRIVE_READ_ONLY=true` — для клиентов, которым нужна только аналитика/отчёты.

---

## Что получить от клиента перед развёртыванием

- [ ] Доступ к облачному серверу (SSH) или к панели хостинга. Нужен Python 3.10+ (рекомендуется 3.12) или Docker.
- [ ] Домен/поддомен для коннектора (например `gdrive.company.ru`), DNS A-запись на сервер.
- [ ] Google-аккаунт, к диску которого подключаемся (или админ Google Workspace — для сервисного аккаунта).
- [ ] Решение по безопасности: весь диск или только определённые папки (ID папок); нужна ли запись или только чтение; можно ли удалять в корзину; можно ли выдавать доступ.
- [ ] Email для страницы политики конфиденциальности.

---

## 1. Настройка Google Cloud (один раз на клиента)

1. <https://console.cloud.google.com/> → создать проект.
2. **APIs & Services → Library** — включить **Google Drive API**, **Google Docs API**, **Google Sheets API**.
3. **Google Auth Platform → Branding**: название, email поддержки; Home page `https://<домен>/`, Privacy policy `https://<домен>/privacy` (эти страницы отдаёт сам коннектор); в Authorized domains — домен.
4. **Audience**: тип External (или Internal для Workspace) → **Publish app** (статус *In production*). В статусе *Testing* токен перестаёт работать через 7 дней.
5. **Clients → Create client → Web application**, Authorized redirect URI: `https://<домен>/oauth/callback`. Сохранить Client ID и Client secret.

При входе Google покажет «Приложение не проверено» — для собственного использования это нормально: «Дополнительные настройки» → «Перейти…».

**Сервисный аккаунт** (вместо OAuth) — для общих дисков/Workspace: IAM → Service Accounts → ключ JSON → открыть ему доступ к нужным папкам → `GOOGLE_SERVICE_ACCOUNT_FILE`. У сервисного аккаунта нет своего места на диске: загрузка новых файлов работает только в общие диски.

---

## 2. Развёртывание

### Вариант A: сервер с systemd (рекомендуется)

```bash
sudo useradd -r -m -d /opt/gdrive-mcp gdrive-mcp
sudo -u gdrive-mcp git clone https://github.com/otstoyotstoy/GoogleDrive-MCP-for-GigaCowork.git /opt/gdrive-mcp/app
cd /opt/gdrive-mcp/app
sudo -u gdrive-mcp python3 -m venv venv
sudo -u gdrive-mcp venv/bin/pip install -r requirements.lock -r requirements-dev.txt
sudo -u gdrive-mcp venv/bin/python -m pytest -q tests      # должно быть: all passed
sudo -u gdrive-mcp cp .env.example .env && sudo -u gdrive-mcp nano .env
sudo chmod 600 .env
```

Затем установите службу:

```bash
sudo cp deploy/gdrive-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gdrive-mcp
curl http://127.0.0.1:8000/health
```

nginx + HTTPS: `deploy/nginx.conf` (замените домен) → `sudo certbot --nginx -d <домен>`. В `.env` укажите `MCP_ALLOWED_HOSTS=<домен>`.

### Вариант B: Docker

```bash
cp .env.example .env && nano .env          # MCP_HOST оставьте как есть — в контейнере он переопределён
mkdir -p data logs && sudo chown -R 10001:10001 data logs   # контейнер работает не от root — дайте ему право писать токен и журнал
docker compose up -d --build
curl http://127.0.0.1:8000/health
```
Токен Google хранится в `./data`, журнал — в `./logs` (не теряются при пересборке).

### Вариант C: виртуальный хостинг с Passenger (reg.ru / ispmanager)

1. Создайте сайт-поддомен, в его настройках включите **CGI-скрипты → Python 3.12**, выпустите сертификат Let's Encrypt.
2. Код — в `~/gdrive-mcp`: `/opt/python/python-3.12/bin/python3 -m venv venv && venv/bin/pip install -r requirements.lock`, заполните `.env`.
3. В папку сайта положите `passenger_wsgi.py`:
   ```python
   import sys, runpy
   sys.path.insert(0, "/var/www/<user>/data/gdrive-mcp/venv/lib/python3.12/site-packages")
   application = runpy.run_path("/var/www/<user>/data/gdrive-mcp/passenger_wsgi.py")["application"]
   ```
4. Перезапуск: `touch ~/www/<сайт>/tmp/restart.txt`.

### Подключение Google-аккаунта

Откройте `https://<домен>/oauth/start?token=<MCP_AUTH_TOKEN>`, войдите в Google-аккаунт клиента, отметьте все разрешения → «Продолжить». Появится «Google Drive подключён ✅». Токен сохраняется в `token.json` (права 600).

---

## 3. Подключение к GigaCowork

| Поле | Значение |
|---|---|
| MCP URL | `https://<домен>/mcp?token=<MCP_AUTH_TOKEN>` |
| Авторизация | «Без авторизации» (токен уже в URL) |

Либо URL `https://<домен>/mcp` + заголовок `Authorization: Bearer <MCP_AUTH_TOKEN>` (также принимается `X-API-Key`). Если GigaCowork пишет `server responded with HTTP 401` — платформа не передала токен: используйте вариант с токеном в URL.

---

## 4. Проверка после запуска (5 минут)

1. `curl https://<домен>/health` → `{"status":"ok","version":"2.0.1"}`.
2. Без токена `https://<домен>/mcp` → `401`.
3. В платформе: «покажи информацию о моём Google Диске» → в ответе `safety_settings` совпадают с задуманными.
4. «Найди файлы со словом …» → список файлов.
5. «Удали файл X» → ассистент должен **спросить подтверждение** (а не удалить сразу).
6. Проверить журнал: `tail logs/audit.log`.

---

## Настройки (`.env`)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `MCP_AUTH_TOKEN` | — | Секрет доступа к коннектору (обязателен для HTTP, ≥24 символов): `openssl rand -hex 32` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | — | OAuth-клиент (Web application) |
| `GOOGLE_OAUTH_REDIRECT_URI` | `https://<host>/oauth/callback` | Адрес возврата после входа |
| `GOOGLE_REFRESH_TOKEN` | — | Альтернатива входу через браузер |
| `GOOGLE_SERVICE_ACCOUNT_FILE` / `_JSON` | — | Сервисный аккаунт |
| `GDRIVE_READ_ONLY` | `false` | Только чтение |
| `GDRIVE_REQUIRE_CONFIRM` | `true` | Подтверждение опасных действий (не отключайте) |
| `GDRIVE_ALLOW_DELETE` | `true` | Удаление в корзину |
| `GDRIVE_ALLOW_FOLDER_DELETE` | `false` | Удаление папок целиком |
| `GDRIVE_ALLOW_PERMANENT_DELETE` | `false` | Безвозвратное удаление (только из корзины) |
| `GDRIVE_ALLOW_OVERWRITE` | `true` | Полная замена обычных файлов (с сохранением старой версии) |
| `GDRIVE_ALLOW_SHARING` / `GDRIVE_ALLOW_REVOKE` | `true` | Выдача / отзыв доступа по email |
| `GDRIVE_ALLOW_PUBLIC_SHARING` | `false` | Доступ «всем по ссылке» |
| `GDRIVE_ALLOWED_FOLDER_IDS` | пусто (весь диск) | Жёсткое ограничение папками (ID через запятую) |
| `GDRIVE_ROOT_FOLDER_ID` | — | Папка по умолчанию (не ограничение) |
| `GDRIVE_AUDIT_LOG` | `logs/audit.log` | Журнал изменений (ротация 10×10 МБ) |
| `GDRIVE_TOKEN_FILE` | `token.json` | Где хранить токен Google |
| `MCP_TRANSPORT` | `stdio` | `stdio` / `streamable-http` / `sse` |
| `MCP_HOST`, `MCP_PORT` | `127.0.0.1:8000` | Адрес HTTP-сервера (за nginx) |
| `MCP_ALLOWED_HOSTS` | — | Домены — защита от DNS rebinding |
| `GDRIVE_MAX_DOWNLOAD_MB` / `_READ_MB` / `_UPLOAD_MB` | 20 / 50 / 50 | Лимиты размеров |
| `GDRIVE_MAX_SHEET_CELLS` | 20000 | Максимум ячеек за одну запись |
| `GDRIVE_HTTP_TIMEOUT`, `GDRIVE_RETRIES` | 60, 4 | Таймаут и число повторов запросов к Google |

---

## Журнал и восстановление данных

Каждое изменение и каждая заблокированная попытка пишутся в `logs/audit.log` (одна строка JSON): время, инструмент, файл, результат.

- **Файл удалён** → он в корзине Google Диска 30 дней: попросите ассистента `restore_file` или восстановите в интерфейсе Диска.
- **Затёрты ячейки таблицы** → в журнале запись `"status":"backup"` с прежними значениями и формулами (`previous_values`); также «Файл → История версий» в Google Таблицах.
- **Испорчен Google Документ** → «Файл → История версий»; в журнале есть `revision_before`.
- **Перезаписан обычный файл** → «Управление версиями» на Диске; прежняя версия закреплена навсегда (`previous_revision_kept`).

---

## Обновление коннектора у клиента (без риска)

```bash
cd /opt/gdrive-mcp/app
git fetch && git log --oneline HEAD..origin/main      # что изменилось
cp -a . ../app-backup-$(date +%F)                      # резервная копия текущей версии вместе с .env и token.json
git pull
venv/bin/pip install -r requirements.lock -r requirements-dev.txt
venv/bin/python -m pytest -q tests                      # только если all passed — продолжаем
sudo systemctl restart gdrive-mcp && curl http://127.0.0.1:8000/health
```

Откат: `sudo systemctl stop gdrive-mcp && rm -rf /opt/gdrive-mcp/app && mv /opt/gdrive-mcp/app-backup-<дата> /opt/gdrive-mcp/app && sudo systemctl start gdrive-mcp`.

Правила совместимости для разработчиков: названия инструментов и их параметры не переименовываются и не удаляются (только добавляются новые необязательные параметры); защиты не ослабляются по умолчанию; любое изменение — вместе с тестом; версии библиотек обновляются только с прогоном тестов и новым `requirements.lock`. См. `CHANGELOG.md`.

---

## Тесты

```bash
pip install -r requirements.lock -r requirements-dev.txt
python -m pytest -q tests
```

Тесты работают на имитации Google Drive/Docs/Sheets в памяти (без доступа к настоящему диску) и проверяют: что без подтверждения ничего не удаляется и не затирается; режим только чтения; запрет публичных ссылок и передачи владения; ограничение папками; резервные копии в журнале; авторизацию HTTP; работу по MCP-протоколу и через WSGI (Passenger); потокобезопасность.

---

## Частые проблемы

| Симптом | Причина / решение |
|---|---|
| `HTTP 401` при подключении в платформе | Токен не передан → используйте `.../mcp?token=...` |
| «Google-аккаунт не подключён» / «Доступ отозван или истёк» | Откройте `/oauth/start?token=...` и войдите заново |
| Формулы показывают `#ERROR!` | Локаль ru_RU: разделитель `;`. Коннектор сообщает об этом модели после записи |
| `403 insufficient permissions` | У подключённого аккаунта на файл права «Читатель» — нужен «Редактор» |
| `storage quota` при загрузке | Сервисный аккаунт без места — используйте OAuth или общий диск |
| Сервер не стартует: «MCP_AUTH_TOKEN не задан» | Это защита: задайте токен ≥24 символов |
