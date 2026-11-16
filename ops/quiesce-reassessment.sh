#!/usr/bin/env bash
# Stop only an explicitly recognized research Python process at a quiet boundary.
# No database writes, credentials, budget changes, TERM, KILL, or Docker pause.
set -Eeuo pipefail
umask 077

target_name=${QUIESCE_CONTAINER:-ai-builder-reassessment-v152}
expected_parent=1d76fe99e56240989cd67794b7143dce
expected_module=ai_builder.reassess_collection
expected_schema=1.2
case "$target_name" in
  ai-builder-reassessment-v152|ai-builder-reassessment-v152-repair1|ai-builder-reassessment-v152-repair2) ;;
  ai-builder-factor-v153)
    expected_parent=13057b6a4dbf418fb01b2ac5c93a3fc1
    expected_module=ai_builder.factor_revision
    expected_schema=1.3 ;;
  *) echo 'Refusing an unrecognized research container name.' >&2; exit 2 ;;
esac
if (( $# < 4 || $# > 5 )); then
  echo 'Usage: quiesce-reassessment.sh COLLECTION_ID POSTGRES_CONTAINER DATABASE USER [MAX_SECONDS=600]' >&2
  exit 2
fi
collection_id=$1
task_db_container=$2
task_db_name=$3
task_db_user=$4
max_seconds=${5:-600}
[[ $collection_id =~ ^[a-zA-Z0-9_-]{1,100}$ ]] || exit 2
[[ $task_db_container =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$ ]] || exit 2
[[ $task_db_name =~ ^[a-zA-Z_][a-zA-Z0-9_-]{0,62}$ ]] || exit 2
[[ $task_db_user =~ ^[a-zA-Z_][a-zA-Z0-9_-]{0,62}$ ]] || exit 2
[[ $max_seconds =~ ^[0-9]+$ ]] && (( max_seconds >= 1 && max_seconds <= 600 )) || exit 2
for binary in docker python3 timeout; do command -v "$binary" >/dev/null || exit 2; done
python3 -c 'import os,signal; assert hasattr(os,"pidfd_open") and hasattr(signal,"pidfd_send_signal")' || exit 2

stopped=0
target_pid=''
target_start=''
target_id=''
log() { printf '%s\n' "$*"; }

# A pidfd pins process identity across PID reuse. Starttime is checked AFTER
# opening the descriptor, before sending any signal. Only one process is sent
# a signal; never its process group, the web service, or the database container.
signal_target() {
  python3 - "$target_pid" "$target_start" "$1" <<'PY'
import os, signal, sys
pid, expected, name = int(sys.argv[1]), sys.argv[2], sys.argv[3]
fd = os.pidfd_open(pid)
try:
    with open(f'/proc/{pid}/stat') as source:
        actual = source.read().rsplit(') ', 1)[1].split()[19]
    if actual != expected:
        raise SystemExit('Refusing signal: process identity changed.')
    signal.pidfd_send_signal(fd, getattr(signal, 'SIG' + name), None, 0)
finally:
    os.close(fd)
PY
}

cleanup() {
  rc=$?
  trap - EXIT HUP INT TERM
  if (( stopped )); then
    if signal_target CONT 2>/dev/null; then
      log 'Cleanup resumed the research process.'
    elif [[ -e /proc/$target_pid/stat ]]; then
      log 'ATTENTION: unable to resume the original process; inspect its identity and send CONT manually.' >&2
      rc=4
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

inspect_target() {
  timeout --kill-after=1s 3s docker inspect --format '{{.Id}}|{{.State.Pid}}|{{.State.Running}}|{{.State.Paused}}|{{.HostConfig.RestartPolicy.Name}}|{{.Name}}' "$target_name" 2>/dev/null
}
identity=$(inspect_target) || { log 'Cannot inspect the research container.' >&2; exit 2; }
IFS='|' read -r target_id target_pid running paused restart inspected_name <<< "$identity"
[[ $target_id =~ ^[a-f0-9]{64}$ && $target_pid =~ ^[1-9][0-9]*$ && $running == true && $paused == false && $restart == no && $inspected_name == /$target_name ]] || {
  log 'Expected a running, unpaused research container with no restart policy.' >&2; exit 2;
}
mapfile -d '' -t task_argv < "/proc/$target_pid/cmdline"
[[ ${task_argv[0]##*/} =~ ^python([0-9]+([.][0-9]+)?)?$ && ${task_argv[1]:-} == -m && ${task_argv[2]:-} == "$expected_module" ]] || {
  log 'Container PID is not the expected direct Python research command.' >&2; exit 2;
}
# Bind either the resumed collection or the known original launch parameters.
# Initial launch creates its collection inside Python, so DB provenance below
# is additionally required BEFORE ANY SIGNAL.
command_matches=0
launch_parent=''
launch_config=''
launch_settings=''
launch_analogues=''
launch_first=''
for (( i=3; i+1<${#task_argv[@]}; i++ )); do
  if [[ ${task_argv[i]} == --resume-collection && ${task_argv[i+1]} == "$collection_id" ]]; then command_matches=1; fi
  case ${task_argv[i]} in
    --parent-collection) launch_parent=${task_argv[i+1]} ;;
    --config) launch_config=${task_argv[i+1]} ;;
    --settings) launch_settings=${task_argv[i+1]} ;;
    --analogues) launch_analogues=${task_argv[i+1]} ;;
    --first-model) launch_first=${task_argv[i+1]} ;;
  esac
done
if (( ${#task_argv[@]} == 13 )) && [[ $launch_parent == "$expected_parent" && ${launch_config##*/} == conditional-config.json && ${launch_settings##*/} == conditional-roles.json && ${launch_analogues##*/} == conditional-analogues.json && $launch_first == 23c49394cadf4e0ab3b7f78d9d70373a ]]; then
  command_matches=1
fi
(( command_matches )) || { log 'Command does not match the known initial launch or exact resumed collection; no signal sent.' >&2; exit 2; }
task_stat=$(<"/proc/$target_pid/stat")
task_rest=${task_stat##*) }
read -ra task_fields <<< "$task_rest"
target_start=${task_fields[19]}
read -r _ sig_ignored < <(grep '^SigIgn:' "/proc/$target_pid/status")
read -r _ sig_caught < <(grep '^SigCgt:' "/proc/$target_pid/status")
(( (16#${sig_ignored: -2} & 2) == 0 && (16#${sig_caught: -2} & 2) != 0 )) || {
  log 'SIGINT is not caught, or is ignored; refusing to stop this process.' >&2; exit 2;
}

provenance=$(timeout --signal=TERM --kill-after=1s 3s docker exec -i "$task_db_container" \
  psql -X -qAt -v ON_ERROR_STOP=1 -v "target_collection=$collection_id" -v "expected_parent=$expected_parent" -v "expected_schema=$expected_schema" \
  -U "$task_db_user" -d "$task_db_name" <<'SQL'
BEGIN READ ONLY;
SET LOCAL statement_timeout = '1200ms';
SELECT
 (SELECT count(*) FROM app.collections WHERE id=:'target_collection' AND state='running'
   AND context->>'reassessment_of_collection_id'=:'expected_parent'
   AND context->'config'->>'schema_version'=:'expected_schema'
   AND (:'expected_schema'='1.2' OR
        (context->>'execution_version'='collection-3' AND jsonb_array_length(context->'selected_model_ids')=5))) || '|' ||
 (SELECT count(*) FROM app.collections WHERE state IN ('discovering','running'));
COMMIT;
SQL
) || { log 'Read-only collection provenance check failed; no signal sent.' >&2; exit 3; }
[[ $provenance == '1|1' ]] || { log 'Target is not the unique active reassessment of the expected parent; no signal sent.' >&2; exit 3; }

read_boundary() {
  timeout --signal=TERM --kill-after=1s 3s docker exec -i "$task_db_container" \
    psql -X -qAt -v ON_ERROR_STOP=1 -v "target_collection=$collection_id" -U "$task_db_user" -d "$task_db_name" <<'SQL'
BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY;
SET LOCAL statement_timeout = '1200ms';
SET LOCAL lock_timeout = '200ms';
-- First statement is a transaction barrier: invisible/uncommitted worker
-- reservations cannot be judged safe from ledger counts alone. Lack of stats
-- permissions also fails closed because NULL activity is counted as busy.
SELECT count(*) FROM pg_stat_activity
 WHERE datname=current_database() AND pid<>pg_backend_pid()
   AND backend_type='client backend'
   AND (COALESCE(state,'unknown')<>'idle' OR xact_start IS NOT NULL);
-- A fresh READ COMMITTED snapshot follows that barrier. Frozen Python cannot
-- issue another request. All children and coordinator are included, not only
-- the model that happened to be running when this helper was started.
WITH selected AS (
 SELECT coordinator_run_id AS run_id FROM app.collections WHERE id=:'target_collection'
 UNION SELECT run_id FROM app.collection_items WHERE collection_id=:'target_collection' AND run_id IS NOT NULL
)
SELECT
 (SELECT count(*) FROM app.collections WHERE id=:'target_collection') || '|' ||
 (SELECT count(*) FROM app.usage WHERE run_id IN (SELECT run_id FROM selected) AND state='reserved') || '|' ||
 (SELECT count(*) FROM app.usage WHERE run_id IN (SELECT run_id FROM selected) AND state='unknown') || '|' ||
 (SELECT count(*) FROM app.provider_operations WHERE run_id IN (SELECT run_id FROM selected) AND state='running') || '|' ||
 (SELECT count(*) FROM app.source_fetch_attempts WHERE run_id IN (SELECT run_id FROM selected) AND state IN ('running','unknown')) || '|' ||
 (SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database());
COMMIT;
SQL
}

started=$SECONDS
attempt=0
while (( SECONDS-started < max_seconds )); do
  [[ $(inspect_target) == "$identity" ]] || { log 'Container identity or state changed; stopping helper.' >&2; exit 2; }
  stopped=1
  signal_target STOP
  # Let already-sent local database messages drain before inspecting activity.
  sleep 0.15
  [[ $(awk '/^State:/ {print $2}' "/proc/$target_pid/status") == T ]] || {
    log 'Process did not enter STOP state.' >&2; exit 2;
  }
  if ! boundary=$(read_boundary); then
    signal_target CONT
    stopped=0
    log 'Database check failed; research resumed. No interrupt was sent.' >&2
    exit 3
  fi
  mapfile -t rows <<< "$boundary"
  if (( ${#rows[@]} != 2 )) || [[ ! ${rows[0]} =~ ^[0-9]+$ || ! ${rows[1]} =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+$ ]]; then
    log 'Unexpected database response; no interrupt sent.' >&2
    exit 3
  fi
  IFS='|' read -r collection_count reserved unknown providers source_reads prepared <<< "${rows[1]}"
  (( collection_count == 1 )) || { log 'Collection does not exist uniquely; no interrupt sent.' >&2; exit 3; }
  if (( rows[0] == 0 && reserved == 0 && unknown == 0 && providers == 0 && source_reads == 0 && prepared == 0 )); then
    # Queue INT while frozen, then resume. Python's caught handler cancels the
    # asyncio main task so existing finally blocks save clocks/checkpoints/state.
    signal_target INT
    signal_target CONT
    stopped=0
    log 'Quiet boundary found. SIGINT queued and research resumed for graceful cleanup.'
    for (( grace=0; grace<30; grace++ )); do
      if [[ ! -e /proc/$target_pid/stat ]]; then log 'Original research process exited. Verify saved state before restarting.'; exit 0; fi
      task_stat=$(<"/proc/$target_pid/stat")
      task_rest=${task_stat##*) }
      read -ra task_fields <<< "$task_rest"
      if [[ ${task_fields[19]} != "$target_start" || ${task_fields[0]} == Z ]]; then
        log 'Original research process exited. Verify saved state before restarting.'; exit 0
      fi
      sleep 1
    done
    log 'SIGINT was delivered, but exit was not observed within 30 seconds. No further signal was sent.' >&2
    exit 4
  fi
  signal_target CONT
  stopped=0
  (( attempt+=1 ))
  if (( attempt == 1 || attempt%5 == 0 )); then
    log "Waiting: db_busy=${rows[0]} reserved=$reserved unknown=$unknown provider_running=$providers source_running_or_unknown=$source_reads prepared=$prepared."
  fi
  # Receipt/checkpoint transitions can be shorter than a second. Sample often
  # enough to catch a quiet boundary while keeping each STOP window bounded.
  sleep 0.25
done
log 'No quiet boundary within the time limit. Research is running; no interrupt was sent.'
exit 3
