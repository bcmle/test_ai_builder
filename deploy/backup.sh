#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/test-ai-builder/deploy/common.sh
if [[ ${1:-} == --first-existing && $# == 1 ]]; then
  [[ ${AI_BUILDER_IMAGE:-} =~ ^ghcr\.io/bcmle/test_ai_builder@sha256:[a-f0-9]{64}$ ]] || exit 2
elif [[ $# == 0 ]]; then
  current_image
else
  echo 'Usage: backup.sh [--first-existing]' >&2; exit 2
fi
# Caller must quiesce writers; do not unexpectedly restart them during deployment.
[[ -z $(docker ps -q --filter label=com.docker.compose.project=test-ai-builder --filter label=com.docker.compose.service=web) && -z $(docker ps -q --filter label=com.docker.compose.project=test-ai-builder --filter label=com.docker.compose.service=smoke) && -z $(docker ps -q --filter label=com.docker.compose.project=test-ai-builder --filter label=com.docker.compose.service=migrate) ]] || { echo 'Stop web and smoke before backup.' >&2; exit 1; }
[[ -z $(find "$STATE/data" -type l -print -quit) ]] || { echo 'Unexpected symlink in managed data.' >&2; exit 1; }
install -d -m 700 "$BACKUPS"
TARGET=$(mktemp -d "$BACKUPS/snapshot-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
compose exec -T postgres pg_dump -U postgres -d ai_builder --format=custom > "$TARGET/database.dump"
tar --numeric-owner -C "$STATE" -cf "$TARGET/data.tar" data
(cd "$STATE" && find data -type f -print0 | sort -z | xargs -0 -r sha256sum) > "$TARGET/artifacts.sha256"
printf '%s\n' "$AI_BUILDER_IMAGE" > "$TARGET/image.txt"
HAS_MIGRATIONS=$(compose exec -T postgres psql -X -qAt -U postgres -d ai_builder -c "SELECT to_regclass('app.schema_migrations') IS NOT NULL")
if [[ "$HAS_MIGRATIONS" == t ]]; then
  compose exec -T postgres psql -X -qAt -U postgres -d ai_builder -c "SELECT name||':'||checksum FROM app.schema_migrations ORDER BY name" > "$TARGET/migrations.txt"
else
  printf '%s\n' 'No app.schema_migrations table; existing data preserved before initialization.' > "$TARGET/migrations.txt"
fi
compose exec -T postgres psql -X -qAt -U postgres -d ai_builder < "$ROOT/deploy/fingerprint.sql" > "$TARGET/fingerprint.txt"
(cd "$TARGET" && sha256sum database.dump data.tar artifacts.sha256 image.txt migrations.txt fingerprint.txt > SHA256SUMS)
printf '%s\n' "$TARGET" > "$STATE/latest-backup"
echo "Backup complete: $TARGET"
