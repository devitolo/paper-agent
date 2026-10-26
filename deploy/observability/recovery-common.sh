#!/usr/bin/env bash
# Host-only recovery helpers. Backups remain immutable until manually retired.
validate_transaction() {
  python3 - "$control" "$1" <<'PY'
import hashlib, os, pathlib, re, stat, sys
try:
    c = pathlib.Path(sys.argv[1])
    b = pathlib.Path(sys.argv[2])
    target = os.environ.get('PROJECT_PAPER_OBSERVABILITY_TARGET', 'mini-production')
    targets = {'/usr/local/sbin/project-paper-observability-container', f'/etc/project-paper-observability/{target}/{target}.env', f'/etc/cron.d/project-paper-observability-{target}', f'/var/lib/project-paper-observability/{target}-export/INSTALL-RECEIPT'}
    for p in (c, b):
        s = p.lstat()
        if not (stat.S_ISDIR(s.st_mode) and s.st_uid == 0 and (stat.S_IMODE(s.st_mode) == 0o700)):
            raise ValueError('untrusted recovery directory')
    if not (b.parent == c and b.name.startswith('previous-install-')):
        raise ValueError('backup outside control')
    m = b / 'manifest'
    s = m.lstat()
    if not (stat.S_ISREG(s.st_mode) and s.st_uid == 0 and (not s.st_mode & 0o022)):
        raise ValueError('untrusted manifest')
    seen = set()
    for line in m.read_text().splitlines():
        f = line.split('|')
        if not len(f) == 7:
            raise ValueError('malformed manifest row')
        t, p, e, h, mode, u, g = f
        if not (t in targets and t not in seen):
            raise ValueError('unmanaged or duplicate target')
        seen.add(t)
        if pathlib.Path(t).is_symlink():
            raise ValueError('managed target is a symlink')
        expected = b / (pathlib.Path(t).name + '.previous')
        if not pathlib.Path(p) == expected:
            raise ValueError('backup outside transaction')
        if e == '1':
            if not (re.fullmatch('[0-9a-f]{64}', h) and re.fullmatch('[0-7]{3,4}', mode) and u.isdecimal() and g.isdecimal()):
                raise ValueError('invalid metadata')
            s = expected.lstat()
            if not stat.S_ISREG(s.st_mode):
                raise ValueError('nonregular backup')
            if not hashlib.sha256(expected.read_bytes()).hexdigest() == h:
                raise ValueError('checksum mismatch')
            if not (stat.S_IMODE(s.st_mode), s.st_uid, s.st_gid) == (int(mode, 8), int(u), int(g)):
                raise ValueError('metadata mismatch')
        else:
            if not (e == '0' and (h, mode, u, g) == ('-', '-', '-', '-')):
                raise ValueError('invalid absence')
            if not (not expected.exists() and (not expected.is_symlink())):
                raise ValueError('unexpected backup')
    if not seen == targets:
        raise ValueError('incomplete manifest')
except (OSError, ValueError) as error:
    print('invalid rollback evidence: ' + str(error), file=sys.stderr)
    sys.exit(75)
PY
}
write_pointer() {
  local destination=$1 value=$2
  printf '%s\n' "$value" > "$destination.new" || return
  chown root:root "$destination.new" || return
  chmod 0600 "$destination.new" || return
  mv -f -T "$destination.new" "$destination" || return
}
read_pointer() {
  local path=$1
  [[ -f "$path" && ! -L "$path" && $(stat -c %u "$path") == 0 && $(stat -c %a "$path") == 600 ]] || { echo 'untrusted recovery pointer' >&2; return 75; }
  cat "$path"
}
restore_files() {
  local backup=$1 target previous present digest mode uid gid temporary parent
  validate_transaction "$backup" || return
  while IFS='|' read -r target previous present digest mode uid gid; do
    [[ ! -L "$target" ]] || { echo "managed target is a symlink: $target" >&2; return 75; }
  done < "$backup/manifest"
  while IFS='|' read -r target previous present digest mode uid gid; do
    if [[ "$present" == 1 ]]; then
      parent=$(dirname "$target")
      if [[ "$target" == /var/lib/project-paper-observability/${PROJECT_PAPER_OBSERVABILITY_TARGET:-mini-production}-export/INSTALL-RECEIPT ]]; then
        [[ $(stat -c %d "$parent") == $(stat -c %d "$control") ]] || { echo 'receipt and control filesystem differ' >&2; return 75; }
        temporary=$(mktemp "$control/restore-XXXXXX") || return
      else
        temporary=$(mktemp "$parent/.observability-restore-XXXXXX") || return
      fi
      cp -p --no-dereference "$previous" "$temporary" || return
      chmod "$mode" "$temporary" || return
      chown "$uid:$gid" "$temporary" || return
      mv -f -T "$temporary" "$target" || return
    else
      rm -f "$target" || return
    fi
  done < "$backup/manifest"
  printf 'restored\n' > "$backup/manifest.restored.new" || return
  chmod 0600 "$backup/manifest.restored.new" || return
  mv -f -T "$backup/manifest.restored.new" "$backup/manifest.restored" || return
}
recover_pending() {
  local recovery previous
  recovery=$(read_pointer "$pending") || return
  restore_files "$recovery" || return
  if [[ -f "$recovery/previous-current" ]]; then
    previous=$(cat "$recovery/previous-current") || return
    validate_transaction "$previous" || return
    write_pointer "$backup_file" "$previous" || return
  else
    rm -f "$backup_file" || return
  fi
  write_pointer "$control/RECOVERED-PENDING" "$recovery" || return
  rm -f "$pending" || return
}
