"""Вход в Google через браузер: /oauth/start -> Google -> /oauth/callback.
Refresh-токен сохраняется в файл (GDRIVE_TOKEN_FILE, по умолчанию token.json рядом с сервером)."""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.environ.get("GDRIVE_TOKEN_FILE") or os.path.join(HERE, "token.json")
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _cfg():
    return (os.environ.get("GOOGLE_CLIENT_ID", ""), os.environ.get("GOOGLE_CLIENT_SECRET", ""))


def redirect_uri(request) -> str:
    env = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI")
    if env:
        return env
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"https://{host}/oauth/callback"


def _sign(ts: str) -> str:
    _, secret = _cfg()
    return hmac.new(secret.encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]


def start_url(request) -> str:
    client_id, _ = _cfg()
    ts = str(int(time.time()))
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "scope": " ".join(SCOPES),
        "state": f"{ts}.{_sign(ts)}",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def check_state(state: str) -> bool:
    try:
        ts, sig = state.split(".", 1)
        return hmac.compare_digest(sig, _sign(ts)) and time.time() - int(ts) < 1800
    except Exception:
        return False


def exchange_code(request, code: str) -> dict:
    client_id, secret = _cfg()
    data = urllib.parse.urlencode({
        "code": code, "client_id": client_id, "client_secret": secret,
        "redirect_uri": redirect_uri(request), "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data, timeout=30) as r:
        tok = json.loads(r.read().decode())
    if not tok.get("refresh_token"):
        raise RuntimeError("Google не вернул refresh_token. Отзовите доступ на myaccount.google.com/permissions и повторите.")
    save_refresh_token(tok["refresh_token"])
    return tok


def save_refresh_token(rt: str):
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"refresh_token": rt, "saved_at": int(time.time())}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)


def load_refresh_token() -> str:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f).get("refresh_token", "")
    except Exception:
        return ""


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{title}</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            '<body style="font-family:system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 16px;line-height:1.5">'
            f'<h1>{title}</h1>{body}</body></html>')
