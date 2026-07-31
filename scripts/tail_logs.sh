#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="${1:-logs}"

if compgen -G "$LOG_DIR/*.log" > /dev/null; then
  tail -n 80 -F "$LOG_DIR"/*.log
else
  echo "no log files found in $LOG_DIR" >&2
  exit 1
fi
