#!/usr/bin/env bash
# Destructive only to the fresh Compose project created by this acceptance run.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

APP_IMAGE=${PAPER_ACCEPTANCE_APP_IMAGE:-}
OLLAMA_IMAGE=${PAPER_ACCEPTANCE_OLLAMA_IMAGE:-}
REPORT_DIR=${PAPER_ACCEPTANCE_REPORT_DIR:-$ROOT/release-acceptance-results}
SCOUT_TIMEOUT=${PAPER_ACCEPTANCE_SCOUT_TIMEOUT:-2100}
PORT=${PAPER_ACCEPTANCE_PORT:-8000}
BASE_URL=http://127.0.0.1:$PORT
project=
passed=0

fail() { echo "Release acceptance failed: $*" >&2; exit 1; }
[[ -n "$APP_IMAGE" ]] || fail "Set PAPER_ACCEPTANCE_APP_IMAGE to the immutable release index digest"
[[ "$APP_IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || fail "Application image must use an immutable sha256 digest"
[[ -n "$OLLAMA_IMAGE" ]] || fail "Set PAPER_ACCEPTANCE_OLLAMA_IMAGE to the selected Ollama digest"
[[ "$OLLAMA_IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || fail "Ollama image must use an immutable sha256 digest"
[[ "$SCOUT_TIMEOUT" != *[!0-9]* && "$SCOUT_TIMEOUT" -gt 0 ]] || fail "Scout timeout must be a positive integer"
[[ "$PORT" != *[!0-9]* && "$PORT" -gt 0 && "$PORT" -le 65535 ]] || fail "Port must be an integer from 1 to 65535"

[[ ! -e .env && ! -e .paper-install ]] || fail "Acceptance requires a fresh checkout without installation state"
[[ ! -e .paper-install.lock ]] || fail "An installer lock already exists"

mkdir -p "$REPORT_DIR"
rm -f "$REPORT_DIR/status.txt" "$REPORT_DIR/compose.log" "$REPORT_DIR/scout-status.json" "$REPORT_DIR/scout-last.log"
printf 'host=%s/%s\napp_image=%s\nollama_image=%s\nport=%s\nstarted_at=%s\n' \
  "$(uname -s)" "$(uname -m)" "$APP_IMAGE" "$OLLAMA_IMAGE" "$PORT" "$(date -u +%FT%TZ)" > "$REPORT_DIR/status.txt"

compose() {
  [[ -n "$project" ]] || return 1
  docker compose --project-name "$project" --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/docker-compose.yml" "$@"
}

collect_and_clean() {
  code=$?
  if [[ -f .paper-install ]]; then project=$(sed -n 's/^project=//p' .paper-install); fi
  if [[ -n "$project" ]]; then
    compose logs --no-color app ollama prepare-model > "$REPORT_DIR/compose.log" 2>&1 || true
    compose exec -T app sh -c 'cat /app/data/scout-status.json 2>/dev/null || true' > "$REPORT_DIR/scout-status.json" 2>/dev/null || true
    compose exec -T app sh -c 'cat /app/data/scout-last.log 2>/dev/null || true' > "$REPORT_DIR/scout-last.log" 2>/dev/null || true
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -f .env .paper-install
  rmdir .paper-install.lock 2>/dev/null || true
  printf 'completed_at=%s\nresult=%s\n' "$(date -u +%FT%TZ)" "$([[ "$passed" == 1 ]] && echo pass || echo fail)" >> "$REPORT_DIR/status.txt"
  exit "$code"
}
trap collect_and_clean EXIT

if [[ "$PORT" != 8000 ]]; then
  cp .env.example .env
  printf '\nPAPER_PORT=%s\nPAPER_APP_IMAGE=%s\nPAPER_OLLAMA_IMAGE=%s\n' "$PORT" "$APP_IMAGE" "$OLLAMA_IMAGE" >> .env
fi
bash scripts/install_project_paper.sh --app-image "$APP_IMAGE" --ollama-image "$OLLAMA_IMAGE"
project=$(sed -n 's/^project=//p' .paper-install)
[[ "$project" =~ ^paper-[a-z0-9-]+$ ]] || fail "Installer did not record a valid project identity"

curl --fail --silent --show-error "$BASE_URL/ready" | python3 -c 'import json,sys; assert json.load(sys.stdin)["ready"] is True'
curl --fail --silent --show-error "$BASE_URL/runtime" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); assert d["ready"] is True and d["model"]["present"] is True'

# Exercise the same form submission as a customer adding a topic in the web UI.
topic_code=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --data-urlencode 'action=add' --data-urlencode 'topic_text=AIOps reliability' \
  --data-urlencode 'query=AIOps' --data-urlencode 'sources=arxiv' \
  --data-urlencode 'cadence=manual' --data-urlencode 'priority=normal' \
  --data-urlencode 'enabled=1' "$BASE_URL/topics")
[[ "$topic_code" == 303 ]] || fail "Topic UI submission returned HTTP $topic_code"

# The UI performs its own bounded inference check before enabling Scout.
deadline=$((SECONDS + 180))
while true; do
  curl --fail --silent --show-error "$BASE_URL/scout/status" > "$REPORT_DIR/scout-status.json"
  can_run=$(python3 -c 'import json,sys; print("1" if json.load(open(sys.argv[1]))["can_run"] else "0")' "$REPORT_DIR/scout-status.json")
  [[ "$can_run" == 1 ]] && break
  [[ "$SECONDS" -lt "$deadline" ]] || fail "Scout did not become ready after model preparation"
  sleep 5
done

run_code=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --request POST "$BASE_URL/scout/run")
[[ "$run_code" == 303 ]] || fail "Run Scout UI submission returned HTTP $run_code"

deadline=$((SECONDS + SCOUT_TIMEOUT))
while true; do
  curl --fail --silent --show-error "$BASE_URL/scout/status" > "$REPORT_DIR/scout-status.json"
  state=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$REPORT_DIR/scout-status.json")
  case "$state" in completed) break ;; empty|failed|interrupted) fail "Scout ended in $state; inspect uploaded evidence" ;; esac
  [[ "$SECONDS" -lt "$deadline" ]] || fail "Scout exceeded the acceptance timeout"
  sleep 10
