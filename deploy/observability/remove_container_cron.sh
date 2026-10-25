#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'must run as root' >&2; exit 77; }
exec 9>/run/lock/project-paper-observability.lock
/usr/bin/flock -n 9 || { echo 'observability removal already running' >&2; exit 75; }
rm -f /etc/cron.d/project-paper-observability-mini-production /usr/local/sbin/project-paper-observability-container
echo 'container observability cron removed'
