# syntax=docker/dockerfile:1.6
#
# Multi-stage Dockerfile for the Go Migration Mover service.
# Builds a small, non-root container image suitable for local dev or cloud deployment.
#
# To build:
#   docker build -f deploy/docker/Dockerfile.go -t go-mover:dev .
# To run (assuming Kafka accessible locally):
#   docker run --rm -p 8090:8090 -e KAFKA_BROKERS=host.docker.internal:9092 go-mover:dev
#
# For multi-arch:
#   docker buildx build --platform linux/amd64,linux/arm64 -f deploy/docker/Dockerfile.go -t yourrepo/go-mover:latest .

########################
# 1. Builder Stage
########################
FROM golang:1.22-alpine AS builder

# Install build dependencies (if needed)
RUN apk add --no-cache git bash ca-certificates

WORKDIR /src

# Enable module mode
ENV GO111MODULE=on
ENV CGO_ENABLED=0 \
    GOOS=linux \
    GOARCH=amd64

# Copy module definition (go.mod + optional go.sum) for dependency layer caching
# Wildcard go.* safely matches go.mod and go.sum if present.
COPY services/go_api/go.* ./
# Download modules (will use go.sum if present)
RUN go mod download
# Ensure kafka-go module is present (idempotent)
RUN go list -m github.com/segmentio/kafka-go >/dev/null || go get github.com/segmentio/kafka-go@latest

# Copy the actual service code
COPY services/go_api/ ./

# Run go mod tidy after copying full source to generate go.sum
RUN go mod tidy

# Build the binary
RUN go build -trimpath -ldflags="-s -w" -o /out/go_mover ./...

########################
# 2. Runtime Stage
########################
# Using a minimal distroless-like base (alpine for convenience with healthcheck tooling)
FROM alpine:3.20 AS runtime

# Create non-root user
RUN addgroup -g 10001 app && adduser -u 10000 -G app -h /home/app -D app

# Install minimal runtime deps (ca-certs, curl for healthcheck)
RUN apk add --no-cache ca-certificates curl && update-ca-certificates

WORKDIR /app
# Prepare writable directory for job store and shared file copies
RUN mkdir -p /shared_storage && chown -R app:app /shared_storage

# Copy binary from builder
COPY --from=builder /out/go_mover /app/go_mover

# Expose service port
EXPOSE 8090

# Environment defaults (override at runtime)
ENV APP_NAME=data-in-motion-go-mover \
    APP_VERSION=0.1.0 \
    HOST=0.0.0.0 \
    PORT=8090 \
    LOG_LEVEL=INFO \
    KAFKA_BROKERS=localhost:9092 \
    KAFKA_MIGRATION_JOBS_TOPIC=migration_jobs \
    KAFKA_MIGRATION_STATUS_TOPIC=migration_status \
    KAFKA_GROUP=go_mover_group \
    DISABLE_KAFKA=true \
    SHUTDOWN_GRACE_SECONDS=10 \
    JOB_CONSUMER_POLL_INTERVAL_MS=500 \
    JOB_SIMULATION_CYCLES=4

USER app

# Healthcheck: queries /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8090/health || exit 1

# Labels (optional metadata)
LABEL org.opencontainers.image.title="Go Migration Mover" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.source="https://example.com/netapp-hackathon" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="Migration orchestrator handling dataset copy/verify/switch lifecycle."

# Run the service
ENTRYPOINT ["/app/go_mover"]
