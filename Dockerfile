FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock* README.md ./
COPY vendor ./vendor
COPY src ./src
RUN uv sync --extra talk --group dev --frozen 2>/dev/null || uv sync --extra talk --group dev
COPY tests ./tests
COPY docs ./docs
ENV PATH="/app/.venv/bin:$PATH"
# Default: run the test suite. Override CMD to run an app, e.g. `lw-feeds --once --dry-run`.
CMD ["pytest", "-q"]
