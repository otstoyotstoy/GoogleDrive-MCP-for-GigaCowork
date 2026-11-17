"""
Получение GOOGLE_REFRESH_TOKEN для коннектора (одноразово, на своём компьютере).

1. Скачайте OAuth client (тип "Desktop app") из Google Cloud Console как client_secret.json
   и положите рядом с этим скриптом.
2. Запустите:  python get_token.py
3. Откроется браузер — войдите в Google-аккаунт, к диску которого нужен доступ, и разрешите доступ.
4. Скрипт выведет строки для .env — скопируйте их на сервер.
"""
import json
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

path = sys.argv[1] if len(sys.argv) > 1 else "client_secret.json"
flow = InstalledAppFlow.from_client_secrets_file(path, SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

with open(path, encoding="utf-8") as f:
    cfg = json.load(f)
client = cfg.get("installed") or cfg.get("web")

print("\n=== Добавьте в .env ===\n")
print(f"GOOGLE_CLIENT_ID={client['client_id']}")
print(f"GOOGLE_CLIENT_SECRET={client['client_secret']}")
print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
