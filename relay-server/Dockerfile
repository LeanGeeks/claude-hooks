FROM python:3.12-slim

# Install relay user before anything else so layers can be cached.
RUN groupadd --system relay && useradd --system --gid relay --no-create-home relay

WORKDIR /app

# Install dependencies in a layer that won't bust on source changes.
# requirements-runtime.txt contains only runtime deps (no pytest/dev tools).
# requirements.txt (with pytest) is kept for local dev/CI use only.
COPY pyproject.toml ./
COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

# Copy source and install the package without re-downloading deps.
COPY relay_server/ ./relay_server/
RUN pip install --no-cache-dir --no-deps .

# Ensure the app working directory and the volume mount point are owned by the
# relay user before we drop privileges, so SQLite and any cwd writes succeed.
RUN chown relay:relay /app \
    && mkdir -p /var/lib/relay && chown relay:relay /var/lib/relay

# Hand off to the relay user for runtime.
USER relay

EXPOSE 8080

# Healthcheck is defined in docker-compose.yml where it is more operationally
# tunable (interval, retries, etc. can be adjusted per environment without
# rebuilding the image).

CMD ["uvicorn", "relay_server.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080"]
