import asyncio
import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_tmp = tempfile.mkdtemp()
os.environ.update({
    "MCP_TRANSPORT": "streamable-http",
    "MCP_AUTH_TOKEN": "t" * 40,
    "GDRIVE_AUDIT_LOG": os.path.join(_tmp, "audit.log"),
    "GDRIVE_TOKEN_FILE": os.path.join(_tmp, "token.json"),
    "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "sec",
})

import fake_google  # noqa: E402
import safety  # noqa: E402
import server  # noqa: E402


@pytest.fixture
def world(monkeypatch):
    w = fake_google.World()
    services = {"drive": fake_google.Drive(w), "sheets": fake_google.Sheets(w), "docs": fake_google.Docs(w)}
    monkeypatch.setattr(server, "svc", lambda name: services[name])
    # Безопасные настройки по умолчанию на каждый тест
    for k, v in dict(READ_ONLY=False, ALLOW_TRASH=True, ALLOW_FOLDER_DELETE=False, ALLOW_PERMANENT_DELETE=False,
                     ALLOW_OVERWRITE=True, ALLOW_SHARING=True, ALLOW_PUBLIC_SHARING=False, ALLOW_REVOKE=True,
                     REQUIRE_CONFIRM=True, ALLOWED_FOLDER_IDS=[]).items():
        monkeypatch.setattr(safety, k, v)
    monkeypatch.setattr(server, "GDRIVE_ROOT_FOLDER_ID", "")
    server._scope_cache.clear()
    return w


def call(tool, **kw):
    return json.loads(asyncio.run(tool(**kw)))


def audit_lines():
    with open(os.environ["GDRIVE_AUDIT_LOG"], encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]
