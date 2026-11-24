"""
Google Drive MCP Server

MCP-коннектор для Google Drive: поиск, чтение, скачивание, загрузка и
изменение файлов, работа с Google Docs и Google Sheets.

Безопасность (см. safety.py и README):
  * необратимые действия запрещены по умолчанию;
  * опасные действия требуют подтверждения пользователя (confirm=true);
  * перед перезаписью данные сохраняются (журнал/ревизии Google);
  * каждое изменение пишется в журнал logs/audit.log.

Транспорты:
  - stdio            — Claude Desktop и локальный запуск
  - streamable-http  — хостинг и подключение к AI-платформам по URL
  - sse              — устаревший HTTP-транспорт
"""

import asyncio
import base64
import csv
import hmac
import io
import json
import mimetypes
import os
import sys
import threading
from typing import Any

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import httplib2  # noqa: E402
from google.auth.exceptions import RefreshError  # noqa: E402
from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2 import service_account  # noqa: E402
from google.oauth2.credentials import Credentials as UserCredentials  # noqa: E402
from google_auth_httplib2 import AuthorizedHttp  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import safety  # noqa: E402
from safety import Blocked, audit, confirmation_required, needs_confirmation, require_write  # noqa: E402

__version__ = "2.0.0"

# ===================== КОНФИГУРАЦИЯ =====================

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_IMPERSONATE_USER = os.environ.get("GOOGLE_IMPERSONATE_USER", "")

# Папка по умолчанию для списка/поиска/создания (не ограничение — для ограничения см. GDRIVE_ALLOWED_FOLDER_IDS)
GDRIVE_ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "")

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
MCP_ALLOW_NO_AUTH = safety.flag("MCP_ALLOW_NO_AUTH", False)
MCP_ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

MAX_TEXT_CHARS = int(safety.num("GDRIVE_MAX_TEXT_CHARS", 200000))
MAX_DOWNLOAD_MB = safety.num("GDRIVE_MAX_DOWNLOAD_MB", 20)
MAX_READ_MB = safety.num("GDRIVE_MAX_READ_MB", 50)
MAX_UPLOAD_MB = safety.num("GDRIVE_MAX_UPLOAD_MB", 50)
HTTP_TIMEOUT = safety.num("GDRIVE_HTTP_TIMEOUT", 60)
RETRIES = int(safety.num("GDRIVE_RETRIES", 4))

FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIME = "application/vnd.google-apps.document"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
GSLIDES_MIME = "application/vnd.google-apps.presentation"
GDRAWING_MIME = "application/vnd.google-apps.drawing"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

EXPORT_TEXT = {GDOC_MIME: "text/markdown", GSHEET_MIME: "text/csv", GSLIDES_MIME: "text/plain", GDRAWING_MIME: "image/svg+xml"}
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
CONVERT_ON_UPLOAD = {
    "text/plain": GDOC_MIME, "text/markdown": GDOC_MIME, "text/html": GDOC_MIME,
    "application/msword": GDOC_MIME,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": GDOC_MIME,
    "application/rtf": GDOC_MIME, "application/vnd.oasis.opendocument.text": GDOC_MIME,
    "text/csv": GSHEET_MIME, "application/vnd.ms-excel": GSHEET_MIME,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": GSHEET_MIME,
    "application/vnd.oasis.opendocument.spreadsheet": GSHEET_MIME,
    "application/vnd.ms-powerpoint": GSLIDES_MIME,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": GSLIDES_MIME,
}
FILE_FIELDS = ("id,name,mimeType,size,createdTime,modifiedTime,parents,webViewLink,"
               "owners(displayName,emailAddress),lastModifyingUser(displayName,emailAddress),"
               "shared,trashed,description,starred,driveId,capabilities(canEdit,canTrash,canDelete,canShare)")
LIST_FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime,parents,webViewLink,driveId)"
SHEET_ERRORS = ("#ERROR!", "#NAME?", "#REF!", "#VALUE!", "#N/A", "#DIV/0!", "#NUM!", "#NULL!")

mimetypes.add_type("text/markdown", ".md")


# ===================== АВТОРИЗАЦИЯ GOOGLE (потокобезопасно) =====================

class AuthProblem(Exception):
    pass


_cred_lock = threading.Lock()
_creds = None
_cred_gen = 0                      # меняется при переподключении — потоки пересоздают клиентов
_tls = threading.local()           # клиенты Google API не потокобезопасны: свой набор на каждый поток


def _new_credentials():
    import oauth_web
    refresh_token = GOOGLE_REFRESH_TOKEN or oauth_web.load_refresh_token()
    if refresh_token and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        return UserCredentials(token=None, refresh_token=refresh_token, client_id=GOOGLE_CLIENT_ID,
                               client_secret=GOOGLE_CLIENT_SECRET, token_uri="https://oauth2.googleapis.com/token",
                               scopes=SCOPES)
    if GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE:
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            c = service_account.Credentials.from_service_account_info(json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=SCOPES)
        else:
            c = service_account.Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return c.with_subject(GOOGLE_IMPERSONATE_USER) if GOOGLE_IMPERSONATE_USER else c
    raise AuthProblem("Google-аккаунт не подключён. Откройте https://<домен>/oauth/start?token=<MCP_AUTH_TOKEN> "
                      "и войдите в Google, либо задайте GOOGLE_REFRESH_TOKEN / сервисный аккаунт.")


def get_credentials():
    global _creds
    with _cred_lock:
        if _creds is None:
            _creds = _new_credentials()
        if not _creds.valid:
            try:
                _creds.refresh(Request())
            except RefreshError as e:
                raise AuthProblem("Доступ к Google отозван или истёк (" + str(e)[:120] + "). Переподключите диск: "
                                  "https://<домен>/oauth/start?token=<MCP_AUTH_TOKEN>")
        return _creds


def reset_credentials():
    global _creds, _cred_gen
    with _cred_lock:
        _creds = None
        _cred_gen += 1


def svc(name: str):
    version = {"drive": "v3", "docs": "v1", "sheets": "v4"}[name]
    creds = get_credentials()
    cache = getattr(_tls, "services", None)
    if cache is None or getattr(_tls, "gen", -1) != _cred_gen:
        cache = _tls.services = {}
        _tls.gen = _cred_gen
    if name not in cache:
        http = AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT))
        cache[name] = build(name, version, http=http, cache_discovery=False, static_discovery=True)
    return cache[name]


def ex(request, idempotent: bool = True):
    """Выполнить запрос. Повторы (429/5xx, с паузой) — только для запросов, повтор которых ничего не задвоит."""
    return request.execute(num_retries=RETRIES if idempotent else 0)


