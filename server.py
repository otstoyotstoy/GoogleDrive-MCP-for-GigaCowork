"""
Google Drive MCP Server

MCP-коннектор для Google Drive: поиск, чтение, скачивание, загрузка и
изменение файлов, работа с Google Docs и Google Sheets.

Транспорты:
  - stdio            — для Claude Desktop и локального запуска
  - streamable-http  — для хостинга (VPS) и подключения к AI-платформам по URL
  - sse              — устаревший HTTP-транспорт для старых клиентов
"""

import asyncio
import base64
import csv
import io
import json
import os
import mimetypes
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

load_dotenv()

# ===================== КОНФИГУРАЦИЯ =====================

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Вариант 1: OAuth пользователя (рекомендуется для обычного "Мой диск")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

# Вариант 2: сервисный аккаунт (подходит для общих дисков / Shared Drives)
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
# Для Google Workspace: от чьего имени действует сервисный аккаунт (domain-wide delegation)
GOOGLE_IMPERSONATE_USER = os.environ.get("GOOGLE_IMPERSONATE_USER", "")

# Ограничить работу коннектора одной папкой (необязательно)
GDRIVE_ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "")

# Сервер
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
MCP_ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

# Лимиты
MAX_TEXT_CHARS = int(os.environ.get("GDRIVE_MAX_TEXT_CHARS", "200000"))
MAX_DOWNLOAD_MB = float(os.environ.get("GDRIVE_MAX_DOWNLOAD_MB", "20"))
MAX_UPLOAD_MB = float(os.environ.get("GDRIVE_MAX_UPLOAD_MB", "50"))

FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIME = "application/vnd.google-apps.document"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
GSLIDES_MIME = "application/vnd.google-apps.presentation"
GDRAWING_MIME = "application/vnd.google-apps.drawing"

# В какой формат выгружать Google-файлы при чтении как текст
EXPORT_TEXT = {
    GDOC_MIME: "text/markdown",
    GSHEET_MIME: "text/csv",
    GSLIDES_MIME: "text/plain",
    GDRAWING_MIME: "image/svg+xml",
}

# В какой формат выгружать Google-файлы при скачивании
EXPORT_BINARY = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "pdf": ("application/pdf", ".pdf"),
    "txt": ("text/plain", ".txt"),
    "md": ("text/markdown", ".md"),
    "csv": ("text/csv", ".csv"),
    "html": ("text/html", ".html"),
    "odt": ("application/vnd.oasis.opendocument.text", ".odt"),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", ".ods"),
    "png": ("image/png", ".png"),
    "svg": ("image/svg+xml", ".svg"),
}
DEFAULT_EXPORT = {GDOC_MIME: "docx", GSHEET_MIME: "xlsx", GSLIDES_MIME: "pptx", GDRAWING_MIME: "png"}

# В какой Google-формат конвертировать при загрузке (convert_to_google=True)
CONVERT_ON_UPLOAD = {
    "text/plain": GDOC_MIME,
    "text/markdown": GDOC_MIME,
    "text/html": GDOC_MIME,
    "application/msword": GDOC_MIME,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": GDOC_MIME,
    "application/rtf": GDOC_MIME,
    "application/vnd.oasis.opendocument.text": GDOC_MIME,
    "text/csv": GSHEET_MIME,
    "application/vnd.ms-excel": GSHEET_MIME,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": GSHEET_MIME,
    "application/vnd.oasis.opendocument.spreadsheet": GSHEET_MIME,
    "application/vnd.ms-powerpoint": GSLIDES_MIME,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": GSLIDES_MIME,
}

FILE_FIELDS = (
    "id,name,mimeType,size,createdTime,modifiedTime,parents,webViewLink,"
    "owners(displayName,emailAddress),lastModifyingUser(displayName,emailAddress),"
    "shared,trashed,description,starred,driveId"
)
LIST_FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime,parents,webViewLink,driveId)"

mimetypes.add_type("text/markdown", ".md")


# ===================== АВТОРИЗАЦИЯ GOOGLE =====================

_creds = None
_services: dict[str, Any] = {}


