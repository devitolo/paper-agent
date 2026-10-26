#!/usr/bin/env bash
# One-time explicit Mini bootstrap; updates the existing cron, never adds a second.
set -Eeuo pipefail
[[ $EUID == 0 && $# == 0 ]] || { echo 'run with sudo; no arguments allowed' >&2; exit 64; }
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[[ ! -L "$source_dir/collect-publish.sh" && ! -L "$source_dir/project-paper-observability-deploy" ]] || exit 75
printf '%s  %s\n' effd533529bb0f0db5fadfd212a88079ac36c43b08f068861c1bba68772ff20d "$source_dir/collect-publish.sh" fcb7b7c60a45f5b64b4294468c6b667a8e571612bbdf209d2109e8ce447ff0db "$source_dir/project-paper-observability-deploy" | sha256sum --check --status
for path in /etc/cron.d /usr/local/sbin /etc/project-paper-observability/mini-production /var/lib/project-paper-observability; do
  [[ -d $path && ! -L $path && $(stat -c %u "$path") == 0 ]] || exit 75
  mode=$(stat -c %a "$path"); [[ $((8#$mode & 022)) == 0 ]] || exit 75
done
key=/etc/project-paper-observability/mini-production/transport.key
[[ -f $key && ! -L $key && $(stat -c %u "$key") == 0 && $(stat -c %g "$key") == 10002 && $(stat -c %a "$key") == 440 ]] || { echo 'dedicated key bootstrap missing' >&2; exit 66; }
state=/var/lib/project-paper-observability/mini-production-publisher
[[ -d $state && ! -L $state && $(stat -c %u "$state") == 10002 ]] || exit 66
image=ghcr.io/devitolo/paper-agent/observability-publisher@sha256:7fd64eaeaffce81f259f7fbe6abd61022fe359e9503e89fd57a936885bbc8055
docker image inspect "$image" >/dev/null
exec 9>/run/lock/project-paper-observability-deploy.lock
flock -n 9 || { echo 'deployment active; retry later' >&2; exit 75; }
exec 8>/run/lock/project-paper-observability.lock
flock -n 8 || { echo 'collection active; retry after it finishes' >&2; exit 75; }
cron=/etc/cron.d/project-paper-observability-mini-production
helper=/usr/local/sbin/project-paper-observability-deploy
wrapper=/usr/local/sbin/project-paper-observability-collect-publish
config=/etc/project-paper-observability/mini-production/publisher.env
[[ -f $cron && ! -L $cron ]] || exit 66
control=/var/lib/project-paper-observability/mini-production-control
[[ -d $control && ! -L $control && $(stat -c %u "$control") == 0 ]] || exit 75
release=$(mktemp -d "$control/collect-publish-XXXXXXXX")
chmod 0700 "$release"
paths=("$cron" "$helper" "$wrapper" "$config")
for index in "${!paths[@]}"; do
  path=${paths[$index]}; [[ ! -L $path ]] || exit 75
  [[ ! -e $path ]] || cp -p --no-dereference "$path" "$release/$index.before"
done
# A rollback is made active only once all prior files have been preserved.
# Use Python to write the rollback without shell-quote ambiguity.
python3 - "$release" "${paths[@]}" <<'PY'
import pathlib,shlex,sys
release=pathlib.Path(sys.argv[1]);lines=['#!/usr/bin/env bash','set -euo pipefail']
for index,path in enumerate(sys.argv[2:]):
    previous=release/(str(index)+'.before')
    if previous.exists():lines.append('cp -p --no-dereference '+shlex.quote(str(previous))+' '+shlex.quote(path))
    else:lines.append('rm -f -- '+shlex.quote(path))
(release/'rollback.sh').write_text('\n'.join(lines)+'\n')
PY
chmod 0700 "$release/rollback.sh"
rollback() { rc=$?; trap - ERR; bash "$release/rollback.sh"; echo 'installation failed; prior cron restored' >&2; exit "$rc"; }
trap rollback ERR
python3 - "$cron" "$release/cron.new" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1]);s=p.read_text();old='/usr/local/sbin/project-paper-observability-container';new='/usr/local/sbin/project-paper-observability-collect-publish'
assert s.count(old)==1 or (s.count(old)==0 and s.count(new)==1), 'unexpected cron; refusing overwrite'
assert s.count('*/15 * * * * root ')==1, 'unexpected cadence'
pathlib.Path(sys.argv[2]).write_text(s.replace(old,new))
PY
cat > "$release/publisher.env" <<CONFIG
PAPER_OBSERVABILITY_PUBLISH_ENABLED=1
PROJECT_PAPER_PUBLISHER_IMAGE=$image
PROJECT_PAPER_PUBLISHER_URL=https://paper-prod.tail5fc461.ts.net/observability/v1/mini-status
PROJECT_PAPER_PUBLISHER_DNS=100.100.100.100
CONFIG
install -o root -g root -m 0755 "$source_dir/collect-publish.sh" "$wrapper"
install -o root -g root -m 0755 "$source_dir/project-paper-observability-deploy" "$helper"
install -o root -g root -m 0640 "$release/publisher.env" "$config"
install -o root -g root -m 0644 "$release/cron.new" "$cron.new"
mv -f "$cron.new" "$cron"
trap - ERR
printf 'collect_then_publish_enabled=yes cadence=15_minutes rollback=%s/rollback.sh\n' "$release"