async def run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def out(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def err(e: Exception) -> str:
    if isinstance(e, Blocked):
        return out({"error": True, "blocked": True, "message": str(e)})
    if isinstance(e, AuthProblem):
        return out({"error": True, "auth": True, "message": str(e)})
    if isinstance(e, HttpError):
        try:
            msg = json.loads(e.content.decode()).get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        status = e.resp.status
        hint = ""
        if status == 404:
            hint = " Файл не найден или у подключённого аккаунта нет к нему доступа."
        elif status == 403 and "insufficient" in msg.lower():
            hint = " У подключённого аккаунта недостаточно прав на этот файл (нужен уровень «Редактор»)."
        elif status == 429 or "rate limit" in msg.lower():
            hint = " Google временно ограничил частоту запросов — повторите через минуту."
        elif "storage quota" in msg.lower():
            hint = " Нет места на диске (у сервисного аккаунта своего места нет — используйте общий диск или OAuth)."
        return out({"error": True, "status": status, "message": msg + hint})
    return out({"error": True, "message": str(e)[:1000]})


def common_kwargs() -> dict:
    return {"supportsAllDrives": True}


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


# ===================== ОГРАНИЧЕНИЕ ПАПКАМИ =====================

_scope_cache: dict[str, float] = {}   # file_id -> когда подтверждено, что он внутри разрешённых папок
_scope_lock = threading.Lock()
SCOPE_TTL = 300


def _scope_cached(fid: str) -> bool:
    import time
    with _scope_lock:
        t = _scope_cache.get(fid)
    return t is not None and time.time() - t < SCOPE_TTL


def _scope_remember(fid: str):
    import time
    with _scope_lock:
        _scope_cache[fid] = time.time()


def in_scope(file_id: str) -> bool:
    """True, если файл лежит (на любой глубине) внутри разрешённых папок. Без ограничения — всегда True."""
    allowed = set(safety.ALLOWED_FOLDER_IDS)
    if not allowed:
        return True
    if file_id in allowed:
        return True
    if _scope_cached(file_id):
        return True
    # Обход вверх по родителям (у файла может быть несколько родителей). Кэшируем только положительный ответ:
    # файл могли переместить, поэтому «нельзя» всегда перепроверяется.
    seen, frontier = set(), [file_id]
    for _ in range(40):
        nxt = []
        for fid in frontier:
            if fid in allowed or _scope_cached(fid):
                _scope_remember(file_id)
                return True
            if fid in seen:
                continue
            seen.add(fid)
            try:
                meta = ex(svc("drive").files().get(fileId=fid, fields="id,parents", **common_kwargs()))
            except HttpError:
                continue
            nxt.extend(meta.get("parents", []) or [])
        if not nxt:
            break
        frontier = nxt
    return False


def require_scope(*file_ids: str):
    for fid in file_ids:
        if fid and not in_scope(fid):
            raise Blocked("Этот файл/папка вне разрешённых папок коннектора (GDRIVE_ALLOWED_FOLDER_IDS). Доступ запрещён.")


def default_parent(folder_id: str) -> str:
    if folder_id:
        return folder_id
    if GDRIVE_ROOT_FOLDER_ID:
        return GDRIVE_ROOT_FOLDER_ID
    if safety.ALLOWED_FOLDER_IDS:
        return safety.ALLOWED_FOLDER_IDS[0]
    return ""


# ===================== ВНУТРЕННИЕ ОПЕРАЦИИ С DRIVE =====================

def _get_meta(file_id: str, fields: str = FILE_FIELDS) -> dict:
    return ex(svc("drive").files().get(fileId=file_id, fields=fields, **common_kwargs()))


def _download_bytes(file_id: str, limit_mb: float) -> bytes:
    req = svc("drive").files().get_media(fileId=file_id, **common_kwargs())
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req, chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk(num_retries=RETRIES)
        if buf.tell() > limit_mb * 1024 * 1024:
            raise RuntimeError(f"Файл больше лимита {limit_mb:g} МБ")
    return buf.getvalue()


def _export_bytes(file_id: str, mime: str) -> bytes:
    return ex(svc("drive").files().export(fileId=file_id, mimeType=mime))


def _extract_text(data: bytes, mime: str, name: str) -> str:
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
        from pptx import Presentation
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
        text = "\n\n".join(f"--- Страница {i} ---\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages, 1))
        if not text.replace("-", "").replace("Страница", "").strip(" \n0123456789"):
            text += "\n\n[В PDF нет текстового слоя — вероятно, это скан. Распознавание (OCR) не поддерживается.]"
        return text
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
    offset = max(0, int(offset or 0))
    max_chars = min(max_chars or MAX_TEXT_CHARS, MAX_TEXT_CHARS)
    chunk = text[offset: offset + max_chars]
    res = {"total_chars": total, "offset": offset, "returned_chars": len(chunk), "content": chunk}
    if offset + len(chunk) < total:
        res["truncated"] = True
        res["next_offset"] = offset + len(chunk)
    return res


def _decode_content(content: str, is_base64: bool) -> bytes:
    try:
        data = base64.b64decode(content, validate=True) if is_base64 else content.encode("utf-8")
    except Exception:
        raise RuntimeError("content не является корректным base64")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise RuntimeError(f"Файл больше лимита {MAX_UPLOAD_MB:g} МБ")
    return data


def _count_children(folder_id: str) -> int:
    res = ex(svc("drive").files().list(q=f"'{esc(folder_id)}' in parents and trashed = false", pageSize=1000,
                                       fields="files(id)", includeItemsFromAllDrives=True, corpora="allDrives",
                                       **common_kwargs()))
    return len(res.get("files", []))


def _cells(values: list) -> int:
    return sum(len(r) if isinstance(r, list) else 1 for r in values or [])


# ===================== MCP-СЕРВЕР =====================

if MCP_ALLOWED_HOSTS:
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=MCP_ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in MCP_ALLOWED_HOSTS] + [f"http://{h}" for h in MCP_ALLOWED_HOSTS])
elif MCP_TRANSPORT != "stdio":
    transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
else:
    transport_security = None

