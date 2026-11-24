"""HTTP-авторизация, многопоточность, регистрация инструментов."""
import asyncio
import threading

import pytest
from starlette.testclient import TestClient

import server

TOKEN = "t" * 40


def test_all_tools_registered_with_annotations():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert len(names) == 29
    danger = {t.name for t in tools if t.annotations and t.annotations.destructiveHint}
    assert {"delete_file", "update_file_content", "sheets_clear", "share_file", "remove_permission"} <= danger
    ro = {t.name for t in tools if t.annotations and t.annotations.readOnlyHint}
    assert {"read_file", "search_files", "sheets_read"} <= ro
    for t in tools:
        if t.name in danger:
            assert "confirm" in t.inputSchema["properties"], t.name


def test_http_refuses_to_start_without_token(monkeypatch):
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", "")
    monkeypatch.setattr(server, "MCP_ALLOW_NO_AUTH", False)
    with pytest.raises(RuntimeError):
        server.build_http_app()


def test_http_refuses_short_token(monkeypatch):
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", "short")
    with pytest.raises(RuntimeError):
        server.build_http_app()


@pytest.fixture(scope="module")
def client():
    with TestClient(server.build_http_app()) as c:
        yield c


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HDR = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def test_http_auth(client):
    assert client.get("/health").status_code == 200
    assert client.get("/privacy").status_code == 200
    assert client.post("/mcp", json=INIT, headers=HDR).status_code == 401
    assert client.post("/mcp", json=INIT, headers={**HDR, "authorization": "Bearer wrong" + "x" * 30}).status_code == 401
    assert client.post("/mcp", json=INIT, headers={**HDR, "authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.post("/mcp", json=INIT, headers={**HDR, "x-api-key": TOKEN}).status_code == 200
    assert client.post(f"/mcp?token={TOKEN}", json=INIT, headers=HDR).status_code == 200


def test_oauth_start_requires_token(client):
    assert client.get("/oauth/start", follow_redirects=False).status_code == 401
    r = client.get(f"/oauth/start?token={TOKEN}", follow_redirects=False)
    assert r.status_code in (302, 307) and "accounts.google.com" in r.headers["location"]
    assert client.get("/oauth/callback?code=x&state=forged.sig").status_code == 400


def test_google_clients_are_per_thread(monkeypatch):
    built = []
    monkeypatch.setattr(server, "get_credentials", lambda: object())
    monkeypatch.setattr(server, "build", lambda *a, **k: built.append(1) or object())
    server.reset_credentials()
    got = []

    def worker():
        got.append(server.svc("drive"))
        got.append(server.svc("drive"))
    ts = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # в каждом потоке свой клиент, и внутри потока он переиспользуется
    assert len(built) == 3
    assert got[0] is got[1]


def test_auth_problem_message(monkeypatch):
    from google.auth.exceptions import RefreshError

    class C:
        valid = False

        def refresh(self, req):
            raise RefreshError("invalid_grant")
    monkeypatch.setattr(server, "_creds", C())
    with pytest.raises(server.AuthProblem) as e:
        server.get_credentials()
    assert "oauth/start" in str(e.value)
    monkeypatch.setattr(server, "_creds", None)


def test_tool_call_over_mcp_protocol_respects_confirm(client, world):
    """Полный путь как у платформы: JSON-RPC tools/call по HTTP. Без confirm файл остаётся на месте."""
    import json as _json
    fid = world.add("Договор.docx")
    h = {**HDR, "authorization": f"Bearer {TOKEN}", "mcp-protocol-version": "2025-06-18"}
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete_file", "arguments": {"file_id": fid}}}
    r = client.post("/mcp", json=body, headers=h)
    assert r.status_code == 200
    text = r.text
    payload = _json.loads(text[text.index("{"):]) if not text.startswith("{") else r.json()
    if "data:" in text:
        payload = _json.loads(text.split("data:", 1)[1].strip().splitlines()[0])
    inner = _json.loads(payload["result"]["content"][0]["text"])
    assert inner["needs_confirmation"] is True
    assert world.files[fid]["trashed"] is False


def test_wsgi_entrypoint_for_shared_hosting():
    """Запуск как у Passenger (reg.ru): отдельный процесс, WSGI → ASGI."""
    import os as _os
    import subprocess
    import sys as _sys
    code = (
        "import io,json\n"
        "from wsgiref.util import setup_testing_defaults\n"
        "import wsgi\n"
        "def call(path, method='GET', body=b'', hdr=None):\n"
        "    env={}; setup_testing_defaults(env); env['PATH_INFO']=path; env['REQUEST_METHOD']=method\n"
        "    env['wsgi.input']=io.BytesIO(body); env['CONTENT_LENGTH']=str(len(body)); env['CONTENT_TYPE']='application/json'\n"
        "    for k,v in (hdr or {}).items(): env['HTTP_'+k.upper().replace('-','_')]=v\n"
        "    st=[]; out=b''.join(wsgi.application(env, lambda s,h,e=None: st.append(s))); return st[0], out\n"
        "print(call('/health')[0])\n"
        f"init=json.dumps({INIT!r}).encode()\n"
        "print(call('/mcp','POST',init,{'accept':'application/json, text/event-stream'})[0])\n"
        f"print(call('/mcp','POST',init,{{'accept':'application/json, text/event-stream','authorization':'Bearer {TOKEN}'}})[0])\n"
    )
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    env = dict(_os.environ, MCP_JSON_RESPONSE="true")
    r = subprocess.run([_sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    lines = [x for x in r.stdout.splitlines() if x.strip()]
    assert lines == ["200 OK", "401 Unauthorized", "200 OK"], (r.stdout, r.stderr[-2000:])
