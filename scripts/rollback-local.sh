#!/usr/bin/env bash
set -euo pipefail

: "${IMAGE_BASE:?IMAGE_BASE must be set, e.g. ghcr.io/OWNER/taskmanagement}"
: "${CONTAINER_NAME:=taskmanagement-app}"
: "${HOST_PORT:=8082}"
: "${CONTAINER_PORT:=8082}"

ROLLBACK_IMAGE="${IMAGE_BASE}:previous"

echo "Pulling rollback image ${ROLLBACK_IMAGE}"
docker pull "${ROLLBACK_IMAGE}"

if docker ps -a --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
  docker rm -f "${CONTAINER_NAME}"
fi

docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  -e SERVER_PORT="${SERVER_PORT:-8082}" \
  -e SPRING_PROFILES_ACTIVE="${SPRING_PROFILES_ACTIVE:-prod}" \
  -e DB_URL="${DB_URL:-jdbc:h2:mem:taskmanagement;DB_CLOSE_DELAY=-1;MODE=PostgreSQL}" \
  -e DB_USERNAME="${DB_USERNAME:-sa}" \
  -e DB_PASSWORD="${DB_PASSWORD:-}" \
  -e JWT_SECRET="${JWT_SECRET:-ci-cd-secret}" \
  -e JWT_EXPIRATION="${JWT_EXPIRATION:-86400000}" \
  "${ROLLBACK_IMAGE}"

echo "Rollback completed with ${ROLLBACK_IMAGE}"
