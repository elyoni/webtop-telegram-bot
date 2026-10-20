# Build from the parent directory so ../pywebtop is in the context:
#   docker compose build   (compose.yml sets context: ..)
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY pywebtop /pywebtop
WORKDIR /app
COPY webtop-bot/pyproject.toml webtop-bot/uv.lock webtop-bot/README.md ./
COPY webtop-bot/src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 bot && mkdir /data && chown bot /data
USER bot
VOLUME /data
CMD ["/app/.venv/bin/webtop-bot", "run"]
