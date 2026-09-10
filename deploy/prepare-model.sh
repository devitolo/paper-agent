#!/bin/sh
# Runs in the selected Ollama image and communicates only through its API.
set -eu
exec 3>&2
model=qwen2.5:1.5b-instruct
budget=${PAPER_MODEL_PREPARE_TIMEOUT:-1800}
attempts=${PAPER_MODEL_PULL_ATTEMPTS:-2}
case "$budget:$attempts" in *[!0-9:]*|:*) echo 'failed: invalid model preparation limits' >&2; exit 2;; esac
[ "$budget" -gt 0 ] && [ "$attempts" -gt 0 ] || exit 2
worker=
watchdog=
cleanup() {
  [ -z "$worker" ] || kill "$worker" 2>/dev/null || true
  [ -z "$watchdog" ] || kill "$watchdog" 2>/dev/null || true
}
trap cleanup EXIT
trap 'echo "failed: model preparation timed out or interrupted; inspect logs and retry" >&3; exit 1' TERM INT
parent=$$
(sleeper=; trap '[ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null; exit 0' TERM
 sleep "$budget" & sleeper=$!; wait "$sleeper"; kill -TERM "$parent" 2>/dev/null) >/dev/null 2>&1 &
watchdog=$!
run_cli() {
  ollama "$@" &
  worker=$!
  result=0
  wait "$worker" || result=$?
  worker=
  return "$result"
}
echo 'waiting: Ollama API'
until run_cli list >/dev/null 2>&1; do sleep 2; done
if ! run_cli show "$model" >/dev/null 2>&1; then
  count=1
  while :; do
    echo "pulling: $model (attempt $count/$attempts)"
    ollama pull "$model" &
    worker=$!
    if wait "$worker"; then worker=; break; fi
    worker=
    [ "$count" -lt "$attempts" ] || { echo 'failed: model pull; retry preparation' >&2; exit 1; }
    count=$((count + 1))
    sleep 2
  done
fi
echo 'ready: model present; installer performs a bounded inference check before success'
