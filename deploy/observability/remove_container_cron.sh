#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'must run as root' >&2; exit 77; }
exec 9>/run/lock/project-paper-observability.lock
/usr/bin/flock -n 9 || { echo 'observability removal already running' >&2; exit 75; }
state=/var/lib/project-paper-observability/mini-production
control=/var/lib/project-paper-observability/mini-production-control
backup_file="$control/CURRENT-BACKUP"
if [[ -s "$backup_file" ]]; then
  backup=$(cat "$backup_file")
  manifest="$backup/manifest"
  if [[ -s "$manifest" ]]; then
    while IFS='|' read -r target previous; do
      rm -f "$target"
      [[ -f "$previous" ]] && cp -p "$previous" "$target"
    done < "$manifest"
    mv -f "$manifest" "$manifest.restored"
    rm -f "$backup_file"
    echo 'container observability installation restored'
    exit 0
  fi
  echo 'active rollback pointer has no valid manifest; refusing destructive action' >&2
  exit 75
fi
echo 'container observability installation already absent; nothing to restore'
