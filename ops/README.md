# Reassessment recovery

`quiesce-reassessment.sh` is a narrowly scoped operator helper for the running
`ai-builder-reassessment-v152` Python container. The explicitly named recovery
container can be selected with
`QUIESCE_CONTAINER=ai-builder-reassessment-v152-repair1`; all other names are
rejected. It does not alter the deployment bundle, database, immutable research
inputs or budgets.

Run as root on the Linux Docker host with the exact collection ID, PostgreSQL
container, database and user. The optional wait limit is at most 600 seconds.
It requires Linux pidfds and a caught SIGINT handler. Initial-launch provenance
or the exact resume argument must match before any signal is sent.

The helper briefly freezes only the research Python process, checks database
transactions and all tracked in-flight operations, and resumes it immediately
while work is outstanding. At a quiet boundary it queues SIGINT and continues
the process so its existing cleanup can save state and release leases. It never
uses TERM or KILL against the research process. Timeout processes only bound
Docker/SQL inspection commands.

Handled errors and signals resume a frozen process. SIGKILL or host loss cannot
run the cleanup trap; an operator must verify the original container PID and
start time before sending CONT manually in that case. After a successful exit,
verify saved collection state, clocks, leases and unresolved usage before any
deploy or explicit resume. An uncertain external operation is never declared
settled or free by this helper.

Validation before first use: shell syntax, read-only boundary SQL on isolated
PostgreSQL, and STOP → queued INT → CONT on a local asyncio dummy. Linux
pidfd/proc/Docker integration was not available on the development Mac; runtime
checks fail closed when the required host features or identities differ.

`diagnose-factor-projection.py` reads saved repair inputs and provider responses
using the deployed application's `DATABASE_URL`. It establishes a read-only
transaction and replays the installed validation without provider calls or
database writes. Supply `--collection`, `--position` and optionally `--compact`
for console output. Run it through the existing application image; a temporary
compose container can be used after the research worker exits. Do not pass
provider credentials. Output omits full documents and connection secrets and is
bounded to 24,000 characters per candidate, or 3,600 in compact mode.
