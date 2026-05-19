# CD Deployment Guide

## What the CD stage does

- Publishes Docker images to GitHub Container Registry (GHCR).
- Keeps version tags and branch tags for traceability.
- On `main` pushes, preserves current `latest` as `previous` for rollback.
- Replaces the currently running container with the new image.
- Uploads deployment logs as workflow artifacts.

## Image tags produced

- `ghcr.io/<owner>/taskmanagement:1.0.0-<shortsha>-build-<run-number>`
- `ghcr.io/<owner>/taskmanagement:<branch>`
- `ghcr.io/<owner>/taskmanagement:latest` (main branch only)
- `ghcr.io/<owner>/taskmanagement:previous` (main branch only, rollback tag)

## Manual rollback command

Use this if deployment on your host needs manual rollback:

```bash
IMAGE_BASE=ghcr.io/<owner>/taskmanagement \
CONTAINER_NAME=taskmanagement-app \
HOST_PORT=8082 \
CONTAINER_PORT=8082 \
SERVER_PORT=8082 \
SPRING_PROFILES_ACTIVE=prod \
DB_URL='jdbc:postgresql://<db-host>:5432/taskmanagement' \
DB_USERNAME='<db-user>' \
DB_PASSWORD='<db-password>' \
JWT_SECRET='<jwt-secret>' \
./scripts/rollback-local.sh
```

## Notes for Podman usage

- Podman can pull the same GHCR package image tags.
- Example:

```bash
podman login ghcr.io
podman pull ghcr.io/<owner>/taskmanagement:latest
podman run -d --name taskmanagement-app -p 8082:8082 ghcr.io/<owner>/taskmanagement:latest
```

## Failure simulation checklist

1. Introduce an intentionally failing test and push to feature branch.
2. Confirm `build-and-test` job fails and blocks downstream jobs.
3. Fix the test and push again.
4. Confirm full pipeline succeeds, including package publish and deploy on `main`.
