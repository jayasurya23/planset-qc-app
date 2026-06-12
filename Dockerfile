# syntax=docker/dockerfile:1

# ---- Stage 1: build the React/Vite frontend ----
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # -> /build/dist

# ---- Stage 2: Python backend + bundled SPA ----
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app/backend

# PyMuPDF, Pillow, pdfplumber, openpyxl all ship manylinux wheels — no build
# toolchain needed, so the slim base stays slim.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Backend code (app/ package + rules YAML, scripts, etc.)
COPY backend/ ./

# Built SPA from stage 1; main.py mounts it when FRONTEND_DIST is set.
COPY --from=frontend /build/dist /app/frontend_dist

ENV FRONTEND_DIST=/app/frontend_dist \
    PLANSET_DATA_DIR=/home/data \
    AI_PROVIDER=openai

EXPOSE 8000
# Container Apps ingress targets port 8000 (see infra/main.bicep).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
