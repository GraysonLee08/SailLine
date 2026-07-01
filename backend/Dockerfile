# Multi-stage build (2026-06-30): strips test dependencies from the
# production image. The builder stage installs all requirements (including
# pytest); the runtime stage copies only the installed packages + app code,
# leaving pytest and its transitive deps out of the shipped image.
# Also runs as non-root (UID 1000) for security.

# ── Stage 1: Builder ───────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# eccodes is a C library required by cfgrib to parse NOAA GRIB2 files.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-dev \
    libeccodes-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY app/ ./app/
COPY workers/ ./workers/

# ── Stage 2: Runtime ───────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# eccodes runtime library (not the -dev headers) needed by cfgrib at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-tools \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from the builder.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app
COPY --from=builder /app/ ./

# Run as a non-root user for security. Cloud Run adds its own sandbox,
# but non-root inside the container limits escape blast radius.
RUN useradd -r -u 1000 -m -d /home/sailline sailline && \
    chown -R sailline:sailline /app
USER 1000

# Cloud Run injects the PORT env var; default to 8080 for local docker run.
ENV PORT=8080
EXPOSE 8080

# Single Uvicorn worker — Cloud Run scales horizontally.
# Multiple workers per container creates GIL contention with long-lived SSE connections.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1