# Google Drive MCP for GigaCowork

MCP-коннектор (Model Context Protocol) для Google Drive. Через него AI-ассистент или AI-платформа может искать, читать, скачивать, загружать и менять файлы на диске, а также работать с Google Docs и Google Sheets.

Поддерживает **stdio** (Claude Desktop) и **HTTP** (`streamable-http` / `sse`), так что его можно разместить на своём сервере и подключить к платформе по URL.

## Возможности (29 инструментов)

| Группа | Инструменты |
|---|---|
| Обзор и поиск | `get_drive_info`, `list_files`, `search_files`, `get_file_info` |
| Чтение | `read_file` — текст из Google Docs/Sheets/Slides, .docx, .xlsx, .pptx, .pdf, .txt, .csv, .json, .md (длинные файлы читаются по частям) |
| Скачивание | `download_file` — любой файл в base64; Google-файлы экспортируются в docx / xlsx / pptx / pdf / csv / md и т.д. |
| Загрузка и изменение | `upload_file` (с конвертацией в Google-формат или без), `update_file_content` (новая версия того же файла) |
| Управление | `create_folder`, `rename_file`, `move_file`, `copy_file`, `delete_file` (в корзину или навсегда), `restore_file` |
| Доступ | `share_file`, `list_permissions`, `remove_permission` |
| Google Docs | `docs_create` (из Markdown), `docs_append_text`, `docs_insert_text`, `docs_replace_text`, `docs_replace_many` (заполнение шаблонов) |
| Google Sheets | `sheets_create`, `sheets_get_info`, `sheets_read`, `sheets_write`, `sheets_append`, `sheets_clear`, `sheets_add_sheet` |

Общие диски (Shared Drives) поддерживаются во всех операциях. Через `GDRIVE_ROOT_FOLDER_ID` можно задать папку по умолчанию для поиска и загрузки.

---

## 1. Настройка Google Cloud (один раз)

1. Откройте <https://console.cloud.google.com/> и создайте проект (например, `gdrive-mcp`).
2. **APIs & Services → Library** — включите три API:
   - Google Drive API
   - Google Docs API
   - Google Sheets API
3. Дальше выберите способ авторизации.

### Вариант A — OAuth пользователя через браузер (рекомендуется)

Самый простой способ: коннектор сам проводит вход в Google и сохраняет токен в `token.json`.

1. **Google Auth Platform → Branding**: укажите Home page `https://<ваш-домен>/` и Privacy policy `https://<ваш-домен>/privacy` (эти страницы отдаёт сам коннектор), добавьте домен в Authorized domains.
2. **Audience → Publish app** (статус *In production*), иначе токен живёт 7 дней.
3. **Clients → Create client → Web application**, Authorized redirect URI: `https://<ваш-домен>/oauth/callback`.
4. В `.env`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI=https://<ваш-домен>/oauth/callback`, `MCP_AUTH_TOKEN`.
5. Владелец диска открывает `https://<ваш-домен>/oauth/start?token=<MCP_AUTH_TOKEN>`, входит в Google и нажимает «Продолжить». Появится «Google Drive подключён ✅».

### Вариант A2 — OAuth через скрипт на своём компьютере

1. **APIs & Services → OAuth consent screen** (или **Google Auth Platform**):
   - User type: **External** (или Internal, если у вас Google Workspace).
   - Заполните название приложения и email.
   - Scopes добавлять не обязательно.
   - **Важно:** переведите приложение в статус **In production** (Publish app). В статусе *Testing* refresh-токен **перестаёт работать через 7 дней**. Проходить верификацию Google для собственного использования не нужно: при входе просто появится предупреждение «Приложение не проверено» → *Дополнительно → Перейти*.
2. **Credentials → Create credentials → OAuth client ID** → тип **Desktop app**. Скачайте JSON и переименуйте в `client_secret.json`.
3. На своём компьютере:
   ```bash
   pip install google-auth-oauthlib
   python get_token.py client_secret.json
   ```
   Откроется браузер: войдите в аккаунт, к диску которого нужен доступ, и разрешите доступ. Скрипт выведет `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` и `GOOGLE_REFRESH_TOKEN` — добавьте их в `.env` на сервере.

### Вариант B — сервисный аккаунт (для общих дисков или Google Workspace)

1. **IAM & Admin → Service Accounts → Create** → затем **Keys → Add key → JSON**.
2. Откройте доступ к нужной папке или общему диску для email сервисного аккаунта (`...@...iam.gserviceaccount.com`).
3. Укажите путь к ключу в `GOOGLE_SERVICE_ACCOUNT_FILE` (или вставьте JSON целиком в `GOOGLE_SERVICE_ACCOUNT_JSON`).

> ⚠️ У сервисного аккаунта **нет своего места на диске**. Читать и править файлы в расшаренной папке он может, но **загружать новые файлы** в обычный «Мой диск» не получится (ошибка *storage quota exceeded*). Загрузка работает только в **общий диск (Shared Drive)**. Если общих дисков нет, используйте вариант A.

---

## 2. Запуск локально (Claude Desktop, stdio)

