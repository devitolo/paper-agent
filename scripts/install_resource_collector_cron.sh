#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo 'must run as root' >&2; exit 77
fi
if [[ ${1:-} != --apply || ${2:-} != --target=mini-production ]]; then
  echo 'usage: install_resource_collector_cron.sh --apply --target=mini-production' >&2; exit 64
fi

package_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
prefix=/opt/project-paper-observability/mini-production
config_dir=/etc/project-paper-observability/mini-production
state=/var/lib/project-paper-observability/mini-production
backup="$state/previous-install"
install -d -o root -g root -m 0755 "$prefix" "$config_dir"
install -d -o root -g root -m 0700 "$state"
install -d -o root -g root -m 0755 "$state/export"
install -d -o root -g root -m 0700 "$backup"
targets=("$prefix/host_resource_collector.py" "$config_dir/mini-production.env" /usr/local/sbin/project-paper-resource-collector /etc/cron.d/project-paper-resource-collector)
backups=("$backup/host_resource_collector.py.previous" "$backup/mini-production.env.previous" "$backup/collector-wrapper.previous" "$backup/collector-cron.previous")
for i in "${!targets[@]}"; do
  if [[ -e "${targets[$i]}" && ! -e "${backups[$i]}" ]]; then cp -p "${targets[$i]}" "${backups[$i]}"; fi
done
install -o root -g root -m 0755 "$package_root/scripts/host_resource_collector.py" "$prefix/host_resource_collector.py.new"
mv -f "$prefix/host_resource_collector.py.new" "$prefix/host_resource_collector.py"
install -o root -g root -m 0755 "$package_root/scripts/resource_collector_job.sh" /usr/local/sbin/project-paper-resource-collector.new
mv -f /usr/local/sbin/project-paper-resource-collector.new /usr/local/sbin/project-paper-resource-collector
cat > "$config_dir/mini-production.env.new" <<'EOF'
PROJECT_PAPER_RESOURCE_ROOT=/var/lib/project-paper-observability/mini-production
PROJECT_PAPER_RESOURCE_COLLECTOR=/opt/project-paper-observability/mini-production/host_resource_collector.py
PROJECT_PAPER_RESOURCE_EXPORT_DIR=/var/lib/project-paper-observability/mini-production/export
PROJECT_PAPER_RESOURCE_HOST=clarkmini
PROJECT_PAPER_RESOURCE_ENVIRONMENT=mini-production
PROJECT_PAPER_CONTAINER_NAMES=paper-mini-production-app-1,paper-minilm-production-minilm-1,paper-mini-production-ollama-1
EOF
chown root:root "$config_dir/mini-production.env.new"; chmod 0640 "$config_dir/mini-production.env.new"; mv -f "$config_dir/mini-production.env.new" "$config_dir/mini-production.env"
cat > /etc/cron.d/project-paper-resource-collector.new <<'EOF'
* * * * * root . /etc/project-paper-observability/mini-production/mini-production.env; export PROJECT_PAPER_RESOURCE_ROOT PROJECT_PAPER_RESOURCE_COLLECTOR PROJECT_PAPER_RESOURCE_EXPORT_DIR PROJECT_PAPER_RESOURCE_HOST PROJECT_PAPER_RESOURCE_ENVIRONMENT PROJECT_PAPER_CONTAINER_NAMES; /usr/local/sbin/project-paper-resource-collector
EOF
chown root:root /etc/cron.d/project-paper-resource-collector.new; chmod 0644 /etc/cron.d/project-paper-resource-collector.new; mv -f /etc/cron.d/project-paper-resource-collector.new /etc/cron.d/project-paper-resource-collector
printf '%s\n' "${targets[0]}|${backups[0]}" "${targets[1]}|${backups[1]}" "/usr/local/sbin/project-paper-resource-collector|${backups[2]}" "/etc/cron.d/project-paper-resource-collector|${backups[3]}" > "$state/INSTALL-MANIFEST"
if [[ -n ${PROJECT_PAPER_OBSERVABILITY_RELEASE:-} ]]; then
  printf 'release=%s\n' "$PROJECT_PAPER_OBSERVABILITY_RELEASE" > "$state/RELEASE"
  chown root:root "$state/RELEASE"; chmod 0640 "$state/RELEASE"
fi
echo 'installed; add a read-only UI mount separately, then verify samples before enabling production display'
