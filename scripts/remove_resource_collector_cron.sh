#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then echo 'must run as root' >&2; exit 77; fi
state=/var/lib/project-paper-observability/mini-production
exec 9>"$state/collector.lock"
/usr/bin/flock -n 9 || { echo 'collector is running; refusing rollback' >&2; exit 75; }
manifest="$state/INSTALL-MANIFEST"
[[ -f "$manifest" ]] || { echo 'owned manifest missing; refusing rollback' >&2; exit 66; }
while IFS='|' read -r target previous; do
  [[ -n "$target" ]] || continue
  rm -f "$target"
  if [[ -f "$previous" ]]; then mv -f "$previous" "$target"; fi
done < "$manifest"
echo 'owned collector files and cron removed/restored; retained state at /var/lib/project-paper-observability'