mcp = FastMCP(
    "Google Drive",
    instructions=(
        "Коннектор к Google Drive. Ищите файлы через search_files/list_files, читайте через read_file "
        "(для таблиц — sheets_read), правьте Google Docs через docs_*, Google Sheets через sheets_*. "
        "Если инструмент вернул needs_confirmation — ничего не выполнено: перескажите пользователю, что будет "
        "сделано, получите явное согласие и повторите вызов с confirm=true. Никогда не ставьте confirm=true "
        "без согласия пользователя."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    max_request_body_size=int(MAX_UPLOAD_MB * 1.4 * 1024 * 1024) + 1024 * 1024,
    stateless_http=safety.flag("MCP_STATELESS", True),
    json_response=safety.flag("MCP_JSON_RESPONSE", False),
    transport_security=transport_security,
)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DANGER = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


# ===================== ОБЗОР / ПОИСК =====================

@mcp.tool(annotations=RO)
async def get_drive_info() -> str:
    """Информация о подключённом Google-аккаунте: пользователь, место на диске, общие диски и настройки безопасности коннектора."""
    try:
        def _do():
            about = ex(svc("drive").about().get(fields="user(displayName,emailAddress),storageQuota"))
            try:
                drives = ex(svc("drive").drives().list(pageSize=50, fields="drives(id,name)")).get("drives", [])
            except HttpError:
                drives = []
            q = about.get("storageQuota", {})
            return {"user": about.get("user"), "storage": {k: human_size(v) for k, v in q.items()},
                    "shared_drives": drives, "connector_version": __version__,
                    "safety_settings": safety.settings_summary()}
        return out(await run(_do))
    except Exception as e:
        return err(e)


@mcp.tool(annotations=RO)
async def list_files(folder_id: str = "", page_size: int = 50, page_token: str = "",
                     order_by: str = "folder,modifiedTime desc", include_trashed: bool = False) -> str:
    """Список файлов и папок внутри папки. folder_id — ID папки (пусто = папка по умолчанию или корень «Моего диска»;
    'root' = корень). Файлы «Доступные мне» в корне не лежат — ищите их через search_files(shared_with_me=true).
    page_size — до 200; page_token — для следующей страницы. order_by — например 'name', 'modifiedTime desc'."""
    try:
        parent = default_parent(folder_id) or "root"
        page_size = max(1, min(int(page_size), 200))

        def _do():
            require_scope(parent if parent != "root" else "")
            if parent == "root" and safety.ALLOWED_FOLDER_IDS:
                raise Blocked("Коннектор ограничен папками; укажите folder_id одной из разрешённых папок.")
            q = f"'{esc(parent)}' in parents" + ("" if include_trashed else " and trashed = false")
            return ex(svc("drive").files().list(q=q, pageSize=page_size, pageToken=page_token or None, orderBy=order_by,
                                                fields=LIST_FIELDS, includeItemsFromAllDrives=True, corpora="allDrives",
                                                **common_kwargs()))
        res = await run(_do)
        for f in res.get("files", []):
            f["is_folder"] = f["mimeType"] == FOLDER_MIME
            if "size" in f:
                f["size_human"] = human_size(f["size"])
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool(annotations=RO)
async def search_files(name_contains: str = "", full_text: str = "", mime_type: str = "",
                       folder_id: str = "", modified_after: str = "", only_folders: bool = False,
                       shared_with_me: bool = False, raw_query: str = "", page_size: int = 50, page_token: str = "") -> str:
    """Поиск файлов по всему диску, включая «Доступные мне» и общие диски.
    name_contains — часть имени; full_text — поиск по содержимому; mime_type — тип или псевдоним:
    doc, sheet, slides, folder, pdf, image, docx, xlsx; folder_id — только непосредственно в этой папке;
    modified_after — дата ISO ('2026-01-01'); shared_with_me=true — только файлы, которыми поделились с аккаунтом;
    raw_query — запрос на языке Drive API (заменяет остальные условия)."""
    aliases = {"doc": GDOC_MIME, "sheet": GSHEET_MIME, "slides": GSLIDES_MIME, "folder": FOLDER_MIME,
               "pdf": "application/pdf",
               "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
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
                mime_type = "folder"
            if mime_type == "image":
                conds.append("mimeType contains 'image/'")
            elif mime_type:
                conds.append(f"mimeType = '{esc(aliases.get(mime_type, mime_type))}'")
            if folder_id:
                conds.append(f"'{esc(folder_id)}' in parents")
            if shared_with_me:
                conds.append("sharedWithMe = true")
            if modified_after:
                ts = modified_after if "T" in modified_after else modified_after + "T00:00:00"
                conds.append(f"modifiedTime > '{esc(ts)}'")
            q = " and ".join(conds)
        page_size = max(1, min(int(page_size), 200))

        def _do():
            kw = dict(q=q, pageSize=page_size, pageToken=page_token or None, fields=LIST_FIELDS,
                      includeItemsFromAllDrives=True, corpora="allDrives", **common_kwargs())
            if not full_text:
                kw["orderBy"] = "modifiedTime desc"
            res = ex(svc("drive").files().list(**kw))
            if safety.ALLOWED_FOLDER_IDS:
                res["files"] = [f for f in res.get("files", []) if in_scope(f["id"])]
            return res
        res = await run(_do)
        res["query"] = q
        for f in res.get("files", []):
            if "size" in f:
                f["size_human"] = human_size(f["size"])
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool(annotations=RO)
async def get_file_info(file_id: str) -> str:
    """Подробная информация о файле/папке: имя, тип, размер, владелец, даты, ссылка, папки, права подключённого аккаунта (capabilities)."""
    try:
        def _do():
            require_scope(file_id)
            return _get_meta(file_id)
        meta = await run(_do)
        if "size" in meta:
            meta["size_human"] = human_size(meta["size"])
        return out(meta)
    except Exception as e:
        return err(e)


# ===================== ЧТЕНИЕ / СКАЧИВАНИЕ =====================

@mcp.tool(annotations=RO)
async def read_file(file_id: str, offset: int = 0, max_chars: int = 50000) -> str:
    """Прочитать содержимое файла как текст. Google Docs → Markdown, Google Sheets → CSV первого листа
    (для всех листов и диапазонов — sheets_read), Google Slides → текст, .docx/.xlsx/.pptx/.pdf/.txt/.csv/.json/.md →
    извлечённый текст. Длинные файлы читайте частями: offset и max_chars (в ответе есть next_offset)."""
    try:
        def _do():
            require_scope(file_id)
            meta = _get_meta(file_id, "id,name,mimeType,size,modifiedTime,webViewLink")
            mime = meta["mimeType"]
            if mime == FOLDER_MIME:
                raise RuntimeError("Это папка. Используйте list_files.")
            if mime == SHORTCUT_MIME:
                raise RuntimeError("Это ярлык. Найдите исходный файл через get_file_info/search_files.")
            if mime in EXPORT_TEXT:
                text = _export_bytes(file_id, EXPORT_TEXT[mime]).decode("utf-8", errors="replace")
            elif mime.startswith("application/vnd.google-apps"):
                raise RuntimeError(f"Тип {mime} нельзя прочитать как текст")
            else:
                size = int(meta.get("size", 0) or 0)
                if size > MAX_READ_MB * 1024 * 1024:
                    raise RuntimeError(f"Файл слишком большой ({human_size(size)}) для чтения, лимит {MAX_READ_MB:g} МБ")
                text = _extract_text(_download_bytes(file_id, MAX_READ_MB), mime, meta["name"])
            return meta, text
        meta, text = await run(_do)
        res = {"file": meta}
        res.update(_truncate(text, offset, max_chars))
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool(annotations=RO)
async def download_file(file_id: str, export_format: str = "") -> str:
    """Скачать файл целиком в base64 (картинки, PDF, офисные документы). Google-файлы экспортируются:
    export_format = docx | xlsx | pptx | pdf | txt | md | csv | html | odt | ods | png | svg.
    Лимит — GDRIVE_MAX_DOWNLOAD_MB. Если нужен только текст — используйте read_file."""
    try:
        def _do():
            require_scope(file_id)
            meta = _get_meta(file_id, "id,name,mimeType,size")
            mime, name = meta["mimeType"], meta["name"]
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
                size = int(meta.get("size", 0) or 0)
                if size > MAX_DOWNLOAD_MB * 1024 * 1024:
                    raise RuntimeError(f"Файл {human_size(size)} больше лимита {MAX_DOWNLOAD_MB:g} МБ")
                data = _download_bytes(file_id, MAX_DOWNLOAD_MB)
            if len(data) > MAX_DOWNLOAD_MB * 1024 * 1024:
                raise RuntimeError(f"Файл больше лимита {MAX_DOWNLOAD_MB:g} МБ")
            return {"id": file_id, "name": name, "mimeType": mime, "size": len(data), "size_human": human_size(len(data)),
                    "encoding": "base64", "content_base64": base64.b64encode(data).decode()}
        return out(await run(_do))
    except Exception as e:
        return err(e)


# ===================== СОЗДАНИЕ / ИЗМЕНЕНИЕ ФАЙЛОВ =====================

@mcp.tool(annotations=WRITE)
async def upload_file(name: str, content: str, folder_id: str = "", mime_type: str = "",
                      is_base64: bool = False, convert_to_google: bool = False, description: str = "") -> str:
    """Загрузить НОВЫЙ файл (существующие файлы не затрагиваются; одноимённый файл будет создан рядом).
    name — имя с расширением; content — текст (is_base64=false) или base64 (is_base64=true); folder_id — папка;
    convert_to_google=true — сконвертировать в Google Docs/Sheets/Slides (txt/md/docx/html → Doc, csv/xlsx → Sheet, pptx → Slides)."""
    tool = "upload_file"
    try:
        require_write()
        if not name.strip():
            raise RuntimeError("Укажите имя файла")
        data = _decode_content(content, is_base64)
        mime = mime_type or mimetypes.guess_type(name)[0] or ("application/octet-stream" if is_base64 else "text/plain")

        def _do():
            parent = default_parent(folder_id)
            require_scope(parent)
            body: dict = {"name": name}
            if parent:
                body["parents"] = [parent]
            if description:
                body["description"] = description
            if convert_to_google and mime in CONVERT_ON_UPLOAD:
                body["mimeType"] = CONVERT_ON_UPLOAD[mime]
                if "." in name and name.rsplit(".", 1)[-1].lower() in ("txt", "md", "docx", "doc", "csv", "xlsx", "xls", "pptx", "html", "odt", "ods", "rtf"):
                    body["name"] = name.rsplit(".", 1)[0]
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=len(data) > 5 * 1024 * 1024)
            return ex(svc("drive").files().create(body=body, media_body=media, fields=FILE_FIELDS, **common_kwargs()), idempotent=False)
        res = await run(_do)
        audit(tool, "ok", file_id=res.get("id"), name=res.get("name"), size=len(data), folder=folder_id)
        return out({"uploaded": True, "file": res})
    except Exception as e:
        audit(tool, "error" if not isinstance(e, Blocked) else "blocked", name=name, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=DANGER)
async def update_file_content(file_id: str, content: str, is_base64: bool = False, mime_type: str = "", confirm: bool = False) -> str:
    """ПОЛНОСТЬЮ заменить содержимое обычного файла (docx, xlsx, pdf, txt и т.п.) новой версией — ID и ссылка сохраняются.
    Предыдущая версия закрепляется в истории версий Google навсегда (её можно восстановить).
    Google Docs / Sheets / Slides этим инструментом НЕ перезаписываются — для них есть точечные docs_* и sheets_*.
    Требует подтверждения пользователя: без confirm=true ничего не меняет и возвращает описание действия."""
    tool = "update_file_content"
    try:
        require_write()
        if not safety.ALLOW_OVERWRITE:
            raise Blocked("Перезапись файлов отключена настройкой GDRIVE_ALLOW_OVERWRITE=false. Загрузите новую версию отдельным файлом.")
        data = _decode_content(content, is_base64)

        def _do():
            require_scope(file_id)
            meta = _get_meta(file_id, "id,name,mimeType,size,modifiedTime,headRevisionId,capabilities(canEdit)")
            cur = meta["mimeType"]
            if cur == FOLDER_MIME:
                raise Blocked("Это папка — её содержимое нельзя заменить.")
            if cur.startswith("application/vnd.google-apps"):
                raise Blocked("Google Docs/Sheets/Slides нельзя перезаписать целиком — это стёрло бы форматирование, "
                              "другие листы и формулы. Используйте docs_replace_text/docs_insert_text/docs_append_text "
                              "или sheets_write/sheets_append, либо загрузите новый файл через upload_file.")
            if confirmation_required(confirm):
                return needs_confirmation("Полная замена содержимого файла", {
                    "file": meta.get("name"), "file_id": file_id, "current_size": human_size(meta.get("size")),
                    "new_size": human_size(len(data)), "last_modified": meta.get("modifiedTime")},
                    "Текущее содержимое файла будет заменено. Старая версия останется в истории версий Google.")
            kept = None
            rev = meta.get("headRevisionId")
            if rev:
                try:
                    ex(svc("drive").revisions().update(fileId=file_id, revisionId=rev, body={"keepForever": True}))
                    kept = rev
                except HttpError:
                    kept = None
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type or cur, resumable=len(data) > 5 * 1024 * 1024)
            res = ex(svc("drive").files().update(fileId=file_id, media_body=media, fields=FILE_FIELDS, **common_kwargs()), idempotent=False)
            return {"updated": True, "file": res, "previous_revision_kept": kept,
                    "restore_hint": "Предыдущая версия: Google Диск → файл → «Управление версиями»." if kept else None}
        res = await run(_do)
        if res.get("needs_confirmation"):
            audit(tool, "confirm_requested", file_id=file_id)
        else:
            audit(tool, "ok", file_id=file_id, previous_revision_kept=res.get("previous_revision_kept"), size=len(data))
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=file_id, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=WRITE)
async def create_folder(name: str, parent_id: str = "") -> str:
    """Создать папку. parent_id — ID родительской папки (пусто = папка по умолчанию или корень)."""
    tool = "create_folder"
    try:
        require_write()

        def _do():
            parent = default_parent(parent_id)
            require_scope(parent)
            body = {"name": name, "mimeType": FOLDER_MIME}
            if parent:
                body["parents"] = [parent]
            return ex(svc("drive").files().create(body=body, fields="id,name,webViewLink,parents", **common_kwargs()), idempotent=False)
        res = await run(_do)
        audit(tool, "ok", file_id=res.get("id"), name=name, parent=parent_id)
        return out({"created": True, "folder": res})
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", name=name, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=WRITE)
async def rename_file(file_id: str, new_name: str, description: str = "") -> str:
    """Переименовать файл или папку (и при необходимости изменить описание). Содержимое не меняется."""
    tool = "rename_file"
    try:
        require_write()
        if not new_name.strip():
            raise RuntimeError("Новое имя не может быть пустым")

        def _do():
            require_scope(file_id)
            old = _get_meta(file_id, "name")
            body = {"name": new_name}
            if description:
                body["description"] = description
            res = ex(svc("drive").files().update(fileId=file_id, body=body, fields="id,name,description,webViewLink", **common_kwargs()))
            return old.get("name"), res
        old_name, res = await run(_do)
        audit(tool, "ok", file_id=file_id, old_name=old_name, new_name=new_name)
        return out({"renamed": True, "old_name": old_name, "file": res})
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=file_id, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=WRITE)
async def move_file(file_id: str, new_folder_id: str) -> str:
    """Переместить файл или папку в другую папку. Внимание: если файл был в общей папке, люди с доступом
    только к ней перестанут его видеть — сообщите об этом пользователю. Обратимо (переместить обратно)."""
    tool = "move_file"
    try:
        require_write()

        def _do():
            require_scope(file_id, new_folder_id)
            meta = _get_meta(file_id, "id,name,parents,mimeType")
            if file_id == new_folder_id:
                raise RuntimeError("Нельзя переместить папку саму в себя")
            dest = _get_meta(new_folder_id, "id,mimeType")
            if dest.get("mimeType") != FOLDER_MIME:
                raise RuntimeError("new_folder_id не является папкой")
            prev = ",".join(meta.get("parents", []) or [])
            res = ex(svc("drive").files().update(fileId=file_id, addParents=new_folder_id, removeParents=prev or None,
                                                 fields="id,name,parents,webViewLink", **common_kwargs()))
            return prev, res
        prev, res = await run(_do)
        audit(tool, "ok", file_id=file_id, from_parents=prev, to=new_folder_id)
        return out({"moved": True, "previous_parents": prev, "file": res})
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=file_id, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=WRITE)
async def copy_file(file_id: str, new_name: str = "", folder_id: str = "") -> str:
    """Сделать копию файла (оригинал не меняется). new_name — имя копии; folder_id — куда положить копию."""
    tool = "copy_file"
    try:
        require_write()

        def _do():
            require_scope(file_id, folder_id)
            body: dict = {}
            if new_name:
                body["name"] = new_name
            if folder_id:
                body["parents"] = [folder_id]
            return ex(svc("drive").files().copy(fileId=file_id, body=body, fields="id,name,mimeType,parents,webViewLink", **common_kwargs()), idempotent=False)
        res = await run(_do)
        audit(tool, "ok", source=file_id, file_id=res.get("id"), name=res.get("name"))
        return out({"copied": True, "file": res})
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=file_id, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=DANGER)
async def delete_file(file_id: str, permanent: bool = False, confirm: bool = False) -> str:
    """Переместить файл в корзину (можно восстановить через restore_file в течение 30 дней).
    Удаление папок и безвозвратное удаление запрещены настройками по умолчанию.
    Требует подтверждения пользователя: без confirm=true ничего не удаляет и возвращает описание действия."""
    tool = "delete_file"
    try:
        require_write()

        def _do():
            require_scope(file_id)
            if file_id in safety.ALLOWED_FOLDER_IDS or (GDRIVE_ROOT_FOLDER_ID and file_id == GDRIVE_ROOT_FOLDER_ID):
                raise Blocked("Нельзя удалить корневую рабочую папку коннектора.")
            meta = _get_meta(file_id, "id,name,mimeType,trashed,owners(emailAddress),capabilities(canTrash,canDelete)")
            is_folder = meta["mimeType"] == FOLDER_MIME
            details = {"name": meta.get("name"), "file_id": file_id, "type": "папка" if is_folder else meta["mimeType"],
                       "owners": [o.get("emailAddress") for o in meta.get("owners", [])]}
            if permanent:
                if not safety.ALLOW_PERMANENT_DELETE:
                    raise Blocked("Безвозвратное удаление отключено (GDRIVE_ALLOW_PERMANENT_DELETE=false). "
                                  "Используйте удаление в корзину — оно обратимо.")
                if not meta.get("trashed"):
                    raise Blocked("Безвозвратно можно удалить только файл, который уже лежит в корзине. "
                                  "Сначала переместите его в корзину.")
            else:
                if not safety.ALLOW_TRASH:
                    raise Blocked("Удаление отключено настройкой GDRIVE_ALLOW_DELETE=false.")
                if meta.get("trashed"):
                    return {"trashed": True, "already": True, "file": details}
            if is_folder:
                if not safety.ALLOW_FOLDER_DELETE:
                    raise Blocked("Удаление папок отключено (GDRIVE_ALLOW_FOLDER_DELETE=false): вместе с папкой "
                                  "удалилось бы всё её содержимое. Удаляйте файлы по одному.")
                details["items_inside"] = _count_children(file_id)
            caps = meta.get("capabilities", {})
            if not permanent and caps and not caps.get("canTrash", True):
                raise Blocked("У подключённого аккаунта нет права удалять этот файл (обычно удалить может только владелец).")
            if confirmation_required(confirm):
                return needs_confirmation("Безвозвратное удаление" if permanent else "Перемещение в корзину", details,
                                          "Файл будет удалён НАВСЕГДА, восстановить нельзя." if permanent else
                                          "Файл попадёт в корзину; его можно восстановить в течение 30 дней.")
            if permanent:
                ex(svc("drive").files().delete(fileId=file_id, **common_kwargs()))
                return {"deleted": True, "permanent": True, "file": details}
            res = ex(svc("drive").files().update(fileId=file_id, body={"trashed": True}, fields="id,name,trashed", **common_kwargs()))
            return {"trashed": True, "file": res, "restore_hint": "restore_file(file_id) — вернуть из корзины"}
        res = await run(_do)
        audit(tool, "confirm_requested" if res.get("needs_confirmation") else "ok", file_id=file_id, permanent=permanent,
              details=res.get("details") or res.get("file"))
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=file_id, permanent=permanent, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=WRITE)
async def restore_file(file_id: str) -> str:
    """Восстановить файл или папку из корзины."""
    tool = "restore_file"
    try:
        require_write()

        def _do():
            require_scope(file_id)
            return ex(svc("drive").files().update(fileId=file_id, body={"trashed": False}, fields="id,name,trashed", **common_kwargs()))
        res = await run(_do)
        audit(tool, "ok", file_id=file_id, name=res.get("name"))
        return out({"restored": True, "file": res})
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=file_id, error=str(e)[:300])
        return err(e)


