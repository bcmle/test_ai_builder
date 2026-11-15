#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/test-ai-builder/deploy/common.sh
[[ $# == 2 && $1 =~ ^[a-f0-9]{64}$ && $2 =~ ^[a-f0-9]{40}$ ]] || { echo 'Usage: test-ai-builder-deploy IMAGE_DIGEST_HEX COMMIT_SHA' >&2; exit 2; }
export AI_BUILDER_IMAGE="ghcr.io/bcmle/test_ai_builder@sha256:$1"
COMMIT=$2
exec 9>/var/lock/test-ai-builder-deploy.lock
flock -n 9 || { echo 'A deployment is already active.' >&2; exit 1; }
[[ $(df --output=avail -k "$STATE" | tail -1) -ge 2097152 ]] || { echo 'At least 2 GiB free disk required.' >&2; exit 1; }
docker info >/dev/null
compose config --quiet
compose pull postgres web migrate smoke
# Verify OCI provenance label agrees with the requested, already-tested commit.
LABEL=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$AI_BUILDER_IMAGE")
[[ "$LABEL" == "$COMMIT" ]] || { echo 'Image revision label does not match requested commit.' >&2; exit 1; }
cmp -- "${BASH_SOURCE[0]}" "$ROOT/deploy/deploy.sh" >/dev/null || { echo 'Installed deploy entry point differs from the reviewed bundle.' >&2; exit 1; }
# An image tested with newer Compose/scripts must never run with an older host bundle.
BUNDLE_LABEL=$(docker image inspect --format '{{ index .Config.Labels "io.ai-builder.deploy-bundle-sha256" }}' "$AI_BUILDER_IMAGE")
[[ "$BUNDLE_LABEL" =~ ^[a-f0-9]{64}$ ]] || { echo 'Image deployment bundle label is missing or invalid.' >&2; exit 1; }
[[ -f "$ROOT/bundle.sha256" && $(stat -c %u "$ROOT/bundle.sha256") == 0 && $(stat -c %a "$ROOT/bundle.sha256") == 644 ]] || { echo 'Installed deployment bundle record is invalid.' >&2; exit 1; }
IFS= read -r INSTALLED_BUNDLE < "$ROOT/bundle.sha256"
ACTUAL_BUNDLE=$("$ROOT/deploy/bundle-hash.sh")
[[ "$BUNDLE_LABEL" == "$INSTALLED_BUNDLE" && "$INSTALLED_BUNDLE" == "$ACTUAL_BUNDLE" ]] || { echo 'Deployment bundle mismatch: install the reviewed host bundle before releasing this image.' >&2; exit 1; }
install -d -m 700 "$BACKUPS" "$STATE/releases"
# Quiesce all writers before backup/migration. No live worker exists in this profile.
compose stop web smoke
# docker compose run jobs are one-off containers and may not be stopped by compose stop.
# Stop only this project's smoke jobs, never arbitrary containers or another deployment.
while IFS= read -r container; do
  [[ -z "$container" ]] || docker stop --time 30 "$container" >/dev/null
done < <(docker ps -q --filter label=com.docker.compose.project=test-ai-builder --filter label=com.docker.compose.service=smoke)
[[ -z $(docker ps -q --filter label=com.docker.compose.project=test-ai-builder --filter label=com.docker.compose.service=migrate) ]] || { echo 'Another migration job is active; aborting deployment.' >&2; exit 1; }
compose up -d --wait postgres
if [[ -f "$STATE/current-image" ]]; then
  "$ROOT/deploy/backup.sh"
else
  EMPTY=$(compose exec -T postgres psql -X -qAt -U postgres -d ai_builder -c "SELECT count(*) FROM pg_tables WHERE schemaname IN ('app','checkpoints')")
  if [[ "$EMPTY" != 0 ]]; then
    "$ROOT/deploy/backup.sh" --first-existing
  else
    printf '%s\n' "first-empty-initialization $(date -u +%FT%TZ)" >> "$STATE/deploy-history.log"
  fi
fi
# Nothing starts serving again after a failed migration. Operator review is required.
compose exec -T postgres psql -X -q -U postgres -d ai_builder < "$ROOT/deploy/roles.sql"
compose run --rm --no-deps migrate
compose exec -T postgres psql -X -q -U postgres -d ai_builder < "$ROOT/deploy/grants.sql"
compose run --rm --no-deps smoke
compose up -d --wait web
curl --fail --silent --show-error http://127.0.0.1:8080/api/v1/health/ready >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/preview >/dev/null
cp "$STATE/data/technical-preview.json" "$STATE/releases/$COMMIT-first-preview.json"
# Prove the DB, checkpoints and files survive a real PostgreSQL container restart.
# Stop web first; preserve the existing container and named volume.
compose stop web
compose restart postgres
compose up -d --wait postgres
compose up -d --wait web
curl --fail --silent --show-error http://127.0.0.1:8080/api/v1/health/ready >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/preview >/dev/null
# The repeated smoke must validate persisted state without new logical work.
compose run --rm --no-deps smoke
python3 -I - "$STATE/releases/$COMMIT-first-preview.json" "$STATE/data/technical-preview.json" <<'PY'
import json, sys
first, second = [json.load(open(p)) for p in sys.argv[1:]]
for key in ('run_id', 'synthetic_calculated_micro', 'physical_attempts', 'evidence_manifest'):
    assert first[key] == second[key], key
assert second['external_api_calls'] == 0
assert second['fixture_http_calls_this_invocation'] == 0
PY
compose stop web smoke
"$ROOT/deploy/backup.sh" --first-existing
IFS= read -r LATEST_BACKUP < "$STATE/latest-backup"
"$ROOT/deploy/verify-restore.sh" "${LATEST_BACKUP##*/}"
compose up -d --wait web
curl --fail --silent --show-error http://127.0.0.1:8080/preview >/dev/null
printf '%s\n' "$AI_BUILDER_IMAGE" > "$STATE/current-image.next"
mv "$STATE/current-image.next" "$STATE/current-image"
MIGRATIONS=$(compose exec -T postgres psql -X -qAt -U postgres -d ai_builder -c "SELECT coalesce(json_agg(row_to_json(m))::text,'[]') FROM (SELECT name,checksum FROM app.schema_migrations ORDER BY name) m")
printf '{"commit":"%s","image":"%s","deployed_at":"%s","migrations":%s,"mode":"technical-no-live-api"}\n' "$COMMIT" "$AI_BUILDER_IMAGE" "$(date -u +%FT%TZ)" "$MIGRATIONS" > "$STATE/releases/$COMMIT.json"
printf '%s %s %s\n' "$(date -u +%FT%TZ)" "$COMMIT" "$AI_BUILDER_IMAGE" >> "$STATE/deploy-history.log"
echo "Technical deployment ready: $COMMIT"