```bash
git clone https://github.com/otstoyotstoy/GoogleDrive-MCP-for-GigaCowork.git gdrive-mcp
cd gdrive-mcp
pip install -r requirements.txt
```

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gdrive": {
      "command": "python",
      "args": ["C:/path/to/gdrive-mcp/server.py"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "GOOGLE_CLIENT_ID": "...",
        "GOOGLE_CLIENT_SECRET": "...",
        "GOOGLE_REFRESH_TOKEN": "..."
      }
    }
  }
}
```

---

## 3. Деплой на VPS (HTTP, для подключения к AI-платформе)

### Вариант 1: systemd

```bash
sudo mkdir -p /opt/gdrive-mcp && cd /opt/gdrive-mcp
sudo git clone https://github.com/otstoyotstoy/GoogleDrive-MCP-for-GigaCowork.git .
sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env      # заполнить ключи Google и MCP_AUTH_TOKEN
openssl rand -hex 32                              # сгенерировать MCP_AUTH_TOKEN
sudo chown -R www-data:www-data /opt/gdrive-mcp
sudo cp deploy/gdrive-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gdrive-mcp
curl http://127.0.0.1:8000/health                 # {"status":"ok"}
```

### Вариант 1б: виртуальный хостинг с Passenger (reg.ru, ispmanager)

1. В ispmanager создайте сайт-поддомен, в его настройках включите **CGI-скрипты → Python 3.12** и выпустите сертификат Let's Encrypt.
2. Положите код в `~/gdrive-mcp`, создайте venv: `/opt/python/python-3.12/bin/python3 -m venv venv && venv/bin/pip install -r requirements.txt`, заполните `.env`.
3. В папку сайта положите `passenger_wsgi.py`:
   ```python
   import sys, runpy
   sys.path.insert(0, "/var/www/<user>/data/gdrive-mcp/venv/lib/python3.12/site-packages")
   application = runpy.run_path("/var/www/<user>/data/gdrive-mcp/passenger_wsgi.py")["application"]
   ```
4. Перезапуск после изменений: `touch ~/www/<сайт>/tmp/restart.txt`.

### Вариант 2: Docker

```bash
cp .env.example .env && nano .env
docker compose up -d --build
```

### nginx + HTTPS

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/gdrive-mcp   # заменить gdrive.example.ru на свой домен
sudo ln -s /etc/nginx/sites-available/gdrive-mcp /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d gdrive.example.ru
```

В `.env` укажите `MCP_ALLOWED_HOSTS=gdrive.example.ru`.

### Подключение к GigaCowork / другой MCP-платформе

| Параметр | Значение |
|---|---|
| URL (streamable-http) | `https://gdrive.example.ru/mcp` |
| URL (sse, если платформа поддерживает только SSE) | `https://gdrive.example.ru/sse` и `MCP_TRANSPORT=sse` |
| Авторизация | заголовок `Authorization: Bearer <MCP_AUTH_TOKEN>` |
| Если платформа не умеет заголовки | `https://gdrive.example.ru/mcp?token=<MCP_AUTH_TOKEN>` |

Также принимается заголовок `X-API-Key: <MCP_AUTH_TOKEN>`.

> **GigaCowork:** если при создании интеграции появляется ошибка `server responded with HTTP 401`, платформа не передала токен. Самый надёжный вариант — указать токен прямо в MCP URL (`.../mcp?token=...`) и выбрать «без авторизации».

### Формулы в Google Таблицах
Формулы записываются в синтаксисе локали таблицы. Для русской локали (`ru_RU`) разделитель аргументов — `;` (`=SUMIFS(A:A;B:B;"Закрыта")`). Подсказка об этом уже есть в описании инструментов `sheets_write` / `sheets_append`.

---

## Переменные окружения

| Переменная | Описание |
|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | OAuth пользователя (вариант A) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` / `GOOGLE_SERVICE_ACCOUNT_JSON` | сервисный аккаунт (вариант B) |
| `GOOGLE_IMPERSONATE_USER` | Workspace: от чьего имени действовать (domain-wide delegation) |
| `GOOGLE_OAUTH_REDIRECT_URI` | адрес возврата для входа через браузер (`https://<домен>/oauth/callback`) |
| `GDRIVE_TOKEN_FILE` | где хранить refresh-токен после входа через браузер (по умолчанию `token.json`) |
| `GDRIVE_ROOT_FOLDER_ID` | папка по умолчанию для списка, поиска и загрузки |
| `MCP_TRANSPORT` | `stdio` / `streamable-http` / `sse` |
| `MCP_HOST`, `MCP_PORT` | адрес и порт HTTP-сервера (по умолчанию `0.0.0.0:8000`) |
| `MCP_AUTH_TOKEN` | секрет для доступа к коннектору по HTTP (**обязательно задать на сервере**) |
| `MCP_ALLOWED_HOSTS` | домены через запятую: включает защиту от DNS rebinding |
| `MCP_STATELESS` | `true` (по умолчанию): каждый запрос независим, удобно за прокси |
| `GDRIVE_MAX_DOWNLOAD_MB` / `GDRIVE_MAX_UPLOAD_MB` | лимиты размера файлов (20 / 50 МБ) |
| `GDRIVE_MAX_TEXT_CHARS` | максимум символов, которые `read_file` отдаёт за один вызов |

## Примеры запросов к ассистенту

- «Найди на диске договоры за ноябрь и прочитай последний»
- «Загрузи этот отчёт в папку "Клиенты/Ромашка" как Google Doc»
- «В таблице "Продажи 2026" добавь строку: 17.11, ООО Ромашка, 150 000»
- «Возьми шаблон договора, сделай копию и подставь реквизиты клиента вместо {{...}}»
- «Дай доступ на редактирование к папке "Проект X" для ivan@company.ru»

## Безопасность

- Никогда не коммитьте `.env`, `client_secret.json` и ключи сервисного аккаунта (они уже в `.gitignore`).
- На сервере обязательно задайте `MCP_AUTH_TOKEN` и работайте только через HTTPS.
- Чтобы жёстко ограничить доступ одной папкой, используйте сервисный аккаунт и откройте ему только эту папку. `GDRIVE_ROOT_FOLDER_ID` задаёт только папку по умолчанию и не запрещает доступ к другим файлам по ID.
- `delete_file` по умолчанию перемещает файлы в корзину; безвозвратное удаление делается только с `permanent=true`.
