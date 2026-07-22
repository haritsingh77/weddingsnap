# Single stage, slim base. There used to be a builder stage that compiled dlib
# from source (cmake, boost, OpenBLAS, gtk) — several minutes of build time for
# face_recognition, which stopped being used when the pipeline moved to
# InsightFace, and InsightFace itself no longer ships here either: face
# recognition runs on the preprocessing machine and the result is synced to the
# database. Nothing left needs compiling.
FROM python:3.11-slim

# curl is for the healthcheck; nothing else needs system libraries now that
# dlib, OpenCV and ONNX are gone.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so edits to the source don't invalidate this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Never bake credentials into the image — mount them as a secret at runtime.
# .dockerignore already excludes .env and service-account.json; this is a second
# line of defence in case someone edits that file.
RUN rm -f .env service-account.json

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health" || exit 1

# $PORT is injected by Cloud Run, Railway and Render alike; 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
