#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'must run as root' >&2; exit 77; }
package_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
arg=${1:-}
target=${arg#--target=}
[[ "$arg" == --target=* ]] || target=${PROJECT_PAPER_OBSERVABILITY_TARGET:-mini-production}
case "$target" in aws-production|mini-production) ;; *) echo 'unsupported target' >&2; exit 64 ;; esac
export PROJECT_PAPER_OBSERVABILITY_TARGET="$target"
control=/var/lib/project-paper-observability/${target}-control
backup_file="$control/CURRENT-BACKUP"
pending="$control/PENDING-BACKUP"
source "$package_root/deploy/observability/recovery-common.sh"
exec 9>/run/lock/project-paper-observability.lock
/usr/bin/flock -n 9 || { echo 'observability removal already running' >&2; exit 75; }
if [[ -e "$pending" ]]; then
  recover_pending
  echo 'pending failed installation restored'
elif [[ -e "$control/RECOVERED-PENDING" ]]; then
  completed=$(read_pointer "$control/RECOVERED-PENDING")
  validate_transaction "$completed"
  [[ -f "$completed/manifest.restored" ]] || { echo 'recovery completion evidence missing' >&2; exit 75; }
  echo 'failed-install recovery already completed; preserving previous installation'
elif [[ -e "$backup_file" ]]; then
  backup=$(read_pointer "$backup_file")
  # manifest.restored is separate; immutable inputs survive interrupted cleanup.
  restore_files "$backup"
  rm -f "$backup_file"
  echo 'container observability installation restored'
else
  echo 'container observability installation already absent; nothing to restore'
fi
