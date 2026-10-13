FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="/root/.local/bin:$PATH"

# Copy Poetry configuration files
COPY pyproject.toml poetry.lock ./

# Configure Poetry to not use virtualenvs inside Docker
RUN poetry config virtualenvs.create false

# Install the locked dependencies (dev group included: grpcio-tools compiles the
# protobufs below). The lock file is the source of truth; it is never
# regenerated at build time so an image is reproducible from its commit.
RUN poetry install --no-interaction --no-ansi --no-root

# Copy application files
COPY . .

# Set Python path to include the current directory
ENV PYTHONPATH="/app:${PYTHONPATH}"

# Compile protocol buffers
RUN poetry run python -m grpc_tools.protoc --proto_path=./protobufs --python_out=./protobufs frames.proto

# Image tag baked in by CI (.ci/scripts/build-image.sh); surfaced by /health and
# /ready and in the OpenAPI document.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Runtime state (transcripts, floor files, ready signals, call summaries) goes
# outside the source tree so the container can run with a read-only root and a
# mounted volume. The chart mounts an emptyDir here.
ENV SPEAKING_BOT_STATE_DIR=/data
RUN mkdir -p /data

# Environment variables
ENV PORT=7014

EXPOSE ${PORT}

CMD poetry run uvicorn app:app --host 0.0.0.0 --port ${PORT:-7014} --ws-ping-interval 20 --ws-ping-timeout 60
