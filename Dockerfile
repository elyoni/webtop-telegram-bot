FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /usr/local/bin/uv
# git: pywebtop is a git dependency
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 bot && mkdir /data && chown bot /data
USER bot
VOLUME /data
CMD ["/app/.venv/bin/webtop-bot", "run"]
