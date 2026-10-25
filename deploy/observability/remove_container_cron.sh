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
    while IFS='|' read -r target previous present digest mode uid gid; do
      case "$target" in
        /usr/local/sbin/project-paper-observability-container|/etc/project-paper-observability/mini-production/mini-production.env|/etc/cron.d/project-paper-observability-mini-production|/var/lib/project-paper-observability/mini-production-export/INSTALL-RECEIPT) : ;;
        *) echo 'rollback manifest contains an unmanaged target' >&2; exit 75 ;;
      esac
      if [[ "$present" = 1 ]]; then
        [[ -f "$previous" ]] || { echo 'required rollback copy missing' >&2; exit 75; }
        [[ "$(sha256sum "$previous" | cut -d' ' -f1)" = "$digest" ]] || { echo 'rollback copy checksum mismatch' >&2; exit 75; }
      elif [[ "$present" != 0 ]]; then
        echo 'rollback manifest existence flag invalid' >&2; exit 75
      fi
    done < "$manifest"
    while IFS='|' read -r target previous present digest mode uid gid; do
      if [[ "$present" = 1 ]]; then
        cp -p "$previous" "$target"; chmod "$mode" "$target"; chown "$uid:$gid" "$target"
      else
        rm -f "$target"
      fi
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