# ===================== ДОСТУП =====================

@mcp.tool(annotations=DANGER)
async def share_file(file_id: str, email: str = "", role: str = "reader", anyone_with_link: bool = False,
                     notify: bool = False, message: str = "", confirm: bool = False) -> str:
    """Открыть доступ к файлу/папке конкретному человеку по email. role — reader | commenter | writer
    (передача владения запрещена). Доступ «всем по ссылке» запрещён настройками по умолчанию.
    Требует подтверждения пользователя: без confirm=true ничего не меняет."""
    tool = "share_file"
    try:
        require_write()
        if not safety.ALLOW_SHARING:
            raise Blocked("Выдача доступа отключена настройкой GDRIVE_ALLOW_SHARING=false.")
        if role not in ("reader", "commenter", "writer"):
            raise Blocked("Допустимые роли: reader, commenter, writer. Передача владения через коннектор запрещена.")
        if anyone_with_link and not safety.ALLOW_PUBLIC_SHARING:
            raise Blocked("Доступ «всем, у кого есть ссылка» запрещён (GDRIVE_ALLOW_PUBLIC_SHARING=false) — это риск утечки данных. "
                          "Выдайте доступ конкретным людям по email.")
        if not anyone_with_link and ("@" not in email or " " in email.strip()):
            raise RuntimeError("Укажите корректный email или anyone_with_link=true")

        def _do():
            require_scope(file_id)
            meta = _get_meta(file_id, "id,name,mimeType,webViewLink")
            who = "все, у кого есть ссылка" if anyone_with_link else email.strip()
            if confirmation_required(confirm):
                return needs_confirmation("Выдача доступа", {"file": meta.get("name"), "file_id": file_id, "to": who,
                                                             "role": role, "is_folder": meta["mimeType"] == FOLDER_MIME},
                                          "Человек получит доступ к файлу" + (" и ко всему содержимому папки" if meta["mimeType"] == FOLDER_MIME else "") + ".")
            if anyone_with_link:
                body, kw = {"type": "anyone", "role": role}, {}
            else:
                body = {"type": "user", "role": role, "emailAddress": email.strip()}
                kw = {"sendNotificationEmail": bool(notify)}
                if notify and message:
                    kw["emailMessage"] = message
            perm = ex(svc("drive").permissions().create(fileId=file_id, body=body, fields="id,type,role,emailAddress", **kw, **common_kwargs()), idempotent=False)
            return {"shared": True, "permission": perm, "link": meta.get("webViewLink")}
        res = await run(_do)
        audit(tool, "confirm_requested" if res.get("needs_confirmation") else "ok", file_id=file_id, to=email or "anyone", role=role)
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=file_id, to=email, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=RO)
async def list_permissions(file_id: str) -> str:
    """Показать, у кого есть доступ к файлу или папке."""
    try:
        def _do():
            require_scope(file_id)
            return ex(svc("drive").permissions().list(fileId=file_id, fields="permissions(id,type,role,emailAddress,displayName)", **common_kwargs()))
        return out(await run(_do))
    except Exception as e:
        return err(e)


