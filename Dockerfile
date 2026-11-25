FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    UV_COMPILE_BYTECODE=1 \
    PORT=8080

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --no-cache \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

USER appuser
RUN test -r /app && test -x /app && python -c "import gemini_tts_mcp"

EXPOSE 8080

CMD ["python", "-m", "gemini_tts_mcp.server"]
