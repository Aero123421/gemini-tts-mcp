FROM python:3.12-slim

# Pin uv version for reproducible builds and fast cold starts (beats Claude's 10s auth timeout)
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    PORT=8080

COPY pyproject.toml README.md ./
COPY src ./src

RUN uv pip install --no-cache . \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

USER appuser

EXPOSE 8080

CMD ["python", "-m", "gemini_tts_mcp.server"]
