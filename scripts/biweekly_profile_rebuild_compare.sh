#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
LOCK_DIR="${PAPER_AGENT_LOCK_DIR:-/tmp}"
LOCK_PATH="$LOCK_DIR/project-paper-profile-rebuild.lock"
ANCHOR_DATE="${PAPER_AGENT_PROFILE_REBUILD_ANCHOR:-2026-09-07}"
TODAY="${PAPER_AGENT_TODAY:-$(date +%F)}"

should_run="$(
  python3 - "$ANCHOR_DATE" "$TODAY" <<'PY'
from __future__ import annotations

import sys
from datetime import date

anchor = date.fromisoformat(sys.argv[1])
today = date.fromisoformat(sys.argv[2])
delta_days = (today - anchor).days
print("yes" if delta_days >= 0 and delta_days % 14 == 0 else "no")
PY
)"

if [[ "$should_run" != "yes" ]]; then
  echo "Skipping Gemini profile rebuild comparison: $TODAY is not on the biweekly Monday cadence anchored at $ANCHOR_DATE."
  exit 0
fi

cd "$REPO_DIR"
mkdir -p logs
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "Skipping Gemini profile rebuild comparison: another run holds $LOCK_PATH."
  exit "${PAPER_AGENT_BUSY_EXIT_CODE:-0}"
fi

echo "Running Gemini profile rebuild comparison for $TODAY."
scripts/compare_feedback_profile_rebuild.sh
