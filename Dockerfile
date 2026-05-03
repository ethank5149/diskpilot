# ── Stage 1: Build React frontend ────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python backend ───────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.115.0 "uvicorn[standard]==0.30.6" aiosqlite==0.20.0 python-multipart==0.0.9

COPY backend/main.py .
COPY --from=frontend /app/dist ./static

VOLUME ["/data"]
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
