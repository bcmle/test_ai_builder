#!/usr/bin/env bash
# Stable content hash, independent of checkout path, filesystem metadata or platform.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 -I - "$ROOT" <<'PY'
import hashlib
from pathlib import Path
import sys

root = Path(sys.argv[1])
paths = (
    'compose.yaml',
    'deploy/backup.sh',
    'deploy/bootstrap.sh',
    'deploy/bundle-hash.sh',
    'deploy/common.sh',
    'deploy/deploy.sh',
    'deploy/export-backup.sh',
    'deploy/fingerprint.sql',
    'deploy/grants.sql',
    'deploy/roles.sql',
    'deploy/verify-restore.sh',
)
hash_value = hashlib.sha256()
for relative in paths:
    body = (root / relative).read_bytes()
    hash_value.update(relative.encode('utf-8') + b'\0')
    hash_value.update(len(body).to_bytes(8, 'big'))
    hash_value.update(body)
print(hash_value.hexdigest())
PY
