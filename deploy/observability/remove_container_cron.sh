#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'must run as root' >&2; exit 77; }
exec 9>/run/lock/project-paper-observability.lock
/usr/bin/flock -n 9 || { echo 'observability removal already running' >&2; exit 75; }
state=/var/lib/project-paper-observability/mini-production
backup_file="$state/CURRENT-BACKUP"
if [[ -s "$backup_file" ]]; then
  backup=$(cat "$backup_file")
  manifest="$backup/manifest"
  if [[ -s "$manifest" ]]; then
    while IFS='|' read -r target previous; do
      rm -f "$target"
      [[ -f "$previous" ]] && mv -f "$previous" "$target"
    done < "$manifest"
    mv -f "$manifest" "$manifest.restored"
    rm -f "$backup_file"
    echo 'container observability installation restored'
    exit 0
  fi
fi
rm -f /etc/cron.d/project-paper-observability-mini-production /usr/local/sbin/project-paper-observability-container
echo 'container observability cron removed'
