#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then echo 'must run as root' >&2; exit 77; fi
rm -f /etc/cron.d/project-paper-resource-collector
rm -f /usr/local/sbin/project-paper-resource-collector
rm -rf /opt/project-paper-observability /etc/project-paper-observability
echo 'collector code and cron removed; retained state at /var/lib/project-paper-observability'
