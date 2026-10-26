#!/usr/bin/env bash
# One-time, explicit operator bootstrap. Does not activate publisher or change cron.
set -euo pipefail
[[ $EUID == 0 && $# == 0 ]] || { echo 'run with sudo; no arguments allowed' >&2; exit 64; }
config=/etc/project-paper-observability/mini-production
state=/var/lib/project-paper-observability/mini-production-publisher
for path in /etc/project-paper-observability "$config" /var/lib/project-paper-observability; do
  [[ -d $path && ! -L $path && $(stat -c %u "$path") == 0 ]] || { echo 'untrusted parent' >&2; exit 75; }
  mode=$(stat -c %a "$path")
  [[ $((8#$mode & 022)) == 0 ]] || { echo 'writable parent' >&2; exit 75; }
done
[[ ! -L $state && ! -L $config/transport.key ]] || { echo 'symlink refused' >&2; exit 75; }
install -d -o 10002 -g 10002 -m 0700 "$state"
if [[ ! -e $config/transport.key ]]; then
  umask 077
  temp=$(mktemp "$config/.transport-key.XXXXXX")
  trap 'rm -f "$temp"' EXIT
  python3 -c 'import secrets;print(secrets.token_hex(32))' > "$temp"
  chown root:10002 "$temp"; chmod 0440 "$temp"
  mv -T "$temp" "$config/transport.key"
fi
python3 - "$config/transport.key" <<'PY'
import os,pathlib,re,stat,sys
p=pathlib.Path(sys.argv[1]); s=p.stat()
assert s.st_uid == 0 and s.st_gid == 10002 and stat.S_IMODE(s.st_mode) == 0o440
assert re.fullmatch(b'[0-9a-f]{64}\n?',p.read_bytes())
PY
echo 'transport_key_ready=yes publisher_state_ready=yes activated=no'
