# AllSearch MCP server, Streamable HTTP on port 8000.
#   docker build -t allsearch .
#   docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
#     -e ALLSEARCH_HTTP_TOKENS_FILE=/run/secrets/allsearch_tokens \
#     -v ./tokens:/run/secrets/allsearch_tokens:ro -v allsearch-state:/data allsearch
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_STATE_HOME=/data \
    ALLSEARCH_TRANSPORT=streamable-http \
    ALLSEARCH_MCP_HOST=0.0.0.0 \
    ALLSEARCH_MCP_PORT=8000 \
    ALLSEARCH_LOG_LEVEL=WARNING

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-compile . \
    && useradd --system --uid 10001 --home-dir /data allsearch \
    && mkdir -p /data && chown allsearch /data && chmod 700 /data

USER allsearch
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
# Refuses to start without ALLSEARCH_HTTP_TOKEN or ALLSEARCH_HTTP_TOKENS_FILE.
ENTRYPOINT ["allsearch"]
