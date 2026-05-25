# ── Stage 1: Build dlib + face_recognition (compiles from source) ──────────────
FROM python:3.11-slim AS builder

# Install system build dependencies for dlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    libopenblas-dev \
    liblapack-dev \
    libx11-dev \
    libgtk-3-dev \
    libboost-python-dev \
    libboost-thread-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install Python dependencies into a target dir for copying
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Slim runtime image ────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Only runtime libs needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopenblas-dev \
    liblapack-dev \
    libgomp1 \
    libgl1 \
    libglib2.0-0 \
    libx11-6 \
    libgtk-3-0 \
    libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled Python packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application code
COPY . .

# Expose the FastAPI port
EXPOSE 8000

# Railway injects PORT env var; fall back to 8000 locally
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
