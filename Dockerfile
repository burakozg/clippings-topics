FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# uv itself, pinned to a known release rather than whatever pip resolves.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

WORKDIR /app

# --- dependency layer ---------------------------------------------------
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# --- application layer ---------------------------------------------------
COPY clippings_topics/ clippings_topics/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

# Non-root, and the rootfs is read-only at runtime (see docker-compose.nas.yml):
# the only thing this writes is the extraction cache, which is bind-mounted.
RUN groupadd -g 1000 appuser && useradd -g appuser -u 1000 appuser \
    && mkdir -p /data && chown -R appuser:appuser /data
USER appuser

ENV CLIP_CACHE=/data/cache.json

CMD ["python", "-m", "clippings_topics", "--serve"]