@mcp.tool(annotations=DANGER)
async def remove_permission(file_id: str, permission_id: str, confirm: bool = False) -> str:
    """Отозвать чей-то доступ к файлу (permission_id — из list_permissions). Доступ владельца отозвать нельзя.
    Требует подтверждения пользователя: без confirm=true ничего не меняет."""
    tool = "remove_permission"
    try:
        require_write()
        if not safety.ALLOW_REVOKE:
            raise Blocked("Отзыв доступа отключён настройкой GDRIVE_ALLOW_REVOKE=false.")

        def _do():
            require_scope(file_id)
            meta = _get_meta(file_id, "name")
            perm = ex(svc("drive").permissions().get(fileId=file_id, permissionId=permission_id,
                                                     fields="id,type,role,emailAddress,displayName", **common_kwargs()))
            if perm.get("role") == "owner":
                raise Blocked("Нельзя отозвать доступ владельца файла.")
            if confirmation_required(confirm):
                return needs_confirmation("Отзыв доступа", {"file": meta.get("name"), "who": perm.get("emailAddress") or perm.get("type"),
                                                            "role": perm.get("role")},
                                          "Этот человек потеряет доступ к файлу.")
            ex(svc("drive").permissions().delete(fileId=file_id, permissionId=permission_id, **common_kwargs()))
            return {"removed": True, "permission": perm}
        res = await run(_do)
        audit(tool, "confirm_requested" if res.get("needs_confirmation") else "ok", file_id=file_id,
              permission=res.get("permission") or res.get("details"))
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=file_id, permission_id=permission_id, error=str(e)[:300])
        return err(e)


# ===================== GOOGLE DOCS =====================

def _doc_end_index(doc: dict) -> int:
    content = doc.get("body", {}).get("content", [])
    return content[-1]["endIndex"] if content else 1


def _require_doc(document_id: str):
    require_scope(document_id)
    meta = _get_meta(document_id, "id,name,mimeType")
    if meta["mimeType"] != GDOC_MIME:
        raise RuntimeError(f"Файл «{meta.get('name')}» — не Google Документ ({meta['mimeType']}). "
                           "Docs-инструменты работают только с Google Docs; для docx сделайте копию в формате Google.")
    return meta


