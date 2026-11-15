#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/test-ai-builder/deploy/common.sh
[[ $# == 1 && $1 =~ ^snapshot-[A-Za-z0-9-]+$ ]] || { echo 'Supply a backup directory basename.' >&2; exit 2; }
if [[ -z ${AI_BUILDER_IMAGE:-} ]]; then current_image; fi
[[ "$AI_BUILDER_IMAGE" =~ ^ghcr\.io/bcmle/test_ai_builder@sha256:[a-f0-9]{64}$ ]] || exit 2
SNAPSHOT="$BACKUPS/$1"
[[ -d "$SNAPSHOT" && ! -L "$SNAPSHOT" ]] || exit 2
(cd "$SNAPSHOT" && sha256sum --check SHA256SUMS)
CHECK_DB="ai_restore_$(openssl rand -hex 12)"
CREATED=0
CHECK_DIR=$(mktemp -d "$STATE/restore-check-XXXXXX")
# Only this newly created scratch database may be dropped. Production is never a target.
cleanup() {
  if [[ "$CREATED" == 1 ]]; then
    compose exec -T postgres dropdb -U postgres --if-exists "$CHECK_DB" >/dev/null || true
  fi
  # Keep restored files as verification evidence; no recursive deletion of arbitrary paths.
}
trap cleanup EXIT
compose exec -T postgres createdb -U postgres "$CHECK_DB"
CREATED=1
compose exec -T postgres pg_restore -U postgres -d "$CHECK_DB" --exit-on-error < "$SNAPSHOT/database.dump"
tar -C "$CHECK_DIR" -xf "$SNAPSHOT/data.tar"
(cd "$CHECK_DIR" && sha256sum --check "$SNAPSHOT/artifacts.sha256")
compose exec -T postgres psql -X -qAt -U postgres -d "$CHECK_DB" < "$ROOT/deploy/fingerprint.sql" > "$CHECK_DIR/fingerprint.txt"
cmp "$SNAPSHOT/fingerprint.txt" "$CHECK_DIR/fingerprint.txt"
# Prove the application can read the restored graph/receipts and artifact manifest.
# The override changes ONLY the scratch DB and /app/data bind; no production writes.
printf 'services:\n  smoke:\n    environment:\n      DATABASE_URL: "postgresql://ai_runtime:${RUNTIME_PASSWORD}@postgres:5432/%s"\n    volumes:\n      - "%s/data:/app/data"\n' "$CHECK_DB" "$CHECK_DIR" > "$CHECK_DIR/compose.restore.yaml"
docker compose --project-directory "$ROOT" --env-file "$ENV_FILE" -f "$ROOT/compose.yaml" -f "$CHECK_DIR/compose.restore.yaml" run --rm --no-deps smoke > "$CHECK_DIR/smoke-restore.json"
compose exec -T postgres psql -X -qAt -U postgres -d "$CHECK_DB" < "$ROOT/deploy/fingerprint.sql" > "$CHECK_DIR/fingerprint-after.txt"
cmp "$SNAPSHOT/fingerprint.txt" "$CHECK_DIR/fingerprint-after.txt"
printf 'Verified restore %s at %s\n' "$1" "$(date -u +%FT%TZ)" > "$CHECK_DIR/result.txt"
echo "Restore verified; report: $CHECK_DIR/result.txt"
