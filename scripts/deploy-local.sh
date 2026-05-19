#!/usr/bin/env bash
set -euo pipefail

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="deployment-logs"
LOG_FILE="${LOG_DIR}/deploy-${TIMESTAMP}.log"

mkdir -p "${LOG_DIR}"

exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[$(date -Iseconds)] Starting deployment"

: "${IMAGE_REF:?IMAGE_REF must be set}"
: "${CONTAINER_NAME:=taskmanagement-app}"
: "${HOST_PORT:=8082}"
: "${CONTAINER_PORT:=8082}"

PREVIOUS_IMAGE=""
if docker ps -a --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
  PREVIOUS_IMAGE="$(docker inspect -f '{{.Config.Image}}' "${CONTAINER_NAME}")"
  echo "Found existing container ${CONTAINER_NAME} using image ${PREVIOUS_IMAGE}"
  docker rm -f "${CONTAINER_NAME}"
else
  echo "No existing container named ${CONTAINER_NAME}."
fi

echo "Pulling new image ${IMAGE_REF}"
docker pull "${IMAGE_REF}"

echo "Starting new container ${CONTAINER_NAME}"
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
  "${IMAGE_REF}"

sleep 5

if docker ps --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
  echo "[$(date -Iseconds)] Deployment succeeded"
  echo "Container: ${CONTAINER_NAME}"
  echo "Image: ${IMAGE_REF}"
  exit 0
fi

echo "[$(date -Iseconds)] Deployment failed"
if [ -n "${PREVIOUS_IMAGE}" ]; then
  echo "Attempting automatic rollback to ${PREVIOUS_IMAGE}"
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
    "${PREVIOUS_IMAGE}"
  echo "Rollback attempted. Check container health logs."
fi

exit 1
