# Multi-stage: build the Vue frontend, then ship API + built SPA in one image.
# The FastAPI app serves web/frontend/dist automatically when it exists.

FROM node:22-alpine AS frontend
WORKDIR /src
COPY web/frontend/package.json web/frontend/package-lock.json* ./
RUN npm ci --ignore-scripts || npm install --ignore-scripts
COPY web/frontend ./
# Bust layer cache when only source files change under some builders.
ARG FRONTEND_CACHE_BUST=1
RUN echo "frontend_cache_bust=${FRONTEND_CACHE_BUST}" && npm run build

FROM python:3.12-slim

WORKDIR /app

# curl: healthcheck; git: in-panel self-update (host repo); docker CLI: compose via socket
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && DOCKER_ARCH="$TARGETARCH" \
    && if [ -z "$DOCKER_ARCH" ] || [ "$DOCKER_ARCH" = "amd64" ]; then DOCKER_ARCH=x86_64; fi \
    && if [ "$DOCKER_ARCH" = "arm64" ]; then DOCKER_ARCH=aarch64; fi \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-27.5.1.tgz" \
      | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker \
    && chmod +x /usr/local/bin/docker

COPY requirements.txt /app/requirements.txt
COPY web/backend/requirements.txt /app/web/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY web/backend /app/web/backend
COPY --from=frontend /src/dist /app/web/frontend/dist

# Build-time commit (compose / CI can override with --build-arg)
ARG OCIBOT_GIT_SHA=unknown
ENV OCIBOT_GIT_SHA=${OCIBOT_GIT_SHA}

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV OCIBOT_API_WORKERS=2

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "web.backend.run"]
