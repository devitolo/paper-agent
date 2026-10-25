#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo 'must run as root' >&2; exit 77
fi
if [[ ${1:-} != --apply ]]; then
  echo 'usage: install_resource_collector_cron.sh --apply' >&2; exit 64
fi

prefix=/opt/project-paper-observability
config_dir=/etc/project-paper-observability
state=/var/lib/project-paper-observability/mini-production
install -d -o root -g root -m 0755 "$prefix" "$config_dir" "$state"
install -o root -g root -m 0755 scripts/host_resource_collector.py "$prefix/host_resource_collector.py"
install -o root -g root -m 0755 scripts/resource_collector_job.sh /usr/local/sbin/project-paper-resource-collector
cat > "$config_dir/mini-production.env" <<'EOF'
PROJECT_PAPER_RESOURCE_ROOT=/var/lib/project-paper-observability/mini-production
PROJECT_PAPER_RESOURCE_COLLECTOR=/opt/project-paper-observability/host_resource_collector.py
PROJECT_PAPER_RESOURCE_HOST=clarkmini
PROJECT_PAPER_RESOURCE_ENVIRONMENT=mini-production
PROJECT_PAPER_CONTAINER_NAMES=paper-mini-production-app-1,paper-minilm-production-minilm-1,paper-mini-production-ollama-1
EOF
chown root:root "$config_dir/mini-production.env"; chmod 0644 "$config_dir/mini-production.env"
cat > /etc/cron.d/project-paper-resource-collector <<'EOF'
* * * * * root . /etc/project-paper-observability/mini-production.env; export PROJECT_PAPER_RESOURCE_ROOT PROJECT_PAPER_RESOURCE_COLLECTOR PROJECT_PAPER_RESOURCE_HOST PROJECT_PAPER_RESOURCE_ENVIRONMENT PROJECT_PAPER_CONTAINER_NAMES; /usr/local/sbin/project-paper-resource-collector >>/var/lib/project-paper-observability/mini-production/collector.log 2>&1
EOF
chown root:root /etc/cron.d/project-paper-resource-collector; chmod 0644 /etc/cron.d/project-paper-resource-collector
echo 'installed; add a read-only UI mount separately, then verify samples before enabling production display'