@mcp.tool(annotations=WRITE)
async def docs_create(title: str, content: str = "", folder_id: str = "") -> str:
    """Создать НОВЫЙ Google Doc. content — начальный текст; Markdown превращается в форматирование (заголовки, списки, жирный)."""
    tool = "docs_create"
    try:
        require_write()

        def _do():
            parent = default_parent(folder_id)
            require_scope(parent)
            body: dict = {"name": title, "mimeType": GDOC_MIME}
            if parent:
                body["parents"] = [parent]
            kw = {}
            if content:
                kw["media_body"] = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype="text/markdown")
            return ex(svc("drive").files().create(body=body, fields="id,name,webViewLink", **kw, **common_kwargs()), idempotent=False)
        res = await run(_do)
        audit(tool, "ok", file_id=res.get("id"), name=title)
        return out({"created": True, "document": res})
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", name=title, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=WRITE)
async def docs_append_text(document_id: str, text: str) -> str:
    """Добавить текст в конец Google Doc. Существующий текст не меняется."""
    tool = "docs_append_text"
    try:
        require_write()
        if not text:
            raise RuntimeError("Пустой текст")

        def _do():
            _require_doc(document_id)
            doc = ex(svc("docs").documents().get(documentId=document_id))
            end = _doc_end_index(doc) - 1
            reqs = [{"insertText": {"location": {"index": max(end, 1)}, "text": ("\n" if end > 1 else "") + text}}]
            ex(svc("docs").documents().batchUpdate(documentId=document_id, body={
                "requests": reqs, "writeControl": {"requiredRevisionId": doc["revisionId"]}}), idempotent=False)
            return {"appended": True, "document_id": document_id, "chars_added": len(text), "revision_before": doc.get("revisionId")}
        res = await run(_do)
        audit(tool, "ok", file_id=document_id, chars=len(text), revision_before=res.get("revision_before"))
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=document_id, error=str(e)[:300])
        return err(e)


def _replace(document_id: str, pairs: dict, match_case: bool, confirm: bool, tool: str) -> dict:
    for k in pairs:
        if not k or not k.strip():
            raise RuntimeError("Искомый текст не может быть пустым")
        if len(k.strip()) < 2:
            raise RuntimeError(f"Искомый текст «{k}» слишком короткий — заменились бы случайные места по всему документу")
    deleting = [k for k, v in pairs.items() if v == ""]
    meta = _require_doc(document_id)
    doc = ex(svc("docs").documents().get(documentId=document_id))
    text = "".join(tr.get("textRun", {}).get("content", "")
                   for el in doc.get("body", {}).get("content", [])
                   for tr in el.get("paragraph", {}).get("elements", []))
    counts = {k: (text.count(k) if match_case else text.lower().count(k.lower())) for k in pairs}
    big = {k: c for k, c in counts.items() if c > 20}
    if (deleting or big) and confirmation_required(confirm):
        return needs_confirmation("Массовая замена/удаление текста в документе",
                                  {"document": meta.get("name"), "occurrences": counts, "deleting": deleting},
                                  "Будет удалён текст или заменено много мест по всему документу. Старую версию можно вернуть через историю версий.")
    reqs = [{"replaceAllText": {"containsText": {"text": k, "matchCase": match_case}, "replaceText": v}} for k, v in pairs.items()]
    r = ex(svc("docs").documents().batchUpdate(documentId=document_id, body={
        "requests": reqs, "writeControl": {"requiredRevisionId": doc["revisionId"]}}))
    replaced = {k: rep.get("replaceAllText", {}).get("occurrencesChanged", 0) for k, rep in zip(pairs.keys(), r.get("replies", []))}
    return {"document_id": document_id, "replaced": replaced, "revision_before": doc.get("revisionId"),
            "not_found": [k for k, n in replaced.items() if n == 0]}


@mcp.tool(annotations=WRITE)
async def docs_replace_text(document_id: str, find: str, replace_with: str, match_case: bool = True, confirm: bool = False) -> str:
    """Найти и заменить текст во всём Google Doc (все вхождения), остальное форматирование сохраняется.
    Для шаблонов: find='{{client_name}}', replace_with='ООО Ромашка'. Удаление текста (пустая замена) или
    замена более 20 мест требует подтверждения пользователя (confirm=true)."""
    tool = "docs_replace_text"
    try:
        require_write()
        res = await run(_replace, document_id, {find: replace_with}, match_case, confirm, tool)
        audit(tool, "confirm_requested" if res.get("needs_confirmation") else "ok", file_id=document_id, find=find,
              replace_with=replace_with, result=res.get("replaced"), revision_before=res.get("revision_before"))
        if "replaced" in res:
            res["replaced_count"] = res["replaced"].get(find, 0)
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=document_id, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=WRITE)
async def docs_replace_many(document_id: str, replacements: dict[str, str], match_case: bool = True, confirm: bool = False) -> str:
    """Несколько замен за один раз: replacements = {"что найти": "на что заменить", ...}. Идеально для заполнения
    шаблонов договоров/счетов. Пустая замена или >20 вхождений одного фрагмента требуют confirm=true.
    В ответе not_found — плейсхолдеры, которых нет в документе."""
    tool = "docs_replace_many"
    try:
        require_write()
        if not replacements:
            raise RuntimeError("Пустой список замен")
        res = await run(_replace, document_id, dict(replacements), match_case, confirm, tool)
        audit(tool, "confirm_requested" if res.get("needs_confirmation") else "ok", file_id=document_id,
              replacements=replacements, result=res.get("replaced"), revision_before=res.get("revision_before"))
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=document_id, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=WRITE)
async def docs_insert_text(document_id: str, text: str, after_text: str = "", at_start: bool = False) -> str:
    """Вставить текст в Google Doc: после первого вхождения after_text, в начало (at_start=true) или в конец.
    Существующий текст не удаляется."""
    tool = "docs_insert_text"
    try:
        require_write()
        if not text:
            raise RuntimeError("Пустой текст")

        def _do():
            _require_doc(document_id)
            doc = ex(svc("docs").documents().get(documentId=document_id))
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
                    raise RuntimeError(f"Текст «{after_text}» не найден (или разбит форматированием) — ничего не вставлено")
            else:
                idx = max(_doc_end_index(doc) - 1, 1)
            ex(svc("docs").documents().batchUpdate(documentId=document_id, body={
                "requests": [{"insertText": {"location": {"index": idx}, "text": text}}],
                "writeControl": {"requiredRevisionId": doc["revisionId"]}}), idempotent=False)
            return {"inserted": True, "index": idx, "document_id": document_id, "revision_before": doc.get("revisionId")}
        res = await run(_do)
        audit(tool, "ok", file_id=document_id, chars=len(text), index=res.get("index"))
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=document_id, error=str(e)[:300])
        return err(e)


# ===================== GOOGLE SHEETS =====================

def _require_sheet(spreadsheet_id: str):
    require_scope(spreadsheet_id)
    meta = _get_meta(spreadsheet_id, "id,name,mimeType")
    if meta["mimeType"] != GSHEET_MIME:
        raise RuntimeError(f"Файл «{meta.get('name')}» — не Google Таблица ({meta['mimeType']}). "
                           "Для xlsx сделайте копию в формате Google (upload_file с convert_to_google).")
    return meta


def _formula_errors(spreadsheet_id: str, rng: str) -> list:
    try:
        vals = ex(svc("sheets").spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng)).get("values", [])
    except HttpError:
        return []
    found = []
    for r, row in enumerate(vals):
        for c, v in enumerate(row):
            if isinstance(v, str) and v.strip() in SHEET_ERRORS:
                found.append({"row": r + 1, "col": c + 1, "value": v})
    return found[:50]


