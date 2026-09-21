# Multi-stage production Dockerfile for hrms-agentic-plugin
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir hatchling
COPY hrms_plugin ./hrms_plugin
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels .

# Final runtime image
FROM python:3.11-slim AS runner

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

EXPOSE 8000

CMD ["python", "-m", "hrms_plugin.server"]
