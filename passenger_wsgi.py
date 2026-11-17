"""Точка входа для Passenger (виртуальный хостинг). Настройки — в файле .env рядом."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Используем python из виртуального окружения проекта, если Passenger запустил системный
VENV_PY = os.path.join(HERE, "venv", "bin", "python")
if os.path.exists(VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(VENV_PY):
    os.execl(VENV_PY, VENV_PY, *sys.argv)

os.environ.setdefault("MCP_TRANSPORT", "streamable-http")
os.environ.setdefault("MCP_JSON_RESPONSE", "true")

from wsgi import application  # noqa: E402,F401
