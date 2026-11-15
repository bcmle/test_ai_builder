# Prototype deployment on Aeza

## Current v1.3.1 domain release

Caddy 2.10.2 exposes HTTPS at `https://ai-builder-lab.com/` for GET/HEAD on `/`, `/runs/*`, `/models/*`,
`/compare`, `/methodology` and `/assets/*`. Other paths and methods return 404.
The proxy has no provider credentials or application-data mount. Web reads only
the separate public JSON projection; private evidence is never a static directory.
Domain HTTP redirects to HTTPS; the original HTTP by IP remains a read-only fallback.
Owner controls are deferred. Local web health/preview remain at 127.0.0.1:8080.

Aeza DNS has one apex A record pointing to `77.232.142.154` (TTL 300); no AAAA or
www record is required. Public TCP 80/443 map to Caddy's internal 8080/8443.
Caddy automatically obtains and renews the certificate. Its `/data` and `/config`
are private persistent directories under `/var/lib/test-ai-builder/caddy`, owned
by UID/GID 10001 with mode 0700. Never publish these files or certificate keys.
CI uses an explicit HTTP-only site address and temporary directories, so it does
not request production certificates. The server release checks trusted HTTPS,
the domain redirect, private-route denial and TLS persistence after recreation.

Before this release, reinstall the reviewed root-owned deployment bundle using
the bootstrap command below and the existing age recipient. This adds Caddyfile
and updates the bundle hash without rotating credentials or changing SSH access.
Then release the selected full main SHA through the existing GitHub Actions workflow.
Research is a separate explicit one-off container command; see the README. Stop
an active research container before starting a release, so backup has no writers.

## Internal technical services

