#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'must run as root' >&2; exit 77; }
[[ ${1:-} == --apply ]] || { echo 'usage: install_container_cron.sh --apply --target=aws-production' >&2; exit 64; }
target=${2#--target=}
[[ "$2" == --target=* ]] || { echo 'target is required' >&2; exit 64; }
case "$target" in aws-production|mini-production) ;; *) echo 'unsupported target' >&2; exit 64 ;; esac
export PROJECT_PAPER_OBSERVABILITY_TARGET="$target"
[[ -n ${PROJECT_PAPER_OBSERVABILITY_IMAGE:-} ]] || { echo 'PROJECT_PAPER_OBSERVABILITY_IMAGE must be an immutable digest' >&2; exit 64; }
case "$PROJECT_PAPER_OBSERVABILITY_IMAGE" in *[!A-Za-z0-9_./:@-]*) echo 'PROJECT_PAPER_OBSERVABILITY_IMAGE must be an immutable digest' >&2; exit 64 ;; esac
printf '%s' "$PROJECT_PAPER_OBSERVABILITY_IMAGE" | /usr/bin/grep -Eq '^[^@[:space:]]+@sha256:[0-9a-f]{64}$' || { echo 'PROJECT_PAPER_OBSERVABILITY_IMAGE must be an immutable digest' >&2; exit 64; }

package_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
config_dir=/etc/project-paper-observability/$target
state=/var/lib/project-paper-observability/$target
export_dir=/var/lib/project-paper-observability/${target}-export
control=/var/lib/project-paper-observability/${target}-control
env_path=$config_dir/$target.env
cron_path=/etc/cron.d/project-paper-observability-$target
backup_file="$control/CURRENT-BACKUP"
pending="$control/PENDING-BACKUP"
source "$package_root/deploy/observability/recovery-common.sh"
exec 9>"/run/lock/project-paper-observability.lock"
/usr/bin/flock -n 9 || { echo 'observability installation already running' >&2; exit 75; }
[[ ! -L "$control" ]] || { echo "symlink control refused" >&2; exit 75; }
install -d -o root -g root -m 0755 "$config_dir"
install -d -o 10001 -g 10001 -m 0755 "$export_dir"
install -d -o 10001 -g 10001 -m 0700 "$state"
install -d -o root -g root -m 0700 "$control"

[[ ! -e "$pending" ]] || { echo "pending recovery exists; run remove_container_cron.sh" >&2; exit 75; }
[[ $(stat -c %d "$export_dir") == $(stat -c %d "$control") ]] || { echo "receipt filesystem differs from control" >&2; exit 75; }
backup=$(mktemp -d "$control/previous-install-XXXXXX")
manifest="$backup/manifest"
for managed in /usr/local/sbin/project-paper-observability-container "$env_path" "$cron_path" "$export_dir/INSTALL-RECEIPT"; do
  previous="$backup/$(basename "$managed").previous"
  [[ ! -L "$managed" ]] || { echo "managed target is a symlink: $managed" >&2; exit 75; }
  if [ -e "$managed" ]; then
    cp -p --no-dereference "$managed" "$previous"
    printf '%s|%s|1|%s|%s|%s|%s\n' "$managed" "$previous" "$(sha256sum "$previous" | cut -d' ' -f1)" "$(stat -c %a "$previous")" "$(stat -c %u "$previous")" "$(stat -c %g "$previous")" >> "$manifest"
  else
    printf '%s|%s|0|-|-|-|-\n' "$managed" "$previous" >> "$manifest"
  fi
done
chmod 0600 "$manifest"
validate_transaction "$backup"
if [[ -e "$backup_file" ]]; then
  previous_current=$(read_pointer "$backup_file")
  validate_transaction "$previous_current"
  printf '%s\n' "$previous_current" > "$backup/previous-current"
  chmod 0600 "$backup/previous-current"
fi
write_pointer "$pending" "$backup"
restore_on_error() {
  local failure=$?
  trap - ERR
  if recover_pending; then
    echo 'failed installation restored previous state' >&2
  else
    echo "recovery incomplete; evidence retained at $backup; retry remove_container_cron.sh" >&2
  fi
  exit "$failure"
}
trap restore_on_error ERR

install -o root -g root -m 0755 "$package_root/deploy/observability/run-container.sh" /usr/local/sbin/project-paper-observability-container.new
cat > "$env_path.new" <<EOF
PROJECT_PAPER_OBSERVABILITY_IMAGE=$PROJECT_PAPER_OBSERVABILITY_IMAGE
PROJECT_PAPER_RESOURCE_ROOT=$state
PROJECT_PAPER_RESOURCE_EXPORT_DIR=$export_dir
PROJECT_PAPER_RESOURCE_ENVIRONMENT=$target
PROJECT_PAPER_RESOURCE_HOST=${PROJECT_PAPER_RESOURCE_HOST:-$target}
PROJECT_PAPER_RESOURCE_ENV_FILE=$env_path
EOF
chown root:root "$env_path.new"; chmod 0640 "$env_path.new"
cat > "$cron_path.new" <<EOF
*/15 * * * * root . $env_path; export PROJECT_PAPER_OBSERVABILITY_IMAGE PROJECT_PAPER_RESOURCE_ROOT PROJECT_PAPER_RESOURCE_EXPORT_DIR PROJECT_PAPER_RESOURCE_ENVIRONMENT PROJECT_PAPER_RESOURCE_HOST PROJECT_PAPER_RESOURCE_ENV_FILE; /usr/bin/flock -n /run/lock/project-paper-observability.lock /usr/local/sbin/project-paper-observability-container >>$state/collector.log 2>&1
EOF
chown root:root "$cron_path.new"; chmod 0644 "$cron_path.new"
mv -f /usr/local/sbin/project-paper-observability-container.new /usr/local/sbin/project-paper-observability-container
mv -f "$env_path.new" "$env_path"
mv -f "$cron_path.new" "$cron_path"
printf 'target=%s\nimage=%s\nconfig=%s\n' "$target" "$PROJECT_PAPER_OBSERVABILITY_IMAGE" "$env_path" > "$control/INSTALL-RECEIPT.new"
chown root:root "$control/INSTALL-RECEIPT.new"; chmod 0644 "$control/INSTALL-RECEIPT.new"
mv -f -T "$control/INSTALL-RECEIPT.new" "$export_dir/INSTALL-RECEIPT"
write_pointer "$backup_file" "$backup"
rm -f "$control/RECOVERED-PENDING"
rm -f "$pending"
trap - ERR
echo 'container observability cron installed'
