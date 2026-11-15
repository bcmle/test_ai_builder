#!/usr/bin/env bash
# Exercise the real root export helper and Unix permissions on a disposable GitHub runner.
# No live database or provider credential is used. Never run against an existing install.
set -Eeuo pipefail
umask 077
[[ ${EUID} == 0 && ${GITHUB_ACTIONS:-} == true && ${GITHUB_RUN_ID:-} =~ ^[0-9]+$ && ${GITHUB_RUN_ATTEMPT:-} =~ ^[0-9]+$ ]] || { echo 'Requires root in a disposable GitHub Actions job.' >&2; exit 2; }
[[ ! -e /etc/test-ai-builder/deploy.env && ! -e /etc/test-ai-builder/backup-recipient.age && ! -e /opt/test-ai-builder/bundle.sha256 ]] || { echo 'Refusing to test over an existing installation.' >&2; exit 1; }
command -v age >/dev/null
command -v age-keygen >/dev/null
command -v runuser >/dev/null
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d /tmp/ai-builder-ci-export-XXXXXX)
chmod 700 "$WORK"
age-keygen -o "$WORK/fixture-private-key.txt" 2>/dev/null
chmod 600 "$WORK/fixture-private-key.txt"
RECIPIENT=$(age-keygen -y "$WORK/fixture-private-key.txt")
PG_IMAGE='postgres:17.12-bookworm@sha256:40865bc877a289da75963d97d72b59633f639fc5af7a4e856bfa3714cae5b536'
bash "$SOURCE/deploy/bootstrap.sh" "$PG_IMAGE" "$RECIPIENT" >/dev/null
EXPECTED_BUNDLE=$(bash "$SOURCE/deploy/bundle-hash.sh")
[[ $(cat /opt/test-ai-builder/bundle.sha256) == "$EXPECTED_BUNDLE" ]]
[[ $(/opt/test-ai-builder/deploy/bundle-hash.sh) == "$EXPECTED_BUNDLE" ]]
# Idempotent bootstrap with the same public recipient; a different key must be rejected.
bash "$SOURCE/deploy/bootstrap.sh" "$PG_IMAGE" "$RECIPIENT" >/dev/null
age-keygen -o "$WORK/other-fixture-private-key.txt" 2>/dev/null
OTHER_RECIPIENT=$(age-keygen -y "$WORK/other-fixture-private-key.txt")
if bash "$SOURCE/deploy/bootstrap.sh" "$PG_IMAGE" "$OTHER_RECIPIENT" >/dev/null 2>&1; then
  echo 'Bootstrap incorrectly replaced the backup recipient.' >&2; exit 1
fi
[[ $(cat /etc/test-ai-builder/backup-recipient.age) == "$RECIPIENT" ]]
SNAPSHOT="snapshot-ci-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-$(openssl rand -hex 8)"
SNAPSHOT_PATH="/var/backups/test-ai-builder/$SNAPSHOT"
COMMIT=$(printf '%s' "$SNAPSHOT" | sha256sum | cut -c 1-40)
install -d -m 700 "$SNAPSHOT_PATH" /var/lib/test-ai-builder/releases "$WORK/payload" "$WORK/decrypted"
printf '%s\n' 'Synthetic CI-only database bytes; not a PostgreSQL dump.' > "$SNAPSHOT_PATH/database.dump"
printf '%s\n' 'Synthetic CI-only evidence.' > "$WORK/payload/evidence.txt"
tar -C "$WORK/payload" -cf "$SNAPSHOT_PATH/data.tar" evidence.txt
(cd "$WORK/payload" && sha256sum evidence.txt) > "$SNAPSHOT_PATH/artifacts.sha256"
printf '%s\n' 'synthetic CI release fixture' > "$SNAPSHOT_PATH/image.txt"
printf '%s\n' 'synthetic CI migrations fixture' > "$SNAPSHOT_PATH/migrations.txt"
printf '%s\n' 'synthetic CI fingerprint fixture' > "$SNAPSHOT_PATH/fingerprint.txt"
(cd "$SNAPSHOT_PATH" && sha256sum database.dump data.tar artifacts.sha256 image.txt migrations.txt fingerprint.txt > SHA256SUMS)
printf '{"commit":"%s","mode":"ci-synthetic-export-only"}\n' "$COMMIT" > "/var/lib/test-ai-builder/releases/$COMMIT.json"
# No actual DB restore is claimed here; ci-smoke.sh separately tests PostgreSQL restore.
# This fixture marker lets the real export path be exercised without a live DB.
printf '%s\n' 'CI-only synthetic verification marker' > "$SNAPSHOT_PATH/RESTORE_VERIFIED"
/opt/test-ai-builder/deploy/export-backup.sh "$COMMIT" "$SNAPSHOT" >/dev/null
CIPHERTEXT="/var/lib/test-ai-builder/export/$COMMIT.tar.age"
[[ $(stat -c '%U:%G:%a' /var/lib/test-ai-builder) == root:ai-deploy:710 ]]
[[ $(stat -c '%U:%G:%a' /var/lib/test-ai-builder/export) == root:ai-deploy:750 ]]
[[ $(stat -c '%U:%G:%a' "$CIPHERTEXT") == root:ai-deploy:640 ]]
[[ $(stat -c '%U:%G:%a' /etc/test-ai-builder/backup-recipient.age) == root:root:600 ]]
# Ciphertext is readable via the same unprivileged account used by the release workflow.
runuser -u ai-deploy -- cat "$CIPHERTEXT" > "$WORK/transferred.tar.age"
cmp "$CIPHERTEXT" "$WORK/transferred.tar.age"
for private in "$SNAPSHOT_PATH/database.dump" /etc/test-ai-builder/deploy.env \
               "$WORK/fixture-private-key.txt" "/var/lib/test-ai-builder/releases/$COMMIT.json"; do
  if runuser -u ai-deploy -- cat "$private" >/dev/null 2>&1; then
    echo 'Deploy user could read a forbidden plaintext/private file.' >&2; exit 1
  fi
done
age -d -i "$WORK/fixture-private-key.txt" "$WORK/transferred.tar.age" | tar -C "$WORK/decrypted" -xf -
(cd "$WORK/decrypted/$SNAPSHOT" && sha256sum --check SHA256SUMS)
cmp "$SNAPSHOT_PATH/database.dump" "$WORK/decrypted/$SNAPSHOT/database.dump"
cmp "/var/lib/test-ai-builder/releases/$COMMIT.json" "$WORK/decrypted/releases/$COMMIT.json"
mkdir "$WORK/inner"
tar -C "$WORK/inner" -xf "$WORK/decrypted/$SNAPSHOT/data.tar"
(cd "$WORK/inner" && sha256sum --check "$WORK/decrypted/$SNAPSHOT/artifacts.sha256")
# A second export atomically replaces only ciphertext and remains decryptable.
/opt/test-ai-builder/deploy/export-backup.sh "$COMMIT" "$SNAPSHOT" >/dev/null
age -d -i "$WORK/fixture-private-key.txt" "$CIPHERTEXT" | tar -t >/dev/null
[[ -z $(find /var/lib/test-ai-builder/export -maxdepth 1 -type f -name '.encrypt-*' -print -quit) ]]
echo 'Actual backup export, recipient stability, deploy-user permissions and decrypt/hash checks passed.'
# Disposable runner teardown removes these fixtures. Never upload WORK or the private keys.
