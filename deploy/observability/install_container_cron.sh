#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'must run as root' >&2; exit 77; }
[[ ${1:-} == --apply && ${2:-} == --target=mini-production ]] || { echo 'usage: install_container_cron.sh --apply --target=mini-production' >&2; exit 64; }
[[ -n ${PROJECT_PAPER_OBSERVABILITY_IMAGE:-} && "$PROJECT_PAPER_OBSERVABILITY_IMAGE" == *@sha256:* ]] || { echo 'PROJECT_PAPER_OBSERVABILITY_IMAGE must be an immutable digest' >&2; exit 64; }

package_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
config_dir=/etc/project-paper-observability/mini-production
state=/var/lib/project-paper-observability/mini-production
export_dir=/var/lib/project-paper-observability/mini-production-export
install -d -o root -g root -m 0755 "$config_dir" "$export_dir"
install -d -o root -g root -m 0700 "$state"
exec 9>"/run/lock/project-paper-observability.lock"
/usr/bin/flock -n 9 || { echo 'observability installation already running' >&2; exit 75; }

install -o root -g root -m 0755 "$package_root/deploy/observability/run-container.sh" /usr/local/sbin/project-paper-observability-container.new
cat > "$config_dir/mini-production.env.new" <<EOF
PROJECT_PAPER_OBSERVABILITY_IMAGE=$PROJECT_PAPER_OBSERVABILITY_IMAGE
PROJECT_PAPER_RESOURCE_ROOT=$state
PROJECT_PAPER_RESOURCE_EXPORT_DIR=$export_dir
PROJECT_PAPER_RESOURCE_ENVIRONMENT=mini-production
PROJECT_PAPER_RESOURCE_HOST=clarkmini
EOF
chown root:root "$config_dir/mini-production.env.new"; chmod 0640 "$config_dir/mini-production.env.new"
cat > /etc/cron.d/project-paper-observability-mini-production.new <<'EOF'
* * * * * root . /etc/project-paper-observability/mini-production/mini-production.env; export PROJECT_PAPER_OBSERVABILITY_IMAGE PROJECT_PAPER_RESOURCE_ROOT PROJECT_PAPER_RESOURCE_EXPORT_DIR PROJECT_PAPER_RESOURCE_ENVIRONMENT PROJECT_PAPER_RESOURCE_HOST; /usr/bin/flock -n /run/lock/project-paper-observability.lock /usr/local/sbin/project-paper-observability-container >>/var/lib/project-paper-observability/mini-production/collector.log 2>&1
EOF
chown root:root /etc/cron.d/project-paper-observability-mini-production.new; chmod 0644 /etc/cron.d/project-paper-observability-mini-production.new
mv -f /usr/local/sbin/project-paper-observability-container.new /usr/local/sbin/project-paper-observability-container
mv -f "$config_dir/mini-production.env.new" "$config_dir/mini-production.env"
mv -f /etc/cron.d/project-paper-observability-mini-production.new /etc/cron.d/project-paper-observability-mini-production
printf 'target=mini-production\nimage=%s\nconfig=%s\n' "$PROJECT_PAPER_OBSERVABILITY_IMAGE" "$config_dir/mini-production.env" > "$export_dir/INSTALL-RECEIPT"
chown root:root "$export_dir/INSTALL-RECEIPT"; chmod 0644 "$export_dir/INSTALL-RECEIPT"
echo 'container observability cron installed'
