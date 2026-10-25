#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then echo 'must run as root' >&2; exit 77; fi
state=/var/lib/project-paper-observability/mini-production
backup="$state/previous-install"
rm -f /etc/cron.d/project-paper-resource-collector /usr/local/sbin/project-paper-resource-collector
rm -f /opt/project-paper-observability/mini-production/host_resource_collector.py
rm -f /etc/project-paper-observability/mini-production/mini-production.env
for pair in \
  "/opt/project-paper-observability/mini-production/host_resource_collector.py:$backup/host_resource_collector.py.previous" \
  "/etc/project-paper-observability/mini-production/mini-production.env:$backup/mini-production.env.previous"; do
  target=${pair%%:*}; previous=${pair#*:}; [[ -f "$previous" ]] && mv -f "$previous" "$target"
done
echo 'owned collector files and cron removed/restored; retained state at /var/lib/project-paper-observability'
