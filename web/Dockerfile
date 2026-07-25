# Multi-stage: build the Vue frontend, then ship API + built SPA in one image.
# The FastAPI app serves web/frontend/dist automatically when it exists.

FROM node:22-alpine AS frontend
WORKDIR /src
COPY web/frontend/package.json web/frontend/package-lock.json* ./
RUN npm ci --ignore-scripts || npm install --ignore-scripts
COPY web/frontend ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY web/backend/requirements.txt /app/web/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY web/backend /app/web/backend
COPY --from=frontend /src/dist /app/web/frontend/dist

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV OCIBOT_API_WORKERS=2

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "web.backend.run"]
