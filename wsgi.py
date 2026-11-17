"""WSGI-обёртка для запуска на виртуальном хостинге через Passenger (reg.ru и т.п.).
MCP-сервер работает в фоновом event loop, a2wsgi переводит WSGI-запросы в ASGI."""
import asyncio
import threading

from a2wsgi import ASGIMiddleware

import server

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()

_asgi = server.build_http_app()
_ready = threading.Event()


async def _keep_session_manager():
    async with server.mcp.session_manager.run():
        _ready.set()
        await asyncio.Event().wait()


asyncio.run_coroutine_threadsafe(_keep_session_manager(), _loop)
if not _ready.wait(15):
    raise RuntimeError("MCP session manager не запустился")

_inner = ASGIMiddleware(_asgi, loop=_loop)
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
        "trailers", "transfer-encoding", "upgrade"}


def application(environ, start_response):
    """WSGI запрещает hop-by-hop заголовки — убираем их из ответа."""
    def _start(status, headers, exc_info=None):
        return start_response(status, [(k, v) for k, v in headers if k.lower() not in _HOP], exc_info)
    return _inner(environ, _start)