def get_credentials():
    global _creds
    if _creds is not None:
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds

    import oauth_web
    refresh_token = GOOGLE_REFRESH_TOKEN or oauth_web.load_refresh_token()
    if refresh_token and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        _creds = UserCredentials(
            token=None,
            refresh_token=refresh_token,
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
    elif GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE:
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
            _creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            _creds = service_account.Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        if GOOGLE_IMPERSONATE_USER:
            _creds = _creds.with_subject(GOOGLE_IMPERSONATE_USER)
    else:
        raise RuntimeError(
            "Не настроена авторизация Google. Укажите GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + "
            "GOOGLE_REFRESH_TOKEN (OAuth) либо GOOGLE_SERVICE_ACCOUNT_FILE / GOOGLE_SERVICE_ACCOUNT_JSON."
        )
    _creds.refresh(Request())
    return _creds


def svc(name: str):
    """Ленивая инициализация клиентов Google API: drive, docs, sheets."""
    version = {"drive": "v3", "docs": "v1", "sheets": "v4"}[name]
    if name not in _services:
        _services[name] = build(name, version, credentials=get_credentials(), cache_discovery=False)
    else:
        get_credentials()  # обновит токен, если истёк
    return _services[name]


async def run(fn, *args, **kwargs):
    """Google API клиент синхронный — выполняем в отдельном потоке, чтобы не блокировать сервер."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def out(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def err(e: Exception) -> str:
    if isinstance(e, HttpError):
        try:
            detail = json.loads(e.content.decode()).get("error", {})
            msg = detail.get("message", str(e))
        except Exception:
            msg = str(e)
        hint = ""
        if "storage quota" in msg.lower():
            hint = (" Подсказка: у сервисного аккаунта нет своего места на диске — загружайте в общий диск "
                    "(Shared Drive) или используйте OAuth-авторизацию пользователя.")
        return out({"error": True, "status": e.resp.status, "message": msg + hint})
    return out({"error": True, "message": str(e)})


def common_kwargs() -> dict:
    # Поддержка общих дисков (Shared Drives) во всех запросах
    return {"supportsAllDrives": True}


def default_parent(folder_id: str) -> str:
    return folder_id or GDRIVE_ROOT_FOLDER_ID


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def human_size(size: Any) -> str:
    try:
        n = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ===================== ВНУТРЕННИЕ ОПЕРАЦИИ С DRIVE =====================

def _get_meta(file_id: str, fields: str = FILE_FIELDS) -> dict:
    return svc("drive").files().get(fileId=file_id, fields=fields, **common_kwargs()).execute()


def _download_bytes(file_id: str) -> bytes:
    req = svc("drive").files().get_media(fileId=file_id, **common_kwargs())
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req, chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
        if buf.tell() > MAX_DOWNLOAD_MB * 1024 * 1024 * 5:
            raise RuntimeError("Файл слишком большой для обработки")
    return buf.getvalue()


def _export_bytes(file_id: str, mime: str) -> bytes:
    return svc("drive").files().export(fileId=file_id, mimeType=mime).execute()


def _extract_text(data: bytes, mime: str, name: str) -> str:
    """Достаёт текст из офисных форматов, PDF и текстовых файлов."""
    lname = name.lower()
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or lname.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for t_i, table in enumerate(d.tables, 1):
            parts.append(f"\n[Таблица {t_i}]")
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)
    if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or lname.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        parts = []
        for ws in wb.worksheets:
            parts.append(f"### Лист: {ws.title}")
            buf = io.StringIO()
            w = csv.writer(buf)
            for row in ws.iter_rows(values_only=True):
                if row is None or all(v is None for v in row):
                    continue
                w.writerow(["" if v is None else v for v in row])
            parts.append(buf.getvalue())
        return "\n".join(parts)
    if mime == "application/vnd.openxmlformats-officedocument.presentationml.presentation" or lname.endswith(".pptx"):
        try:
            from pptx import Presentation
        except ImportError:
            raise RuntimeError("Для чтения .pptx установите python-pptx")
        prs = Presentation(io.BytesIO(data))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            parts.append(f"--- Слайд {i} ---")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    parts.append(shape.text_frame.text)
        return "\n".join(parts)
    if mime == "application/pdf" or lname.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(f"--- Страница {i} ---\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages, 1))
    if mime.startswith("text/") or mime in ("application/json", "application/xml", "application/javascript",
                                             "application/x-yaml", "application/sql") \
            or lname.endswith((".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".html", ".log", ".py", ".js", ".sql")):
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
    raise RuntimeError(f"Не умею извлекать текст из файла типа {mime}. Используйте download_file.")


def _truncate(text: str, offset: int, max_chars: int) -> dict:
    total = len(text)
    max_chars = min(max_chars or MAX_TEXT_CHARS, MAX_TEXT_CHARS)
    chunk = text[offset: offset + max_chars]
    res = {"total_chars": total, "offset": offset, "returned_chars": len(chunk), "content": chunk}
    if offset + len(chunk) < total:
        res["truncated"] = True
        res["next_offset"] = offset + len(chunk)
    return res


def _decode_content(content: str, is_base64: bool) -> bytes:
    data = base64.b64decode(content) if is_base64 else content.encode("utf-8")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise RuntimeError(f"Файл больше лимита {MAX_UPLOAD_MB} МБ")
    return data


# ===================== MCP-СЕРВЕР =====================

transport_security = None
if MCP_ALLOWED_HOSTS:
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=MCP_ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in MCP_ALLOWED_HOSTS] + [f"http://{h}" for h in MCP_ALLOWED_HOSTS],
    )
elif MCP_TRANSPORT != "stdio":
    transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

mcp = FastMCP(
    "Google Drive",
    instructions=(
        "Коннектор к Google Drive. Ищите файлы через search_files или list_files, "
        "читайте содержимое через read_file, скачивайте через download_file, "
        "загружайте через upload_file. Для правки Google Docs используйте docs_* инструменты, "
        "для Google Sheets — sheets_* инструменты. ID файла берите из результатов поиска."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    max_request_body_size=int(MAX_UPLOAD_MB * 1.4 * 1024 * 1024) + 1024 * 1024,
    stateless_http=os.environ.get("MCP_STATELESS", "true").lower() == "true",
    json_response=os.environ.get("MCP_JSON_RESPONSE", "false").lower() == "true",
    transport_security=transport_security,
)


# ===================== ОБЗОР / ПОИСК =====================

@mcp.tool()
async def get_drive_info() -> str:
    """Информация о подключённом Google-аккаунте: пользователь, занятое и доступное место, список общих дисков."""
    try:
        def _do():
            about = svc("drive").about().get(fields="user(displayName,emailAddress),storageQuota").execute()
            drives = svc("drive").drives().list(pageSize=50, fields="drives(id,name)").execute().get("drives", [])
            q = about.get("storageQuota", {})
            return {
                "user": about.get("user"),
                "storage": {k: human_size(v) for k, v in q.items()},
                "shared_drives": drives,
                "root_folder_restriction": GDRIVE_ROOT_FOLDER_ID or None,
            }
        return out(await run(_do))
    except Exception as e:
        return err(e)


@mcp.tool()
async def list_files(folder_id: str = "", page_size: int = 50, page_token: str = "",
                     order_by: str = "folder,modifiedTime desc", include_trashed: bool = False) -> str:
    """Список файлов и папок внутри папки. folder_id — ID папки (пусто = корень «Мой диск» или папка из GDRIVE_ROOT_FOLDER_ID; 'root' = корень).
    page_size — до 200. page_token — для следующей страницы (из nextPageToken).
    order_by — сортировка, например 'name', 'modifiedTime desc', 'folder,name'."""
    try:
        parent = default_parent(folder_id) or "root"
        q = f"'{esc(parent)}' in parents"
        if not include_trashed:
            q += " and trashed = false"

        def _do():
            return svc("drive").files().list(
                q=q, pageSize=min(page_size, 200), pageToken=page_token or None, orderBy=order_by,
                fields=LIST_FIELDS, includeItemsFromAllDrives=True, corpora="allDrives", **common_kwargs()
            ).execute()
        res = await run(_do)
        for f in res.get("files", []):
            f["is_folder"] = f["mimeType"] == FOLDER_MIME
            if "size" in f:
                f["size_human"] = human_size(f["size"])
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool()
async def search_files(name_contains: str = "", full_text: str = "", mime_type: str = "",
                       folder_id: str = "", modified_after: str = "", only_folders: bool = False,
                       raw_query: str = "", page_size: int = 50, page_token: str = "") -> str:
    """Поиск файлов на диске.
    name_contains — часть имени файла; full_text — поиск по содержимому файла;
    mime_type — фильтр по типу ('application/pdf', 'application/vnd.google-apps.document' — Google Doc,
    'application/vnd.google-apps.spreadsheet' — Google Sheet, 'application/vnd.google-apps.folder' — папка) или короткие псевдонимы:
    doc, sheet, slides, folder, pdf, image, docx, xlsx;
    folder_id — искать только в этой папке (непосредственные дети); modified_after — дата ISO, напр. '2026-01-01';
    raw_query — запрос на языке Drive API, если нужен сложный фильтр (заменяет остальные условия)."""
    aliases = {
        "doc": GDOC_MIME, "sheet": GSHEET_MIME, "slides": GSLIDES_MIME, "folder": FOLDER_MIME,
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    try:
        if raw_query:
            q = raw_query
        else:
            conds = ["trashed = false"]
            if name_contains:
                conds.append(f"name contains '{esc(name_contains)}'")
            if full_text:
                conds.append(f"fullText contains '{esc(full_text)}'")
            if only_folders:
                mime_type = FOLDER_MIME
            if mime_type == "image":
                conds.append("mimeType contains 'image/'")
            elif mime_type:
                conds.append(f"mimeType = '{esc(aliases.get(mime_type, mime_type))}'")
            parent = folder_id or GDRIVE_ROOT_FOLDER_ID
            if parent:
                conds.append(f"'{esc(parent)}' in parents")
            if modified_after:
                ts = modified_after if "T" in modified_after else modified_after + "T00:00:00"
                conds.append(f"modifiedTime > '{esc(ts)}'")
            q = " and ".join(conds)

        def _do():
            kw = dict(q=q, pageSize=min(page_size, 200), pageToken=page_token or None,
                      fields=LIST_FIELDS, includeItemsFromAllDrives=True, corpora="allDrives", **common_kwargs())
            if not full_text:
                kw["orderBy"] = "modifiedTime desc"
            return svc("drive").files().list(**kw).execute()
        res = await run(_do)
        res["query"] = q
        for f in res.get("files", []):
            if "size" in f:
                f["size_human"] = human_size(f["size"])
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool()
async def get_file_info(file_id: str) -> str:
    """Подробная информация о файле или папке по ID: имя, тип, размер, владелец, даты, ссылка, родительские папки."""
    try:
        meta = await run(_get_meta, file_id)
        if "size" in meta:
            meta["size_human"] = human_size(meta["size"])
        return out(meta)
    except Exception as e:
        return err(e)


# ===================== ЧТЕНИЕ / СКАЧИВАНИЕ =====================

@mcp.tool()
async def read_file(file_id: str, offset: int = 0, max_chars: int = 50000) -> str:
    """Прочитать содержимое файла как текст.
    Google Docs → Markdown, Google Sheets → CSV (первый лист; для всех листов используйте sheets_read),
    Google Slides → текст, .docx/.xlsx/.pptx/.pdf/.txt/.csv/.json/.md → извлечённый текст.
    Для длинных файлов используйте offset и max_chars (в ответе есть next_offset)."""
    try:
        def _do():
            meta = _get_meta(file_id, "id,name,mimeType,size,modifiedTime,webViewLink")
            mime = meta["mimeType"]
            if mime == FOLDER_MIME:
                raise RuntimeError("Это папка. Используйте list_files.")
            if mime in EXPORT_TEXT:
                data = _export_bytes(file_id, EXPORT_TEXT[mime])
                text = data.decode("utf-8", errors="replace")
            elif mime.startswith("application/vnd.google-apps"):
                raise RuntimeError(f"Тип {mime} нельзя прочитать как текст")
            else:
                size = int(meta.get("size", 0))
                if size > MAX_DOWNLOAD_MB * 1024 * 1024 * 5:
                    raise RuntimeError(f"Файл слишком большой ({human_size(size)}) для чтения")
                text = _extract_text(_download_bytes(file_id), mime, meta["name"])
            return meta, text
        meta, text = await run(_do)
        res = {"file": meta}
        res.update(_truncate(text, offset, max_chars))
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool()
async def download_file(file_id: str, export_format: str = "") -> str:
    """Скачать файл целиком в base64 (для бинарных файлов: картинки, PDF, архивы, офисные документы).
    Google-файлы экспортируются: export_format = docx | xlsx | pptx | pdf | txt | md | csv | html | odt | ods | png | svg
    (по умолчанию Docs→docx, Sheets→xlsx, Slides→pptx). Лимит размера задаётся GDRIVE_MAX_DOWNLOAD_MB.
    Если нужен только текст — используйте read_file, это экономнее."""
    try:
        def _do():
            meta = _get_meta(file_id, "id,name,mimeType,size")
            mime = meta["mimeType"]
            name = meta["name"]
            if mime == FOLDER_MIME:
                raise RuntimeError("Это папка, скачать её целиком нельзя. Используйте list_files.")
            if mime.startswith("application/vnd.google-apps"):
                fmt = (export_format or DEFAULT_EXPORT.get(mime, "pdf")).lower()
                if fmt not in EXPORT_BINARY:
                    raise RuntimeError(f"Неизвестный формат {fmt}. Доступно: {', '.join(EXPORT_BINARY)}")
                out_mime, ext = EXPORT_BINARY[fmt]
                data = _export_bytes(file_id, out_mime)
                if not name.lower().endswith(ext):
                    name += ext
                mime = out_mime
            else:
                size = int(meta.get("size", 0))
                if size > MAX_DOWNLOAD_MB * 1024 * 1024:
                    raise RuntimeError(f"Файл {human_size(size)} больше лимита {MAX_DOWNLOAD_MB} МБ")
                data = _download_bytes(file_id)
            if len(data) > MAX_DOWNLOAD_MB * 1024 * 1024:
                raise RuntimeError(f"Файл больше лимита {MAX_DOWNLOAD_MB} МБ")
            return {"id": file_id, "name": name, "mimeType": mime, "size": len(data),
                    "size_human": human_size(len(data)), "encoding": "base64",
                    "content_base64": base64.b64encode(data).decode()}
        return out(await run(_do))
    except Exception as e:
        return err(e)


# ===================== ЗАГРУЗКА / ИЗМЕНЕНИЕ ФАЙЛОВ =====================

@mcp.tool()
async def upload_file(name: str, content: str, folder_id: str = "", mime_type: str = "",
                      is_base64: bool = False, convert_to_google: bool = False, description: str = "") -> str:
    """Загрузить новый файл на диск.
    name — имя файла с расширением (например 'отчёт.docx'); content — содержимое:
    обычный текст (is_base64=False) или base64 для бинарных файлов (is_base64=True);
    folder_id — ID папки (пусто = корень или GDRIVE_ROOT_FOLDER_ID);
    mime_type — тип (определяется по расширению, если пусто);
    convert_to_google=True — сконвертировать в Google Docs/Sheets/Slides (txt/md/docx/html → Doc, csv/xlsx → Sheet, pptx → Slides)."""
    try:
        data = _decode_content(content, is_base64)
        mime = mime_type or mimetypes.guess_type(name)[0] or ("text/plain" if not is_base64 else "application/octet-stream")

        def _do():
            body: dict = {"name": name}
            parent = default_parent(folder_id)
            if parent:
                body["parents"] = [parent]
            if description:
                body["description"] = description
            if convert_to_google and mime in CONVERT_ON_UPLOAD:
                body["mimeType"] = CONVERT_ON_UPLOAD[mime]
                if body["name"].rsplit(".", 1)[-1].lower() in ("txt", "md", "docx", "doc", "csv", "xlsx", "xls", "pptx", "html", "odt", "ods", "rtf"):
                    body["name"] = body["name"].rsplit(".", 1)[0]
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=len(data) > 5 * 1024 * 1024)
            return svc("drive").files().create(body=body, media_body=media, fields=FILE_FIELDS, **common_kwargs()).execute()
        res = await run(_do)
        return out({"uploaded": True, "file": res})
    except Exception as e:
        return err(e)


@mcp.tool()
async def update_file_content(file_id: str, content: str, is_base64: bool = False, mime_type: str = "") -> str:
    """Полностью заменить содержимое существующего файла (ID и ссылка сохраняются, в Drive появляется новая версия).
    content — новый текст или base64 (is_base64=True). Для Google Docs текст заменит весь документ;
    для Google Sheets передайте CSV — заменит первый лист. Для точечной правки используйте docs_replace_text / sheets_write."""
    try:
        data = _decode_content(content, is_base64)

        def _do():
            meta = _get_meta(file_id, "id,name,mimeType")
            cur = meta["mimeType"]
            if cur == FOLDER_MIME:
                raise RuntimeError("Это папка")
            if cur == GDOC_MIME:
                upload_mime = mime_type or "text/plain"
            elif cur == GSHEET_MIME:
                upload_mime = mime_type or "text/csv"
            elif cur.startswith("application/vnd.google-apps"):
                upload_mime = mime_type or "application/octet-stream"
            else:
                upload_mime = mime_type or cur
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=upload_mime, resumable=len(data) > 5 * 1024 * 1024)
            return svc("drive").files().update(fileId=file_id, media_body=media, fields=FILE_FIELDS, **common_kwargs()).execute()
        res = await run(_do)
        return out({"updated": True, "file": res})
    except Exception as e:
        return err(e)


@mcp.tool()
async def create_folder(name: str, parent_id: str = "") -> str:
    """Создать папку. parent_id — ID родительской папки (пусто = корень или GDRIVE_ROOT_FOLDER_ID)."""
    try:
        def _do():
            body = {"name": name, "mimeType": FOLDER_MIME}
            parent = default_parent(parent_id)
            if parent:
                body["parents"] = [parent]
            return svc("drive").files().create(body=body, fields="id,name,webViewLink,parents", **common_kwargs()).execute()
        return out({"created": True, "folder": await run(_do)})
    except Exception as e:
        return err(e)


@mcp.tool()
async def rename_file(file_id: str, new_name: str, description: str = "") -> str:
    """Переименовать файл или папку (и при необходимости изменить описание)."""
    try:
        body = {"name": new_name}
        if description:
            body["description"] = description
        res = await run(lambda: svc("drive").files().update(fileId=file_id, body=body, fields="id,name,description,webViewLink", **common_kwargs()).execute())
        return out({"renamed": True, "file": res})
    except Exception as e:
        return err(e)


@mcp.tool()
async def move_file(file_id: str, new_folder_id: str) -> str:
    """Переместить файл или папку в другую папку."""
    try:
        def _do():
            meta = _get_meta(file_id, "parents")
            prev = ",".join(meta.get("parents", []))
            return svc("drive").files().update(
                fileId=file_id, addParents=new_folder_id, removeParents=prev or None,
                fields="id,name,parents,webViewLink", **common_kwargs()
            ).execute()
        return out({"moved": True, "file": await run(_do)})
    except Exception as e:
        return err(e)


@mcp.tool()
async def copy_file(file_id: str, new_name: str = "", folder_id: str = "") -> str:
    """Сделать копию файла. new_name — имя копии (пусто = «Копия ...»), folder_id — куда положить копию."""
    try:
        body: dict = {}
        if new_name:
            body["name"] = new_name
        if folder_id:
            body["parents"] = [folder_id]
        res = await run(lambda: svc("drive").files().copy(fileId=file_id, body=body, fields="id,name,mimeType,parents,webViewLink", **common_kwargs()).execute())
        return out({"copied": True, "file": res})
    except Exception as e:
        return err(e)


@mcp.tool()
async def delete_file(file_id: str, permanent: bool = False) -> str:
    """Удалить файл или папку. По умолчанию — в корзину (можно восстановить через restore_file).
    permanent=True — удалить безвозвратно (осторожно!)."""
    try:
        if permanent:
            await run(lambda: svc("drive").files().delete(fileId=file_id, **common_kwargs()).execute())
            return out({"deleted": True, "permanent": True, "id": file_id})
        res = await run(lambda: svc("drive").files().update(fileId=file_id, body={"trashed": True}, fields="id,name,trashed", **common_kwargs()).execute())
        return out({"trashed": True, "file": res})
    except Exception as e:
        return err(e)


@mcp.tool()
async def restore_file(file_id: str) -> str:
    """Восстановить файл из корзины."""
    try:
        res = await run(lambda: svc("drive").files().update(fileId=file_id, body={"trashed": False}, fields="id,name,trashed", **common_kwargs()).execute())
        return out({"restored": True, "file": res})
    except Exception as e:
        return err(e)


# ===================== ДОСТУП =====================

@mcp.tool()
async def share_file(file_id: str, email: str = "", role: str = "reader", anyone_with_link: bool = False,
                     notify: bool = False, message: str = "") -> str:
    """Открыть доступ к файлу или папке.
    email — кому дать доступ; role — reader (чтение), commenter (комментирование), writer (редактирование);
    anyone_with_link=True — доступ всем, у кого есть ссылка (email тогда не нужен);
    notify — отправить человеку письмо; message — текст письма."""
    try:
        if anyone_with_link:
            body = {"type": "anyone", "role": role}
            kw = {}
        elif email:
            body = {"type": "user", "role": role, "emailAddress": email}
            kw = {"sendNotificationEmail": notify}
            if notify and message:
                kw["emailMessage"] = message
        else:
            return err(RuntimeError("Укажите email или anyone_with_link=True"))

        def _do():
            perm = svc("drive").permissions().create(fileId=file_id, body=body, fields="id,type,role,emailAddress", **kw, **common_kwargs()).execute()
            link = _get_meta(file_id, "webViewLink").get("webViewLink")
            return {"shared": True, "permission": perm, "link": link}
        return out(await run(_do))
    except Exception as e:
        return err(e)


@mcp.tool()
async def list_permissions(file_id: str) -> str:
    """Показать, у кого есть доступ к файлу или папке."""
    try:
        res = await run(lambda: svc("drive").permissions().list(
            fileId=file_id, fields="permissions(id,type,role,emailAddress,displayName)", **common_kwargs()).execute())
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool()
async def remove_permission(file_id: str, permission_id: str) -> str:
    """Отозвать доступ. permission_id берётся из list_permissions."""
    try:
        await run(lambda: svc("drive").permissions().delete(fileId=file_id, permissionId=permission_id, **common_kwargs()).execute())
        return out({"removed": True, "permission_id": permission_id})
    except Exception as e:
        return err(e)


# ===================== GOOGLE DOCS =====================

def _doc_end_index(doc: dict) -> int:
    content = doc.get("body", {}).get("content", [])
    return content[-1]["endIndex"] if content else 1


@mcp.tool()
async def docs_create(title: str, content: str = "", folder_id: str = "") -> str:
    """Создать новый Google Doc. content — начальный текст. Можно передать Markdown — он будет
    сконвертирован в форматирование (заголовки, списки, жирный)."""
    try:
        def _do():
            body: dict = {"name": title, "mimeType": GDOC_MIME}
            parent = default_parent(folder_id)
            if parent:
                body["parents"] = [parent]
            if content:
                media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype="text/markdown")
                return svc("drive").files().create(body=body, media_body=media, fields="id,name,webViewLink", **common_kwargs()).execute()
            return svc("drive").files().create(body=body, fields="id,name,webViewLink", **common_kwargs()).execute()
        return out({"created": True, "document": await run(_do)})
    except Exception as e:
        return err(e)


@mcp.tool()
async def docs_append_text(document_id: str, text: str) -> str:
    """Добавить текст в конец Google Doc (без удаления существующего содержимого)."""
    try:
        def _do():
            doc = svc("docs").documents().get(documentId=document_id).execute()
            end = _doc_end_index(doc) - 1
            reqs = [{"insertText": {"location": {"index": max(end, 1)}, "text": ("\n" if end > 1 else "") + text}}]
            svc("docs").documents().batchUpdate(documentId=document_id, body={"requests": reqs}).execute()
            return {"appended": True, "document_id": document_id, "chars_added": len(text)}
        return out(await run(_do))
    except Exception as e:
        return err(e)


@mcp.tool()
async def docs_replace_text(document_id: str, find: str, replace_with: str, match_case: bool = True) -> str:
    """Найти и заменить текст во всём Google Doc (все вхождения). Форматирование остального документа сохраняется.
    Удобно для заполнения шаблонов: например find='{{client_name}}', replace_with='ООО Ромашка'."""
    try:
        def _do():
            reqs = [{"replaceAllText": {"containsText": {"text": find, "matchCase": match_case}, "replaceText": replace_with}}]
            r = svc("docs").documents().batchUpdate(documentId=document_id, body={"requests": reqs}).execute()
            n = r.get("replies", [{}])[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
            return {"replaced": n, "document_id": document_id}
        return out(await run(_do))
    except Exception as e:
        return err(e)


@mcp.tool()
async def docs_replace_many(document_id: str, replacements: dict[str, str], match_case: bool = True) -> str:
    """Сделать несколько замен за один раз. replacements — словарь {"что найти": "на что заменить", ...}.
    Идеально для заполнения шаблонов договоров/счетов с плейсхолдерами."""
    try:
        def _do():
            reqs = [{"replaceAllText": {"containsText": {"text": k, "matchCase": match_case}, "replaceText": v}}
                    for k, v in replacements.items()]
            r = svc("docs").documents().batchUpdate(documentId=document_id, body={"requests": reqs}).execute()
            counts = {k: rep.get("replaceAllText", {}).get("occurrencesChanged", 0)
                      for k, rep in zip(replacements.keys(), r.get("replies", []))}
            return {"document_id": document_id, "replaced": counts}
        return out(await run(_do))
    except Exception as e:
        return err(e)


@mcp.tool()
async def docs_insert_text(document_id: str, text: str, after_text: str = "", at_start: bool = False) -> str:
    """Вставить текст в Google Doc: после первого вхождения after_text, либо в начало (at_start=True),
    либо в конец (если ничего не указано)."""
    try:
        def _do():
            doc = svc("docs").documents().get(documentId=document_id).execute()
            if at_start:
                idx = 1
            elif after_text:
                idx = None
                for el in doc.get("body", {}).get("content", []):
                    for pe in el.get("paragraph", {}).get("elements", []):
                        tr = pe.get("textRun")
                        if tr and after_text in tr.get("content", ""):
                            idx = pe["startIndex"] + tr["content"].index(after_text) + len(after_text)
                            break
                    if idx is not None:
                        break
                if idx is None:
                    raise RuntimeError(f"Текст '{after_text}' не найден в документе (или разбит форматированием)")
            else:
                idx = max(_doc_end_index(doc) - 1, 1)
            svc("docs").documents().batchUpdate(documentId=document_id, body={"requests": [
                {"insertText": {"location": {"index": idx}, "text": text}}]}).execute()
            return {"inserted": True, "index": idx, "document_id": document_id}
        return out(await run(_do))
    except Exception as e:
        return err(e)


# ===================== GOOGLE SHEETS =====================

@mcp.tool()
async def sheets_create(title: str, sheet_names: list[str] | None = None, folder_id: str = "") -> str:
    """Создать новую Google-таблицу. sheet_names — названия листов (необязательно)."""
    try:
        def _do():
            body: dict = {"properties": {"title": title}}
            if sheet_names:
                body["sheets"] = [{"properties": {"title": n}} for n in sheet_names]
            ss = svc("sheets").spreadsheets().create(body=body, fields="spreadsheetId,spreadsheetUrl").execute()
            parent = default_parent(folder_id)
            if parent:
                svc("drive").files().update(fileId=ss["spreadsheetId"], addParents=parent, removeParents="root",
                                            fields="id", **common_kwargs()).execute()
            return ss
        return out({"created": True, "spreadsheet": await run(_do)})
    except Exception as e:
        return err(e)


@mcp.tool()
async def sheets_get_info(spreadsheet_id: str) -> str:
    """Структура Google-таблицы: название и список листов с размерами (сколько строк/столбцов)."""
    try:
        res = await run(lambda: svc("sheets").spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="properties(title,locale),spreadsheetUrl,sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)))"
        ).execute())
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool()
async def sheets_read(spreadsheet_id: str, range: str = "", value_render: str = "FORMATTED_VALUE") -> str:
    """Прочитать данные из Google-таблицы. range в A1-нотации: 'Лист1!A1:D50', 'Лист1' (весь лист), 'A:C'.
    Пусто — прочитать все листы. value_render: FORMATTED_VALUE (как видно в таблице), UNFORMATTED_VALUE (сырые значения), FORMULA (формулы)."""
    try:
        def _do():
            sh = svc("sheets").spreadsheets()
            if range:
                return sh.values().get(spreadsheetId=spreadsheet_id, range=range, valueRenderOption=value_render).execute()
            meta = sh.get(spreadsheetId=spreadsheet_id, fields="sheets(properties(title))").execute()
            titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
            ranges = [f"'{t}'" for t in titles]
            return sh.values().batchGet(spreadsheetId=spreadsheet_id, ranges=ranges, valueRenderOption=value_render).execute()
        return out(await run(_do))
    except Exception as e:
        return err(e)


@mcp.tool()
async def sheets_write(spreadsheet_id: str, range: str, values: list[list[Any]], as_user_input: bool = True) -> str:
    """Записать данные в ячейки Google-таблицы (перезаписывает указанный диапазон).
    range — начальная ячейка или диапазон, например 'Лист1!A1' или 'Лист1!B2:D10';
    values — двумерный массив строк: [["Имя","Сумма"],["Иван",1000]];
    as_user_input=True — значения обрабатываются как ввод пользователя (формулы и даты работают).
    ВАЖНО: формулы пишутся в синтаксисе локали таблицы (см. sheets_get_info → locale). Для ru_RU разделитель аргументов — точка с запятой:
    '=IFERROR(ROUND(G2/F2;0);"")', '=SUMIFS(\'Лист\'!J:J;\'Лист\'!K:K;A2)'; для en_US — запятая. Дробные числа передавайте числом, не строкой."""
    try:
        res = await run(lambda: svc("sheets").spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=range,
            valueInputOption="USER_ENTERED" if as_user_input else "RAW",
            body={"values": values}).execute())
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool()
async def sheets_append(spreadsheet_id: str, range: str, values: list[list[Any]], as_user_input: bool = True) -> str:
    """Добавить строки в конец таблицы (после последней заполненной строки).
    range — лист или диапазон, например 'Лист1' или 'Лист1!A:F'; values — [[...],[...]].
    Формулы — в синтаксисе локали таблицы (для ru_RU разделитель аргументов ';'), как в sheets_write."""
    try:
        res = await run(lambda: svc("sheets").spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=range,
            valueInputOption="USER_ENTERED" if as_user_input else "RAW",
            insertDataOption="INSERT_ROWS", body={"values": values}).execute())
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool()
async def sheets_clear(spreadsheet_id: str, range: str) -> str:
    """Очистить значения в диапазоне (форматирование остаётся). range — например 'Лист1!A2:F100'."""
    try:
        res = await run(lambda: svc("sheets").spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range=range, body={}).execute())
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool()
async def sheets_add_sheet(spreadsheet_id: str, title: str) -> str:
    """Добавить новый лист в Google-таблицу."""
    try:
        res = await run(lambda: svc("sheets").spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]}).execute())
        props = res["replies"][0]["addSheet"]["properties"]
        return out({"added": True, "sheet": props})
    except Exception as e:
        return err(e)


# ===================== ЗАПУСК =====================

APP_NAME = os.environ.get("APP_PUBLIC_NAME", "Google Drive MCP")
CONTACT_EMAIL = os.environ.get("APP_CONTACT_EMAIL", "")

HOME_HTML = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{APP_NAME}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;line-height:1.5">
<h1>{APP_NAME}</h1>
<p>Служебный MCP-коннектор, через который AI-ассистент компании работает с файлами Google Drive
владельца аккаунта: поиск, чтение, загрузка и редактирование документов.</p>
<p>Коннектор предназначен только для внутреннего использования. <a href="/privacy">Политика конфиденциальности</a>.</p>
</body></html>"""

PRIVACY_HTML = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Политика конфиденциальности — {APP_NAME}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;line-height:1.5">
<h1>Политика конфиденциальности</h1>
<p>{APP_NAME} — внутренний сервис, который по запросу владельца аккаунта Google обращается к его Google Drive,
Google Docs и Google Sheets через официальные API Google.</p>
<h2>Какие данные используются</h2>
<p>Сервис получает доступ к файлам и их метаданным только для выполнения запросов владельца
(поиск, чтение, загрузка, изменение файлов). Данные не продаются, не передаются третьим лицам
и не используются для рекламы.</p>
<h2>Хранение</h2>
<p>Содержимое файлов не сохраняется на сервере сервиса дольше времени обработки запроса.
На сервере хранится только токен доступа, выданный владельцем аккаунта.</p>
<h2>Отзыв доступа</h2>
<p>Владелец может в любой момент отозвать доступ на странице
<a href="https://myaccount.google.com/permissions">https://myaccount.google.com/permissions</a>.</p>
<p>Использование данных, полученных через API Google, соответствует
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>,
включая требования Limited Use.</p>
{f'<h2>Контакты</h2><p>{CONTACT_EMAIL}</p>' if CONTACT_EMAIL else ''}
</body></html>"""

def build_http_app():
    """HTTP-приложение (streamable-http или sse) с проверкой токена доступа и /health."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, HTMLResponse

    app = mcp.sse_app() if MCP_TRANSPORT == "sse" else mcp.streamable_http_app()

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path.rstrip("/") or "/"
            if path == "/health":
                return JSONResponse({"status": "ok", "service": "gdrive-mcp"})
            if path == "/" and request.method == "GET":
                return HTMLResponse(HOME_HTML)
            if path == "/privacy" and request.method == "GET":
                return HTMLResponse(PRIVACY_HTML)
            if path in ("/oauth/start", "/oauth/callback"):
                return await oauth_route(request, path)
            if MCP_AUTH_TOKEN:
                auth = request.headers.get("authorization", "")
                token = auth[7:] if auth.lower().startswith("bearer ") else ""
                token = token or request.headers.get("x-api-key", "") or request.query_params.get("token", "")
                if token != MCP_AUTH_TOKEN:
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    async def oauth_route(request, path):
        import oauth_web
        from starlette.responses import RedirectResponse
        if path == "/oauth/start":
            token = request.query_params.get("token", "")
            if MCP_AUTH_TOKEN and token != MCP_AUTH_TOKEN:
                return HTMLResponse(oauth_web.page("Нет доступа", "<p>Откройте ссылку с параметром ?token=MCP_AUTH_TOKEN.</p>"), status_code=401)
            return RedirectResponse(oauth_web.start_url(request))
        if request.query_params.get("error"):
            return HTMLResponse(oauth_web.page("Доступ не выдан", f"<p>{request.query_params.get('error')}</p>"), status_code=400)
        if not oauth_web.check_state(request.query_params.get("state", "")):
            return HTMLResponse(oauth_web.page("Ссылка устарела", "<p>Начните заново с /oauth/start.</p>"), status_code=400)
        try:
            await run(oauth_web.exchange_code, request, request.query_params.get("code", ""))
        except Exception as e:
            return HTMLResponse(oauth_web.page("Ошибка", f"<p>{e}</p>"), status_code=500)
        global _creds
        _creds = None
        _services.clear()
        return HTMLResponse(oauth_web.page("Google Drive подключён ✅", "<p>Доступ сохранён. Это окно можно закрыть.</p>"))

    app.add_middleware(AuthMiddleware)
    return app


if __name__ == "__main__":
    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        if not MCP_AUTH_TOKEN:
            print("ВНИМАНИЕ: MCP_AUTH_TOKEN не задан — сервер доступен без авторизации!")
        uvicorn.run(build_http_app(), host=MCP_HOST, port=MCP_PORT, proxy_headers=True, forwarded_allow_ips="*")