This profile runs PostgreSQL, the web process and an explicit synthetic smoke job.
It is not the production research worker. No provider API keys are mounted or passed.
`/preview` is enabled only for this profile and serves one validated synthetic report;
private artifacts are not exposed as a directory. Web binds only to server loopback.
Web joins two networks: the private backend and a normal `preview` bridge used for
host port publication. PostgreSQL, migrate and smoke join only the `internal: true`
backend, so the synthetic graph has no external network route. The web bridge has
loopback as its default publish address and the explicit port mapping remains
`127.0.0.1:8080:8000`; it does not open the preview publicly. The web container has no
provider keys and does not execute research. This follows Docker's [bridge port
publication](https://docs.docker.com/engine/network/drivers/bridge/) and [separate
frontend/backend network](https://docs.docker.com/reference/compose-file/networks/) model.

## Bootstrap once

Prerequisites: reviewed commit, Ubuntu server with Docker Engine + Compose v2, curl,
openssl, age, sudo, flock, an active rental, verified SSH host fingerprint, at least 2 GiB
free disk. Confirm `uname -m`; CI must build the matching Linux architecture. Docker
installation and changes to SSH/firewall are separate bootstrap actions. No database
or existing provider secret file is deleted by these scripts.

Copy the reviewed `compose.yaml` and `deploy/` directory to a root-only checkout on
Aeza. Run as root, passing the PostgreSQL 17.12 bookworm manifest digest verified for
this release:

```sh
bash deploy/bootstrap.sh \
  'postgres:17.12-bookworm@sha256:40865bc877a289da75963d97d72b59633f639fc5af7a4e856bfa3714cae5b536' \
  'OWNER_AGE_PUBLIC_RECIPIENT'
```

The script installs fixed root-owned deployment files under `/opt/test-ai-builder`,
creates `/etc/test-ai-builder/deploy.env` only when absent, and creates `ai-deploy`.
That user has no Docker-group membership and can sudo only the fixed deploy command.
It accepts exactly a 64-character image digest and a 40-character commit SHA. Ordinary
releases cannot replace the root scripts/Compose file. Changes to those files require
an explicit root bootstrap update from a reviewed checkout.

The generated deployment env contains PostgreSQL image and three independent random
passwords (postgres administration, `ai_migrator`, `ai_runtime`). It is root:root 0600.
The second bootstrap argument is the owner’s age X25519 public recipient (`age1…`),
not a private key. It is validated by age and stored root:root 0600 at
`/etc/test-ai-builder/backup-recipient.age`. Re-running bootstrap with a different
recipient fails; key rotation requires an explicit recovery plan. The private key
stays on the Mac and is never placed in GitHub or on Aeza. Provider files
`/etc/test-ai-builder/secrets.env` and `tavily.env` are never touched.
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
runs migrations/grants, runs smoke, starts web and verifies readiness/preview. It
then stops web, restarts the existing PostgreSQL container without deleting its
volume, waits for PostgreSQL health, brings web back and rechecks readiness/preview.
The repeated smoke must reuse the saved run with zero new fixture HTTP calls. A post-run backup is restored into a new scratch database and directory, where
smoke validates persisted checkpoints, usage and artifacts without new model calls.
After restore verification, the release manifest is recorded at
`/var/lib/test-ai-builder/releases/COMMIT.json`. The verified snapshot and release
manifest are streamed through age encryption to an atomic
`/var/lib/test-ai-builder/export/COMMIT.tar.age` file. Deployment reports success
only after this ciphertext export is complete.

The second smoke invocation reports zero fixture HTTP calls and unchanged 15 physical
fixture attempts / synthetic tariff total. These values are fixture accounting, not
actual API charges. Initial empty installation is recorded separately. An existing
unrecognized database is preserved and may block a deployment for manual inspection.
Failure never triggers an automatic destructive rollback or restart after a failed migration.

Persistent storage:

- Named Docker volume `test_ai_builder_pgdata`: PostgreSQL.
- `/var/lib/test-ai-builder/data`: managed evidence/preview, UID/GID 10001, mode 0700.
- `/var/lib/test-ai-builder/caddy/data` and `config`: certificate/configuration state,
  UID/GID 10001, mode 0700; retained across Caddy replacement.
- `/var/backups/test-ai-builder`: root-only database/files backups.
- `/var/lib/test-ai-builder/releases`: root-only commit/digest/migration manifests.
- `/var/lib/test-ai-builder/export`: ciphertext only, root:ai-deploy 0750; files 0640.

The state parent is root:ai-deploy 0710, granting traversal but no directory listing.
Plaintext data remains UID 10001 mode 0700; releases/restore reports/backups remain
root-only. The deploy user can read exported ciphertext, not plaintext backups.

Do not use `docker compose down -v`. Do not print `docker compose config` without
`--quiet`: interpolated environment values contain database credentials.

## View the technical version

From the Mac, using the owner's SSH account and verified host key:

```sh
ssh -N -L 8080:127.0.0.1:8080 OWNER@AEZA_HOST
```

Open `http://127.0.0.1:8080/preview`. `/api/v1/health/live` checks the process;
`/api/v1/health/ready` checks PostgreSQL/migration versions and artifact storage.
No server port 8080 or PostgreSQL port is opened publicly. Caddy exposes only the
read-only prototype routes over domain HTTPS and the IP HTTP fallback; it blocks
technical health and preview URLs.

## Backup, restore and rollback

Deployment pauses writes before each backup. Each snapshot contains a custom-format
pg_dump of both schemas, a tar of managed data, per-file hashes, image identity,
migration checksums and ledger/checkpoint counts. SHA256SUMS covers these files.
Restore verification creates and later drops only its own uniquely named scratch
DB. Restored file trees and results remain as private verification evidence; the
production DB/path are not overwritten. No migrations run during restore verification.

Before the technical deployment is accepted, transfer the ciphertext off the VPS.
The release workflow reads only `/var/lib/test-ai-builder/export/COMMIT.tar.age`
through the unprivileged `ai-deploy` SSH account and stores it as a GitHub Actions
artifact. The Mac downloads that artifact with the GitHub CLI; direct Mac→Aeza SSH
is not required for backup transport. Decrypt locally with the owner’s private age
key into a private scratch directory, extract the snapshot and verify its SHA256SUMS.
Keep the encrypted archive and private key separately. Never upload plaintext dumps,
recipient private keys or decrypted files as workflow artifacts or commit them.

This transport complements SSH encryption with encryption to the owner’s key, so
GitHub and the deploy user only receive ciphertext. A failed encryption/export or
failed local decrypt/hash check leaves off-VPS backup acceptance incomplete. Automated
daily external backups and retention from spec 10 remain prerequisites before live/
public operation; a same-VPS snapshot alone is insufficient.

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
actual smoke twice across a PostgreSQL restart and web stop/start, then restores the dump/files into a scratch
database and validates them through smoke again. Cleanup removes only that CI project
and volume. Never upload its generated ci.env or database dump as a public artifact.

CI also executes `deploy/ci-export-smoke.sh` as root on a disposable GitHub-hosted
runner after installing `age`. It bootstraps the real bundle with a generated test
recipient, checks that a different recipient is rejected, and invokes the actual
export helper on an explicitly synthetic snapshot. It proves `ai-deploy` can read
ciphertext while being denied the plaintext dump, deployment credentials, release
manifest and test private key; then decrypts and checks both outer and evidence
hashes. PostgreSQL restoration is tested separately by `ci-smoke.sh`. Export-test
private keys and scratch files must never be uploaded as workflow artifacts.
