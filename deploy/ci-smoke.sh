#!/usr/bin/env bash
# GitHub-hosted CI only. Exercises the technical profile with a locally built image.
set -Eeuo pipefail
umask 077
[[ ${EUID} == 0 && ${GITHUB_RUN_ID:-} =~ ^[0-9]+$ && ${GITHUB_RUN_ATTEMPT:-} =~ ^[0-9]+$ ]] || { echo 'Run as root in a GitHub CI job with numeric run IDs.' >&2; exit 2; }
[[ $# == 1 && $1 =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]+$ ]] || { echo 'Supply the CI-built local image.' >&2; exit 2; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d /tmp/ai-builder-ci-XXXXXX)
PROJECT="ai-builder-ci-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
install -d -o 10001 -g 10001 -m 700 "$WORK/data" "$WORK/restored"
printf 'AI_BUILDER_IMAGE=%s\nPOSTGRES_IMAGE=%s\nPOSTGRES_PASSWORD=%s\nMIGRATOR_PASSWORD=%s\nRUNTIME_PASSWORD=%s\n' \
 "$1" 'postgres:17.12-bookworm@sha256:40865bc877a289da75963d97d72b59633f639fc5af7a4e856bfa3714cae5b536' \
 "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > "$WORK/ci.env"
printf 'services:\n  web:\n    volumes:\n      - "%s/data:/app/data"\n  migrate:\n    volumes:\n      - "%s/data:/app/data"\n  smoke:\n    volumes:\n      - "%s/data:/app/data"\nvolumes:\n  postgres-data:\n    name: "%s-pgdata"\n' "$WORK" "$WORK" "$WORK" "$PROJECT" > "$WORK/override.yaml"
compose() { docker compose -p "$PROJECT" --project-directory "$SOURCE" --env-file "$WORK/ci.env" -f "$SOURCE/compose.yaml" -f "$WORK/override.yaml" "$@"; }
cleanup() {
  local result=$?
  if (( result != 0 )); then compose logs --no-color --tail 60 web caddy || true; fi
  # Only the unique CI project/volume is removed, never the production project.
  compose down --volumes --remove-orphans >/dev/null || true
  # Keep the temp reports for CI artifact collection and diagnosis; no recursive rm.
  echo "CI smoke artifacts: $WORK"
}
trap cleanup EXIT
bash -n "$SOURCE"/deploy/*.sh
compose config --quiet
compose up -d --wait postgres
compose exec -T postgres psql -X -q -U postgres -d ai_builder < "$SOURCE/deploy/roles.sql"
compose run --rm --no-deps migrate
compose exec -T postgres psql -X -q -U postgres -d ai_builder < "$SOURCE/deploy/grants.sql"
# Explicitly demonstrate that the runtime role cannot create tables.
if compose exec -T postgres sh -c 'PGPASSWORD="$RUNTIME_PASSWORD" psql -X -v ON_ERROR_STOP=1 -h 127.0.0.1 -U ai_runtime -d ai_builder -c "CREATE TABLE app.runtime_must_not_create(id int)"' >/dev/null 2>&1; then
  echo 'Runtime role unexpectedly has DDL privileges.' >&2; exit 1
fi
compose run --rm --no-deps smoke
compose up -d --wait web
curl --fail --silent --show-error http://127.0.0.1:8080/api/v1/health/ready >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/preview > "$WORK/preview.html"
grep -q 'Not a live market assessment' "$WORK/preview.html"
cp "$WORK/data/technical-preview.json" "$WORK/first-preview.json"
# Restart the actual stateful service, not only the stateless web container.
# No recreation, volume removal or migration is involved in this recovery check.
compose stop web
compose restart postgres
compose up -d --wait postgres
compose up -d --wait web
curl --fail --silent --show-error http://127.0.0.1:8080/api/v1/health/ready >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/preview >/dev/null
compose run --rm --no-deps smoke
# Inspect persisted smoke identity and costs without relying on command log formatting.
python3 - "$WORK/first-preview.json" "$WORK/data/technical-preview.json" <<'PY'
import json, sys
first, second = [json.load(open(p)) for p in sys.argv[1:]]
for key in ('run_id', 'synthetic_calculated_micro', 'physical_attempts', 'evidence_manifest'):
    assert first[key] == second[key], key
assert second['external_api_calls'] == 0
assert second['fixture_http_calls_this_invocation'] == 0
PY
compose stop web smoke
compose exec -T postgres pg_dump -U postgres -d ai_builder --format=custom > "$WORK/database.dump"
compose exec -T postgres psql -X -qAt -U postgres -d ai_builder < "$SOURCE/deploy/fingerprint.sql" > "$WORK/fingerprint.txt"
tar -C "$WORK/data" -cf "$WORK/data.tar" .
(cd "$WORK/data" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) > "$WORK/artifacts.sha256"
compose exec -T postgres createdb -U postgres ai_builder_ci_restore
compose exec -T postgres pg_restore -U postgres -d ai_builder_ci_restore --exit-on-error < "$WORK/database.dump"
tar -C "$WORK/restored" -xf "$WORK/data.tar"
(cd "$WORK/restored" && sha256sum --check "$WORK/artifacts.sha256")
printf 'services:\n  smoke:\n    environment:\n      DATABASE_URL: "postgresql://ai_runtime:${RUNTIME_PASSWORD}@postgres:5432/ai_builder_ci_restore"\n    volumes:\n      - "%s/restored:/app/data"\n' "$WORK" > "$WORK/restore.yaml"
docker compose -p "$PROJECT" --project-directory "$SOURCE" --env-file "$WORK/ci.env" -f "$SOURCE/compose.yaml" -f "$WORK/override.yaml" -f "$WORK/restore.yaml" run --rm --no-deps smoke
compose exec -T postgres psql -X -qAt -U postgres -d ai_builder_ci_restore < "$SOURCE/deploy/fingerprint.sql" > "$WORK/restored-fingerprint.txt"
cmp "$WORK/fingerprint.txt" "$WORK/restored-fingerprint.txt"
# Only publish non-secret artifacts from WORK; ci.env and raw dumps are private.
echo 'Technical Compose smoke, restart, runtime privileges and restore checks passed.'
compose up -d --wait web caddy
curl --fail --silent --show-error http://127.0.0.1/ > "$WORK/public-home.html"
for path in preview health/ready api/v1/health/ready; do
  [[ $(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1/$path") == 404 ]]
done
[[ $(curl --silent --request POST --output /dev/null --write-out '%{http_code}' http://127.0.0.1/) == 404 ]]
echo 'Public read-only HTTP routes passed.'
