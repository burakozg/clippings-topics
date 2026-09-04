FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY clippings_topics/ clippings_topics/

# Non-root, and the rootfs is read-only at runtime (see docker-compose.nas.yml):
# the only thing this writes is the extraction cache, which is bind-mounted.
RUN groupadd -g 1000 appuser && useradd -g appuser -u 1000 appuser \
    && mkdir -p /data && chown -R appuser:appuser /data
USER appuser

ENV CLIP_CACHE=/data/cache.json

CMD ["python", "-m", "clippings_topics", "--serve"]
