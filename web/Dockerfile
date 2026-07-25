# Multi-stage: build the Vue frontend, then ship API + built SPA in one image.
# The FastAPI app serves web/frontend/dist automatically when it exists.

FROM node:22-alpine AS frontend
WORKDIR /src
COPY web/frontend/package.json web/frontend/package-lock.json* ./
RUN npm install
COPY web/frontend ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY web/backend/requirements.txt /app/web/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt -r /app/web/backend/requirements.txt

COPY app /app/app
COPY web/backend /app/web/backend
COPY --from=frontend /src/dist /app/web/frontend/dist

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "web.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