@mcp.tool(annotations=WRITE)
async def sheets_create(title: str, sheet_names: list[str] | None = None, folder_id: str = "") -> str:
    """Создать НОВУЮ Google-таблицу. sheet_names — названия листов (необязательно)."""
    tool = "sheets_create"
    try:
        require_write()

        def _do():
            parent = default_parent(folder_id)
            require_scope(parent)
            body: dict = {"name": title, "mimeType": GSHEET_MIME}
            if parent:
                body["parents"] = [parent]
            f = ex(svc("drive").files().create(body=body, fields="id,name,webViewLink", **common_kwargs()), idempotent=False)
            sid = f["id"]
            if sheet_names:
                info = ex(svc("sheets").spreadsheets().get(spreadsheetId=sid, fields="sheets(properties(sheetId))"))
                first = info["sheets"][0]["properties"]["sheetId"]
                reqs = [{"updateSheetProperties": {"properties": {"sheetId": first, "title": sheet_names[0]}, "fields": "title"}}]
                reqs += [{"addSheet": {"properties": {"title": n}}} for n in sheet_names[1:]]
                ex(svc("sheets").spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": reqs}), idempotent=False)
            return {"spreadsheetId": sid, "spreadsheetUrl": f.get("webViewLink"), "name": f.get("name")}
        res = await run(_do)
        audit(tool, "ok", file_id=res.get("spreadsheetId"), name=title)
        return out({"created": True, "spreadsheet": res})
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", name=title, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=RO)
async def sheets_get_info(spreadsheet_id: str) -> str:
    """Структура Google-таблицы: название, локаль (важно для формул), листы и их размеры."""
    try:
        def _do():
            _require_sheet(spreadsheet_id)
            return ex(svc("sheets").spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="properties(title,locale),spreadsheetUrl,sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)))"))
        res = await run(_do)
        loc = res.get("properties", {}).get("locale", "")
        res["formula_separator"] = ";" if loc and not loc.startswith(("en", "ja", "zh", "ko", "th", "he")) else ","
        return out(res)
    except Exception as e:
        return err(e)


@mcp.tool(annotations=RO)
async def sheets_read(spreadsheet_id: str, range: str = "", value_render: str = "FORMATTED_VALUE") -> str:
    """Прочитать данные Google-таблицы. range в A1-нотации: "'Лист1'!A1:D50", "'Лист1'" (весь лист). Пусто — все листы.
    value_render: FORMATTED_VALUE (как видно), UNFORMATTED_VALUE (сырые числа), FORMULA (формулы)."""
    try:
        if value_render not in ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"):
            raise RuntimeError("value_render: FORMATTED_VALUE | UNFORMATTED_VALUE | FORMULA")

        def _do():
            _require_sheet(spreadsheet_id)
            sh = svc("sheets").spreadsheets()
            if range:
                return ex(sh.values().get(spreadsheetId=spreadsheet_id, range=range, valueRenderOption=value_render))
            meta = ex(sh.get(spreadsheetId=spreadsheet_id, fields="sheets(properties(title))"))
            ranges = ["'" + s["properties"]["title"].replace("'", "''") + "'" for s in meta.get("sheets", [])]
            return ex(sh.values().batchGet(spreadsheetId=spreadsheet_id, ranges=ranges, valueRenderOption=value_render))
        return out(await run(_do))
    except Exception as e:
        return err(e)


@mcp.tool(annotations=WRITE)
async def sheets_write(spreadsheet_id: str, range: str, values: list[list[Any]], as_user_input: bool = True, confirm: bool = False) -> str:
    """Записать данные в ячейки Google-таблицы. range — начальная ячейка или диапазон ("'Лист1'!A1"),
    values — [["Имя","Сумма"],["Иван",1000]]. Запись в ПУСТЫЕ ячейки выполняется сразу; если будут затёрты
    непустые ячейки — нужен confirm=true после согласия пользователя (старые значения сохраняются в журнал).
    Формулы пишутся в синтаксисе локали таблицы (см. sheets_get_info → formula_separator): для ru_RU разделитель ';',
    например '=IFERROR(ROUND(G2/F2;0);"")'. Дробные числа передавайте числом, не строкой.
    В ответе formula_errors — ячейки с ошибками после записи (проверьте и исправьте)."""
    tool = "sheets_write"
    try:
        require_write()
        if not values or not isinstance(values, list):
            raise RuntimeError("values должен быть непустым двумерным массивом")
        values = [r if isinstance(r, list) else [r] for r in values]
        if _cells(values) > safety.MAX_SHEET_CELLS:
            raise Blocked(f"Слишком много ячеек за раз ({_cells(values)} > {safety.MAX_SHEET_CELLS}). Разбейте запись на части.")

        def _do():
            meta = _require_sheet(spreadsheet_id)
            vals_api = svc("sheets").spreadsheets().values()
            # Сначала узнаём точный целевой диапазон и что в нём сейчас.
            probe = ex(vals_api.get(spreadsheetId=spreadsheet_id, range=range, valueRenderOption="FORMULA"))
            target = _target_range(probe.get("range", range), len(values), max(len(r) for r in values))
            current = ex(vals_api.get(spreadsheetId=spreadsheet_id, range=target, valueRenderOption="FORMULA")).get("values", [])
            non_empty = sum(1 for row in current for v in row if v not in ("", None))
            if non_empty and confirmation_required(confirm):
                sample = [row[:6] for row in current[:5]]
                return needs_confirmation("Перезапись заполненных ячеек", {
                    "spreadsheet": meta.get("name"), "range": target, "non_empty_cells_to_overwrite": non_empty,
                    "current_values_sample": sample},
                    "Будут заменены существующие данные/формулы. Старые значения сохранятся в журнале коннектора "
                    "и в истории версий таблицы.")
            if non_empty:
                audit(tool, "backup", file_id=spreadsheet_id, range=target, previous_values=json.dumps(current, ensure_ascii=False))
            res = ex(vals_api.update(spreadsheetId=spreadsheet_id, range=target,
                                     valueInputOption="USER_ENTERED" if as_user_input else "RAW", body={"values": values}))
            res["overwritten_non_empty_cells"] = non_empty
            res["formula_errors"] = _formula_errors(spreadsheet_id, res.get("updatedRange", target))
            if res["formula_errors"]:
                res["hint"] = ("В ячейках ошибки формул. Частая причина — разделитель аргументов: для русской локали "
                               "используйте ';' вместо ','. Исправьте и перезапишите эти ячейки.")
            return res
        res = await run(_do)
        audit(tool, "confirm_requested" if res.get("needs_confirmation") else "ok", file_id=spreadsheet_id, range=range,
              cells=_cells(values), overwritten=res.get("overwritten_non_empty_cells"))
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=spreadsheet_id, range=range, error=str(e)[:300])
        return err(e)


