FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apk add --no-cache libpq ffmpeg
RUN pip install --no-cache-dir uv==0.13.6
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY server.py ./
COPY vooy_storage ./vooy_storage
COPY scripts ./scripts

ENV PATH="/app/.venv/bin:$PATH"
USER 1000:1000
EXPOSE 8000
CMD ["python", "server.py"]
