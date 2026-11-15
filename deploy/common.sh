#!/usr/bin/env bash
# Sourced only by fixed root-owned deployment scripts.
set -Eeuo pipefail
umask 077
# Never let caller-controlled Docker routing select a different daemon/project.
unset DOCKER_HOST DOCKER_CONTEXT COMPOSE_FILE COMPOSE_PROJECT_NAME
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
if [[ ${EUID} -ne 0 ]]; then echo 'Run through the restricted root deployment command.' >&2; exit 1; fi
ROOT=/opt/test-ai-builder
STATE=/var/lib/test-ai-builder
BACKUPS=/var/backups/test-ai-builder
ENV_FILE=/etc/test-ai-builder/deploy.env
[[ -f "$ENV_FILE" && $(stat -c %u "$ENV_FILE") == 0 && $(stat -c %a "$ENV_FILE") == 600 ]] || { echo 'Invalid deployment credentials file ownership/mode.' >&2; exit 1; }
[[ -f "$ROOT/compose.yaml" && $(stat -c %u "$ROOT/compose.yaml") == 0 && $(stat -c %a "$ROOT/compose.yaml") == 644 ]] || exit 1
compose() { docker compose --project-directory "$ROOT" --env-file "$ENV_FILE" -f "$ROOT/compose.yaml" "$@"; }
current_image() {
  [[ -f "$STATE/current-image" ]] || { echo 'No successful release image recorded.' >&2; return 1; }
  IFS= read -r AI_BUILDER_IMAGE < "$STATE/current-image"
  [[ "$AI_BUILDER_IMAGE" =~ ^ghcr\.io/bcmle/test_ai_builder@sha256:[a-f0-9]{64}$ ]] || return 1
  export AI_BUILDER_IMAGE
}
