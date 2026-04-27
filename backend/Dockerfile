# Multi-stage build is overkill for an early-stage app.
# Single-stage keeps the build simple and debuggable.

FROM python:3.12-slim

# eccodes is a C library required by cfgrib to parse NOAA GRIB2 files.
# This is the most important line in the Dockerfile — without eccodes installed,
# `import cfgrib` raises ImportError at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-dev \
    libeccodes-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first to maximize Docker layer caching.
# If requirements.txt doesn't change, this layer is reused on rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY app/ ./app/

# Cloud Run injects the PORT env var; default to 8080 for local docker run.
ENV PORT=8080
EXPOSE 8080

# Single Uvicorn worker — Cloud Run scales horizontally.
# Multiple workers per container creates GIL contention with long-lived SSE connections.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1