#!/usr/bin/env bash
# Execute once as root from the reviewed checkout. Does not install Docker or touch API keys.
set -Eeuo pipefail
umask 077
[[ ${EUID} -eq 0 ]] || { echo 'Bootstrap requires root.' >&2; exit 1; }
[[ $# == 1 && $1 =~ ^postgres:17\.[0-9]+-bookworm@sha256:[a-f0-9]{64}$ ]] || { echo 'Supply a reviewed immutable PostgreSQL 17 image.' >&2; exit 2; }
exec 9>/var/lock/test-ai-builder-deploy.lock
flock -n 9 || { echo 'A deployment/bootstrap is already active.' >&2; exit 1; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
docker compose version >/dev/null
command -v openssl >/dev/null
install -d -m 755 /opt/test-ai-builder /opt/test-ai-builder/deploy
install -d -m 700 /etc/test-ai-builder /var/lib/test-ai-builder /var/backups/test-ai-builder
install -d -o 10001 -g 10001 -m 700 /var/lib/test-ai-builder/data
install -o root -g root -m 644 "$SOURCE/compose.yaml" /opt/test-ai-builder/compose.yaml
for file in common.sh deploy.sh backup.sh verify-restore.sh bootstrap.sh bundle-hash.sh; do
  install -o root -g root -m 755 "$SOURCE/deploy/$file" "/opt/test-ai-builder/deploy/$file"
done
for file in roles.sql grants.sql fingerprint.sql; do
  install -o root -g root -m 600 "$SOURCE/deploy/$file" "/opt/test-ai-builder/deploy/$file"
done
# Record the exact installed bundle; later images must declare this same hash.
/opt/test-ai-builder/deploy/bundle-hash.sh > /opt/test-ai-builder/bundle.sha256
chown root:root /opt/test-ai-builder/bundle.sha256
chmod 644 /opt/test-ai-builder/bundle.sha256
# Never rotate existing credentials or overwrite the provider secret files.
if [[ ! -e /etc/test-ai-builder/deploy.env ]]; then
  (set -o noclobber
   printf 'POSTGRES_IMAGE=%s\nPOSTGRES_PASSWORD=%s\nMIGRATOR_PASSWORD=%s\nRUNTIME_PASSWORD=%s\n' \
     "$1" "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > /etc/test-ai-builder/deploy.env)
  chmod 600 /etc/test-ai-builder/deploy.env
fi
install -o root -g root -m 755 "$SOURCE/deploy/deploy.sh" /usr/local/sbin/test-ai-builder-deploy
if ! id ai-deploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash ai-deploy
fi
# ai-deploy is intentionally NOT in docker group. Only this fixed root command is allowed.
printf 'ai-deploy ALL=(root) NOPASSWD: /usr/local/sbin/test-ai-builder-deploy\n' > /etc/sudoers.d/test-ai-builder
chmod 440 /etc/sudoers.d/test-ai-builder
visudo -cf /etc/sudoers.d/test-ai-builder >/dev/null
install -d -o ai-deploy -g ai-deploy -m 700 /home/ai-deploy/.ssh
printf '%s\n' 'Bootstrap installed. Add the dedicated public deploy key to ai-deploy authorized_keys; no key was generated or printed.'