done

ids=$(compose exec -T app python -c \
  'import sqlite3; c=sqlite3.connect("/app/data/paper_agent.db"); r=c.execute("SELECT r.paper_id,r.id FROM recommendations r ORDER BY r.id DESC LIMIT 1").fetchone(); assert r; print(f"{r[0]} {r[1]}")')
read -r paper_id recommendation_id <<< "$ids"
before=$(compose exec -T app python -c \
  'import sqlite3; c=sqlite3.connect("/app/data/paper_agent.db"); print(*[c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("papers","recommendations")])')
read -r papers_before recommendations_before <<< "$before"
[[ "$papers_before" -gt 0 && "$recommendations_before" -gt 0 ]] || fail "Discovery did not persist papers and recommendations"

feedback='Decision: keep
Score: 4.5
Reason: Release acceptance feedback persistence check.
More of: operational evidence
Less of: purely theoretical framing'
feedback_code=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --data-urlencode "paper_id=$paper_id" --data-urlencode "recommendation_id=$recommendation_id" \
  --data-urlencode 'action=feedback' --data-urlencode "notes=$feedback" --data-urlencode 'return_to=/' "$BASE_URL/feedback")
[[ "$feedback_code" == 303 ]] || fail "Feedback UI submission returned HTTP $feedback_code"

feedback_before=$(compose exec -T app python -c \
  'import sqlite3; c=sqlite3.connect("/app/data/paper_agent.db"); print(c.execute("SELECT COUNT(*) FROM raw_feedback").fetchone()[0])')
[[ "$feedback_before" -gt 0 ]] || fail "Feedback was not persisted"

compose up -d --no-build --pull never --force-recreate app
deadline=$((SECONDS + 90))
until curl --fail --silent --output /dev/null "$BASE_URL/ready"; do
  [[ "$SECONDS" -lt "$deadline" ]] || fail "App did not recover after container recreation"
  sleep 3
done
after=$(compose exec -T app python -c \
  'import sqlite3; c=sqlite3.connect("/app/data/paper_agent.db"); print(*[c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("papers","recommendations","raw_feedback")])')
read -r papers_after recommendations_after feedback_after <<< "$after"
[[ "$papers_after" == "$papers_before" && "$recommendations_after" == "$recommendations_before" && "$feedback_after" == "$feedback_before" ]] \
  || fail "Data counts changed across app recreation"

# A queued state without a lock owner must reconcile to interrupted.
compose exec -T app python -c \
  'import json,time; json.dump({"run_id":"acceptance-interrupted","status":"queued","origin":"web","topics":["AIOps"],"source":"arxiv","started_at":int(time.time()),"message":"Queued for acceptance recovery."},open("/app/data/scout-status.json","w"))'
curl --fail --silent --show-error "$BASE_URL/scout/status" > "$REPORT_DIR/scout-status.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["status"] == "interrupted" and d["busy"] is False' "$REPORT_DIR/scout-status.json"

printf 'project=%s\npapers=%s\nrecommendations=%s\nfeedback=%s\nscout_recovery=interrupted\n' \
  "$project" "$papers_after" "$recommendations_after" "$feedback_after" >> "$REPORT_DIR/status.txt"
passed=1
echo "Release acceptance passed."
