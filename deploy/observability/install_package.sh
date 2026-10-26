#!/usr/bin/env bash
# Root package entrypoint. Never accepts runner-supplied shell or changes sudoers.
set -Eeuo pipefail
[[ $EUID == 0 && $# == 2 && $1 == --target ]] || { echo 'usage: install_package.sh --target mini-production|aws-production' >&2; exit 64; }
case "$2" in
  mini-production) ;;
  aws-production) echo 'AWS transport publication is not applicable' >&2; exit 64 ;;
  *) exit 64 ;;
esac
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# Existing key/state pairing is preserved. Installer checks all prerequisites,
# locks deployment/collection and backs up cron/helper/config before mutation.
exec bash "$source_dir/install_collect_publish.sh"
