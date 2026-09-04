#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
CRONTAB_TEMPLATE="$REPO_DIR/deploy/project-paper.crontab"
MODE="${1:---dry-run}"

if [[ "$MODE" != "--dry-run" && "$MODE" != "--apply" ]]; then
  echo "Usage: $0 [--dry-run|--apply]" >&2
  exit 2
fi

if [[ ! -f "$CRONTAB_TEMPLATE" ]]; then
  echo "Missing crontab template: $CRONTAB_TEMPLATE" >&2
  exit 1
fi

current="$(mktemp)"
filtered="$(mktemp)"
next="$(mktemp)"
cleanup() {
  rm -f "$current" "$filtered" "$next"
}
trap cleanup EXIT

crontab -l > "$current" 2>/dev/null || true

python3 - "$current" "$filtered" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

current_path = Path(sys.argv[1])
filtered_path = Path(sys.argv[2])

begin = "# BEGIN PROJECT PAPER MANAGED JOBS"
end = "# END PROJECT PAPER MANAGED JOBS"
legacy_markers = (
    "paper-agent",
    "paper_agents.cli pipeline-daily",
    "scripts/nightly_pipeline.sh",
    "scripts/openalex_pipeline.sh",
    "pipeline-semantic-scholar.log",
    "scripts/backup_db.sh",
    "scripts/biweekly_profile_rebuild_compare.sh",
)

lines = current_path.read_text().splitlines()
out: list[str] = []
in_managed = False

for line in lines:
    stripped = line.strip()
    if stripped == begin:
        in_managed = True
        continue
    if stripped == end:
        in_managed = False
        continue
    if in_managed:
        continue
    if stripped and not stripped.startswith("#") and any(marker in line for marker in legacy_markers):
        continue
    out.append(line)

while out and not out[-1].strip():
    out.pop()

filtered_path.write_text("\n".join(out) + ("\n" if out else ""))
PY

{
  if [[ -s "$filtered" ]]; then
    cat "$filtered"
    echo
  fi
  cat "$CRONTAB_TEMPLATE"
} > "$next"

if [[ "$MODE" == "--dry-run" ]]; then
  echo "Dry run. Proposed crontab:"
  cat "$next"
  exit 0
fi

crontab "$next"
echo "Installed Project Paper cron jobs from $CRONTAB_TEMPLATE."
