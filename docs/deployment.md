# Technical deployment on Aeza

This profile runs PostgreSQL, the web process and an explicit synthetic smoke job.
It is not the production research worker. No provider API keys are mounted or passed.
`/preview` is enabled only for this profile and serves one validated synthetic report;
private artifacts are not exposed as a directory. Web binds only to server loopback.

## Bootstrap once

Prerequisites: reviewed commit, Ubuntu server with Docker Engine + Compose v2, curl,
openssl, sudo, flock, an active rental, verified SSH host fingerprint, at least 2 GiB
free disk. Confirm `uname -m`; CI must build the matching Linux architecture. Docker
installation and changes to SSH/firewall are separate bootstrap actions. No database
or existing provider secret file is deleted by these scripts.

Copy the reviewed `compose.yaml` and `deploy/` directory to a root-only checkout on
Aeza. Run as root, passing the PostgreSQL 17.12 bookworm manifest digest verified for
this release:

```sh
bash deploy/bootstrap.sh 'postgres:17.12-bookworm@sha256:40865bc877a289da75963d97d72b59633f639fc5af7a4e856bfa3714cae5b536'
```

The script installs fixed root-owned deployment files under `/opt/test-ai-builder`,
creates `/etc/test-ai-builder/deploy.env` only when absent, and creates `ai-deploy`.
That user has no Docker-group membership and can sudo only the fixed deploy command.
It accepts exactly a 64-character image digest and a 40-character commit SHA. Ordinary
releases cannot replace the root scripts/Compose file. Changes to those files require
an explicit root bootstrap update from a reviewed checkout.

The generated deployment env contains PostgreSQL image and three independent random
passwords (postgres administration, `ai_migrator`, `ai_runtime`). It is root:root 0600.
Provider files `/etc/test-ai-builder/secrets.env` and `tavily.env` are never touched.
Runtime has DML on app/checkpoint data and sequence usage, but no schema/database DDL;
migration-version tables are read-only to runtime. Migrations use `ai_migrator` only.

Install a dedicated deployment public key in `/home/ai-deploy/.ssh/authorized_keys`.
Restrict the CI key with `restrict` (no forwarding/PTY); SSH-tunnel access uses a separate
owner key/account because the CI key must not forward ports. Pin the host key in GitHub
Actions; never set `StrictHostKeyChecking=no`. This technical workflow uses a public
GHCR package containing only the public source/build. GitHub initially creates packages
as private: after the first publish, set `bcmle/test_ai_builder` package visibility to
public and rerun the manual release. The workflow checks anonymous digest access before
SSH; the server needs no registry token. Provider keys and runtime data are never in the image.

## Release

GitHub CI tests PostgreSQL integration and builds the Linux image. A manual release
workflow chooses a tested commit from `main`, pushes the image to GHCR with an OCI
`org.opencontainers.image.revision` label, then invokes:

```sh
sudo /usr/local/sbin/test-ai-builder-deploy IMAGE_DIGEST_HEX FULL_COMMIT_SHA
```

The immutable application image is always
`ghcr.io/bcmle/test_ai_builder@sha256:IMAGE_DIGEST_HEX`. The root script verifies its
revision label and `io.ai-builder.deploy-bundle-sha256` label. The latter must match
both `/opt/test-ai-builder/bundle.sha256` and a fresh hash of the installed production
Compose/scripts/SQL. A mismatch stops release before services are stopped or migrations
run; install the reviewed host bundle explicitly first. CI/release compute the label
with `bash deploy/bundle-hash.sh` from the selected commit. The hash excludes CI-only
helpers and documentation, includes bootstrap/hash scripts, and ignores timestamps
and checkout paths. It serializes deployments with flock, checks disk, pulls images,
stops writers, backs up any existing installation, provisions roles, explicitly
runs migrations/grants, runs smoke, starts web, verifies readiness/preview and repeats
smoke. A post-run backup is restored into a new scratch database and directory, where
smoke validates persisted checkpoints, usage and artifacts without new model calls.
Only then is success recorded in `/var/lib/test-ai-builder/releases/COMMIT.json`.

The second smoke invocation reports zero fixture HTTP calls and unchanged 15 physical
fixture attempts / synthetic tariff total. These values are fixture accounting, not
actual API charges. Initial empty installation is recorded separately. An existing
unrecognized database is preserved and may block a deployment for manual inspection.
Failure never triggers an automatic destructive rollback or restart after a failed migration.

Persistent storage:

- Named Docker volume `test_ai_builder_pgdata`: PostgreSQL.
- `/var/lib/test-ai-builder/data`: managed evidence/preview, UID/GID 10001, mode 0700.
- `/var/backups/test-ai-builder`: root-only database/files backups.
- `/var/lib/test-ai-builder/releases`: commit/digest/migration release manifests.

Do not use `docker compose down -v`. Do not print `docker compose config` without
`--quiet`: interpolated environment values contain database credentials.

## View the technical version

From the Mac, using the owner's SSH account and verified host key:

```sh
ssh -N -L 8080:127.0.0.1:8080 OWNER@AEZA_HOST
```

Open `http://127.0.0.1:8080/preview`. `/api/v1/health/live` checks the process;
`/api/v1/health/ready` checks PostgreSQL/migration versions and artifact storage.
No server port 8080 or PostgreSQL port is opened publicly. HTTPS/Caddy and owner
application authentication precede any later public release.

## Backup, restore and rollback

Deployment pauses writes before each backup. Each snapshot contains a custom-format
pg_dump of both schemas, a tar of managed data, per-file hashes, image identity,
migration checksums and ledger/checkpoint counts. SHA256SUMS covers these files.
Restore verification creates and later drops only its own uniquely named scratch
DB. Restored file trees and results remain as private verification evidence; the
production DB/path are not overwritten. No migrations run during restore verification.

Before considering the first technical deployment accepted, transfer one verified
snapshot over authenticated SSH to the Mac and encrypt it with the owner's backup
public key (for example age). Keep the private decryption key separately from the
archive and test decrypting the archive and checking SHA256SUMS. Do not commit either
archive or key. Automated daily external backups and retention from spec 10 are a
separate prerequisite before live/public operation; a same-VPS snapshot is insufficient.

Rollback to a previous image is allowed only after confirming domain/checkpoint
schema compatibility. Incompatible rollback requires a reviewed restore plan with
an explicit statement of data lost after the backup. The deploy script does not
perform that decision automatically.

## Verification boundary

Shell syntax and Compose topology can be checked locally. Linux image build, actual
Docker operations, restricted-role permissions, SSH access, digest delivery, remote
preview and backup/restore must pass on CI/the server before this deployment is called
complete. Availability of these files alone is not evidence of a successful release.

CI command after building the local image:

```sh
sudo GITHUB_RUN_ID="$GITHUB_RUN_ID" GITHUB_RUN_ATTEMPT="$GITHUB_RUN_ATTEMPT" bash deploy/ci-smoke.sh ai-builder-ci:tested
```

It uses a unique CI volume and scratch directory, checks denied runtime DDL, runs the
actual smoke twice across a web restart, then restores the dump/files into a scratch
database and validates them through smoke again. Cleanup removes only that CI project
and volume. Never upload its generated ci.env or database dump as a public artifact.
