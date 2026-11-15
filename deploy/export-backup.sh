#!/usr/bin/env bash
# Encrypt a verified snapshot for transport. Never writes a plaintext export archive.
set -Eeuo pipefail
source /opt/test-ai-builder/deploy/common.sh
[[ $# == 2 && $1 =~ ^[a-f0-9]{40}$ && $2 =~ ^snapshot-[A-Za-z0-9-]+$ ]] || { echo 'Supply commit SHA and verified snapshot basename.' >&2; exit 2; }
COMMIT=$1
SNAPSHOT=$2
RECIPIENT_FILE=/etc/test-ai-builder/backup-recipient.age
EXPORT="$STATE/export"
[[ -f "$RECIPIENT_FILE" && ! -L "$RECIPIENT_FILE" && $(stat -c %u "$RECIPIENT_FILE") == 0 && $(stat -c %a "$RECIPIENT_FILE") == 600 ]] || { echo 'Invalid backup recipient file.' >&2; exit 1; }
IFS= read -r RECIPIENT < "$RECIPIENT_FILE"
[[ "$RECIPIENT" =~ ^age1[0-9a-z]{58}$ ]] || { echo 'Invalid age recipient.' >&2; exit 1; }
[[ -d "$BACKUPS/$SNAPSHOT" && ! -L "$BACKUPS/$SNAPSHOT" && -f "$BACKUPS/$SNAPSHOT/RESTORE_VERIFIED" ]] || { echo 'Snapshot has not passed restore verification.' >&2; exit 1; }
[[ -f "$STATE/releases/$COMMIT.json" && ! -L "$STATE/releases/$COMMIT.json" ]] || { echo 'Release manifest missing.' >&2; exit 1; }
[[ -d "$EXPORT" && ! -L "$EXPORT" && $(stat -c %u "$EXPORT") == 0 && $(stat -c %G "$EXPORT") == ai-deploy && $(stat -c %a "$EXPORT") == 750 ]] || { echo 'Invalid ciphertext export directory.' >&2; exit 1; }
(cd "$BACKUPS/$SNAPSHOT" && sha256sum --check SHA256SUMS)
TEMP=$(mktemp "$EXPORT/.encrypt-XXXXXX")
cleanup() { [[ ! -e "$TEMP" ]] || unlink -- "$TEMP"; }
trap cleanup EXIT
# A pipe prevents the plaintext tar stream from ever being saved in the readable export directory.
tar -C "$BACKUPS" -cf - "$SNAPSHOT" -C "$STATE" "releases/$COMMIT.json" | age -r "$RECIPIENT" > "$TEMP"
chown root:ai-deploy "$TEMP"
chmod 640 "$TEMP"
sync -f "$TEMP"
mv -f -- "$TEMP" "$EXPORT/$COMMIT.tar.age"
sync -f "$EXPORT"
echo "Encrypted backup ready: $EXPORT/$COMMIT.tar.age"
