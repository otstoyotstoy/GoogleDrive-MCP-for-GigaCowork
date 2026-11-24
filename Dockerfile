FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY *.py ./
RUN useradd -r -u 10001 app && mkdir -p /app/logs /app/data && chown -R app /app
USER app
ENV MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8000 \
    GDRIVE_TOKEN_FILE=/app/data/token.json GDRIVE_AUDIT_LOG=/app/logs/audit.log
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "server.py"]
