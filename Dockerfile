FROM ghcr.io/astral-sh/uv:0.8.3 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PERISCOPE_DATA_DIR=/data

COPY --from=uv /uv /uvx /bin/

RUN groupadd --gid 1000 periscope \
    && useradd --uid 1000 --gid 1000 --create-home periscope \
    && mkdir -p /app /data \
    && chown -R periscope:periscope /app /data

WORKDIR /app
COPY --chown=periscope:periscope pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=periscope:periscope periscope ./periscope
RUN uv sync --frozen --no-dev

USER periscope
VOLUME ["/data"]
EXPOSE 3999

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3999/health', timeout=3)"]

CMD ["uvicorn", "periscope.web.app:app", "--host", "0.0.0.0", "--port", "3999", "--no-server-header"]