def _col_to_num(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n


def _num_to_col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _target_range(api_range: str, rows: int, cols: int) -> str:
    """Из нормализованного диапазона API ('Лист1'!B2:D5 или 'Лист1'!B2) делаем ровно rows×cols от его левого верхнего угла."""
    import re
    sheet, _, cells = api_range.rpartition("!")
    start = cells.split(":")[0]
    m = re.match(r"^([A-Za-z]+)(\d+)$", start)
    if not m:
        return api_range
    c0, r0 = _col_to_num(m.group(1)), int(m.group(2))
    end = f"{_num_to_col(c0 + cols - 1)}{r0 + rows - 1}"
    return f"{sheet}!{m.group(1).upper()}{r0}:{end}" if sheet else f"{m.group(1).upper()}{r0}:{end}"


@mcp.tool(annotations=WRITE)
async def sheets_append(spreadsheet_id: str, range: str, values: list[list[Any]], as_user_input: bool = True) -> str:
    """Добавить строки в КОНЕЦ таблицы (после последней заполненной строки) — существующие данные не затираются.
    range — лист или столбцы, например "'Лист1'" или "'Лист1'!A:F". Формулы — в синтаксисе локали (для ru_RU ';')."""
    tool = "sheets_append"
    try:
        require_write()
        if not values or not isinstance(values, list):
            raise RuntimeError("values должен быть непустым двумерным массивом")
        values = [r if isinstance(r, list) else [r] for r in values]
        if _cells(values) > safety.MAX_SHEET_CELLS:
            raise Blocked(f"Слишком много ячеек за раз ({_cells(values)} > {safety.MAX_SHEET_CELLS}). Разбейте на части.")

        def _do():
            _require_sheet(spreadsheet_id)
            res = ex(svc("sheets").spreadsheets().values().append(
                spreadsheetId=spreadsheet_id, range=range, valueInputOption="USER_ENTERED" if as_user_input else "RAW",
                insertDataOption="INSERT_ROWS", body={"values": values}), idempotent=False)
            upd = res.get("updates", {}).get("updatedRange")
            res["formula_errors"] = _formula_errors(spreadsheet_id, upd) if upd else []
            if res["formula_errors"]:
                res["hint"] = "В добавленных строках ошибки формул — для русской локали используйте ';' вместо ','."
            return res
        res = await run(_do)
        audit(tool, "ok", file_id=spreadsheet_id, range=res.get("updates", {}).get("updatedRange"), cells=_cells(values))
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=spreadsheet_id, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=DANGER)
async def sheets_clear(spreadsheet_id: str, range: str, confirm: bool = False) -> str:
    """Очистить значения в диапазоне (форматирование остаётся). Нельзя очистить лист/таблицу целиком без диапазона ячеек.
    Требует подтверждения пользователя; старые значения сохраняются в журнал коннектора."""
    tool = "sheets_clear"
    try:
        require_write()
        if "!" not in range and ":" not in range:
            raise Blocked("Укажите конкретный диапазон ячеек, например \"'Лист1'!A2:F100\". Очистка целого листа запрещена.")

        def _do():
            meta = _require_sheet(spreadsheet_id)
            vals_api = svc("sheets").spreadsheets().values()
            current = ex(vals_api.get(spreadsheetId=spreadsheet_id, range=range, valueRenderOption="FORMULA"))
            rows = current.get("values", [])
            non_empty = sum(1 for row in rows for v in row if v not in ("", None))
            if not non_empty:
                return {"cleared": False, "message": "Диапазон уже пуст", "range": current.get("range")}
            if ":" not in range.rpartition("!")[2]:
                raise Blocked("Укажите диапазон ячеек (A2:F100), а не целый лист.")
            if confirmation_required(confirm):
                return needs_confirmation("Очистка ячеек", {"spreadsheet": meta.get("name"), "range": current.get("range"),
                                                            "non_empty_cells": non_empty, "sample": [r[:6] for r in rows[:5]]},
                                          "Значения и формулы в диапазоне будут удалены (копия сохранится в журнале и истории версий).")
            audit(tool, "backup", file_id=spreadsheet_id, range=current.get("range"), previous_values=json.dumps(rows, ensure_ascii=False))
            res = ex(vals_api.clear(spreadsheetId=spreadsheet_id, range=range, body={}))
            res["cleared_cells"] = non_empty
            return res
        res = await run(_do)
        audit(tool, "confirm_requested" if res.get("needs_confirmation") else "ok", file_id=spreadsheet_id, range=range,
              cleared=res.get("cleared_cells"))
        return out(res)
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=spreadsheet_id, range=range, error=str(e)[:300])
        return err(e)


@mcp.tool(annotations=WRITE)
async def sheets_add_sheet(spreadsheet_id: str, title: str) -> str:
    """Добавить новый пустой лист в Google-таблицу. Существующие листы не меняются."""
    tool = "sheets_add_sheet"
    try:
        require_write()

        def _do():
            _require_sheet(spreadsheet_id)
            res = ex(svc("sheets").spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": [{"addSheet": {"properties": {"title": title}}}]}), idempotent=False)
            return res["replies"][0]["addSheet"]["properties"]
        props = await run(_do)
        audit(tool, "ok", file_id=spreadsheet_id, title=title)
        return out({"added": True, "sheet": props})
    except Exception as e:
        audit(tool, "blocked" if isinstance(e, Blocked) else "error", file_id=spreadsheet_id, error=str(e)[:300])
        return err(e)


# ===================== HTTP: АВТОРИЗАЦИЯ, СТРАНИЦЫ, OAUTH =====================

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
<p>Содержимое файлов не сохраняется на сервере сервиса дольше времени обработки запроса, за исключением
журнала изменений (для возможности восстановления данных). На сервере хранится токен доступа, выданный владельцем аккаунта.</p>
<h2>Отзыв доступа</h2>
<p>Владелец может в любой момент отозвать доступ на странице
<a href="https://myaccount.google.com/permissions">https://myaccount.google.com/permissions</a>.</p>
<p>Использование данных, полученных через API Google, соответствует
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>,
включая требования Limited Use.</p>
{f'<h2>Контакты</h2><p>{CONTACT_EMAIL}</p>' if CONTACT_EMAIL else ''}
</body></html>"""


def _token_ok(token: str) -> bool:
    return bool(MCP_AUTH_TOKEN) and bool(token) and hmac.compare_digest(token.encode(), MCP_AUTH_TOKEN.encode())


def check_http_auth_config():
    """Сервер по HTTP без токена = чужие люди получают доступ к диску клиента. Не запускаемся."""
    if not MCP_AUTH_TOKEN and not MCP_ALLOW_NO_AUTH:
        raise RuntimeError("MCP_AUTH_TOKEN не задан. Запуск по HTTP без токена запрещён (это открыло бы диск всем). "
                           "Сгенерируйте токен: openssl rand -hex 32. (Только для локальной отладки: MCP_ALLOW_NO_AUTH=true)")
    if MCP_AUTH_TOKEN and len(MCP_AUTH_TOKEN) < 24:
        raise RuntimeError("MCP_AUTH_TOKEN слишком короткий (минимум 24 символа). Сгенерируйте: openssl rand -hex 32")


def build_http_app():
    """HTTP-приложение (streamable-http или sse) с проверкой токена, /health, /, /privacy и входом в Google."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

    check_http_auth_config()
    app = mcp.sse_app() if MCP_TRANSPORT == "sse" else mcp.streamable_http_app()

    async def oauth_route(request, path):
        import oauth_web
        if path == "/oauth/start":
            if not _token_ok(request.query_params.get("token", "")):
                return HTMLResponse(oauth_web.page("Нет доступа", "<p>Откройте ссылку с параметром ?token=MCP_AUTH_TOKEN.</p>"), status_code=401)
            return RedirectResponse(oauth_web.start_url(request))
        if request.query_params.get("error"):
            return HTMLResponse(oauth_web.page("Доступ не выдан", "<p>Вход в Google отменён.</p>"), status_code=400)
        if not oauth_web.check_state(request.query_params.get("state", "")):
            return HTMLResponse(oauth_web.page("Ссылка устарела", "<p>Начните заново с /oauth/start.</p>"), status_code=400)
        try:
            await run(oauth_web.exchange_code, request, request.query_params.get("code", ""))
        except Exception as e:
            return HTMLResponse(oauth_web.page("Ошибка", f"<p>{str(e)[:300]}</p>"), status_code=500)
        reset_credentials()
        audit("oauth_connect", "ok")
        return HTMLResponse(oauth_web.page("Google Drive подключён ✅", "<p>Доступ сохранён. Это окно можно закрыть.</p>"))

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path.rstrip("/") or "/"
            if path == "/health":
                return JSONResponse({"status": "ok", "service": "gdrive-mcp", "version": __version__})
            if path == "/" and request.method == "GET":
                return HTMLResponse(HOME_HTML)
            if path == "/privacy" and request.method == "GET":
                return HTMLResponse(PRIVACY_HTML)
            if path in ("/oauth/start", "/oauth/callback"):
                return await oauth_route(request, path)
            if MCP_AUTH_TOKEN:
                auth = request.headers.get("authorization", "")
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                token = token or request.headers.get("x-api-key", "") or request.query_params.get("token", "")
                if not _token_ok(token):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app.add_middleware(AuthMiddleware)
    return app


def main():
    print(f"gdrive-mcp {__version__}; transport={MCP_TRANSPORT}; safety={json.dumps(safety.settings_summary(), ensure_ascii=False)}",
          file=sys.stderr)
    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(build_http_app(), host=MCP_HOST, port=MCP_PORT, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
