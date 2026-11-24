"""
Защитный слой коннектора: настройки безопасности, проверки перед опасными
операциями, подтверждения и журнал действий (аудит).

Принцип: по умолчанию коннектор НЕ может сделать ничего необратимого.
Всё, что может потерять данные или открыть к ним доступ, либо запрещено
настройкой, либо требует явного подтверждения пользователя (confirm=true).
"""

import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any


def flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "да")


def num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ===================== НАСТРОЙКИ БЕЗОПАСНОСТИ =====================

# Полный запрет любых изменений: только поиск и чтение.
READ_ONLY = flag("GDRIVE_READ_ONLY", False)
# Перемещение в корзину (обратимо 30 дней).
ALLOW_TRASH = flag("GDRIVE_ALLOW_DELETE", True)
# Удаление папок (в корзину вместе со всем содержимым).
ALLOW_FOLDER_DELETE = flag("GDRIVE_ALLOW_FOLDER_DELETE", False)
# Безвозвратное удаление. Даже если включено — только для файлов, уже лежащих в корзине.
ALLOW_PERMANENT_DELETE = flag("GDRIVE_ALLOW_PERMANENT_DELETE", False)
# Полная замена содержимого обычных файлов (docx, xlsx, pdf...). Google Docs/Sheets так не перезаписываются никогда.
ALLOW_OVERWRITE = flag("GDRIVE_ALLOW_OVERWRITE", True)
# Выдача доступа конкретным людям по email.
ALLOW_SHARING = flag("GDRIVE_ALLOW_SHARING", True)
# Доступ «всем, у кого есть ссылка» — риск утечки, по умолчанию запрещён.
ALLOW_PUBLIC_SHARING = flag("GDRIVE_ALLOW_PUBLIC_SHARING", False)
# Отзыв чужих доступов.
ALLOW_REVOKE = flag("GDRIVE_ALLOW_REVOKE", True)
# Требовать подтверждение (confirm=true) для опасных действий.
REQUIRE_CONFIRM = flag("GDRIVE_REQUIRE_CONFIRM", True)

# Жёсткое ограничение папками: коннектор видит и меняет только то, что лежит внутри них (на любой глубине).
ALLOWED_FOLDER_IDS = [x.strip() for x in os.environ.get("GDRIVE_ALLOWED_FOLDER_IDS", "").split(",") if x.strip()]
_root = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "").strip()
if _root and flag("GDRIVE_RESTRICT_TO_ROOT", False) and _root not in ALLOWED_FOLDER_IDS:
    ALLOWED_FOLDER_IDS.append(_root)

MAX_SHEET_CELLS = int(num("GDRIVE_MAX_SHEET_CELLS", 20000))
MAX_BACKUP_CHARS = int(num("GDRIVE_MAX_BACKUP_CHARS", 300000))

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG = os.environ.get("GDRIVE_AUDIT_LOG") or os.path.join(HERE, "logs", "audit.log")


class Blocked(Exception):
    """Действие запрещено настройками безопасности."""


def require_write():
    if READ_ONLY:
        raise Blocked("Коннектор работает в режиме «только чтение» (GDRIVE_READ_ONLY=true). Изменения запрещены.")


def needs_confirmation(action: str, details: dict, warning: str) -> dict:
    """Ответ вместо выполнения: модель должна спросить пользователя и повторить вызов с confirm=true."""
    return {
        "needs_confirmation": True,
        "action": action,
        "warning": warning,
        "details": details,
        "next_step": ("НИЧЕГО НЕ ИЗМЕНЕНО. Перескажи пользователю, что именно будет сделано, "
                      "получи его явное согласие в чате и только после этого вызови этот же инструмент "
                      "повторно с теми же параметрами и confirm=true."),
    }


def confirmation_required(confirm: bool) -> bool:
    return REQUIRE_CONFIRM and not confirm


# ===================== ЖУРНАЛ ДЕЙСТВИЙ =====================

_audit = logging.getLogger("gdrive.audit")
_audit.setLevel(logging.INFO)
_audit.propagate = False
try:
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    _h: logging.Handler = RotatingFileHandler(AUDIT_LOG, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8")
except Exception:  # нет прав на запись — пишем в stderr, но не падаем
    _h = logging.StreamHandler(sys.stderr)
_h.setFormatter(logging.Formatter("%(message)s"))
_audit.addHandler(_h)


def audit(tool: str, status: str, **fields: Any):
    """Одна строка JSON на каждое изменяющее действие (и на каждую заблокированную попытку)."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "status": status}
    for k, v in fields.items():
        if isinstance(v, str) and len(v) > MAX_BACKUP_CHARS:
            v = v[:MAX_BACKUP_CHARS] + "…[обрезано]"
        rec[k] = v
    try:
        _audit.info(json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:
        pass


def settings_summary() -> dict:
    return {
        "read_only": READ_ONLY,
        "allow_trash": ALLOW_TRASH,
        "allow_folder_delete": ALLOW_FOLDER_DELETE,
        "allow_permanent_delete": ALLOW_PERMANENT_DELETE,
        "allow_overwrite": ALLOW_OVERWRITE,
        "allow_sharing": ALLOW_SHARING,
        "allow_public_sharing": ALLOW_PUBLIC_SHARING,
        "allow_revoke": ALLOW_REVOKE,
        "require_confirm": REQUIRE_CONFIRM,
        "allowed_folders": ALLOWED_FOLDER_IDS or "весь диск",
    }
