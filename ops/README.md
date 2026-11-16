# Reassessment recovery

`quiesce-reassessment.sh` is a narrowly scoped operator helper for the running
`ai-builder-reassessment-v152` Python container. It does not alter the deployment
bundle, database, immutable research inputs or budgets.

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
