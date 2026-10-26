#!/usr/bin/env bash
# Reviewed one-time enablement for the existing AWS serving service only.
set -Eeuo pipefail
[[ $EUID == 0 && $# == 1 && $1 == --apply ]] || { echo 'usage: sudo bash observability_transport_aws_enable.sh --apply' >&2; exit 64; }
project=/home/ubuntu/project-paper-prod
key=/etc/project-paper-observability/aws-production/mini-transport.key
state=/var/lib/project-paper-observability-ingest
[[ -f $key && ! -L $key && -f $project/compose.yml && -f $project/.env ]] || { echo 'prerequisite missing' >&2; exit 66; }
exec 9>"$project/deploy.lock"
flock -n 9 || { echo 'another deployment is active' >&2; exit 75; }
python3 - "$key" <<'PY'
import pathlib,re,stat,sys
p=pathlib.Path(sys.argv[1]);s=p.stat()
assert s.st_uid==0 and s.st_gid==10001 and stat.S_IMODE(s.st_mode)==0o440
assert re.fullmatch(b'[0-9a-f]{64}\n?',p.read_bytes())
PY
[[ ! -L $state ]] || { echo 'symlink state refused' >&2; exit 75; }
install -d -o 10001 -g 10001 -m 0750 "$state"
release=$(mktemp -d "$project/production-releases/observability-transport-XXXXXXXX")
chmod 0700 "$release"
cp -p "$project/compose.yml" "$release/compose.before"
cp -p "$project/.env" "$release/env.before"
docker inspect --format '{{.Config.Image}}' project-paper-prod-app-1 > "$release/image.before"
# Consistent backup stays on the existing AWS backup volume; never transmitted.
backup_name="$(basename "$release").db"
docker exec project-paper-prod-app-1 python3 -c 'import sqlite3,sys; src=sqlite3.connect("file:/app/data/paper_agent.db?mode=ro",uri=True); dst=sqlite3.connect("/backups/"+sys.argv[1]); src.backup(dst); dst.close(); src.close()' "$backup_name"
printf 'database_backup=/backups/%s\n' "$backup_name" > "$release/backup-evidence"
cat > "$release/rollback.sh" <<ROLLBACK
#!/usr/bin/env bash
set -euo pipefail
cp -p '$release/compose.before' '$project/compose.yml'
cp -p '$release/env.before' '$project/.env'
docker compose --project-directory '$project' --env-file '$project/.env' -f '$project/compose.yml' up -d --no-build --pull never --no-deps app
ROLLBACK
chmod 0700 "$release/rollback.sh"
rollback() { rc=$?; trap - ERR; bash "$release/rollback.sh"; printf 'rollback=attempted\n' > "$release/result"; exit "$rc"; }
trap rollback ERR
python3 - "$project/compose.yml" "$state" "$key" <<'PY'
import copy,os,pathlib,sys,tempfile,yaml
p=pathlib.Path(sys.argv[1]);data=yaml.safe_load(p.read_text());app=data['services']['app'];before=copy.deepcopy(app.get('volumes',[]))
env=app.setdefault('environment',{})
if isinstance(env,list):
    env=dict(item.split('=',1) if '=' in item else (item,None) for item in env);app['environment']=env
env.update(PAPER_OBSERVABILITY_INGEST_ENABLED='1',PAPER_OBSERVABILITY_INGEST_KEY_FILE='/run/secrets/paper_observability_transport_key',PAPER_OBSERVABILITY_STATE_FILE='/var/lib/project-paper-observability-ingest/mini-state.json')
for mount in ({'type':'bind','source':sys.argv[2],'target':'/var/lib/project-paper-observability-ingest'}, {'type':'bind','source':sys.argv[3],'target':'/run/secrets/paper_observability_transport_key','read_only':True}):
    if any(isinstance(v,dict) and v.get('target')==mount['target'] for v in before):raise ValueError('target already present; review instead of overwrite')
    app.setdefault('volumes',[]).append(mount)
assert app['volumes'][:len(before)]==before
fd,name=tempfile.mkstemp(dir=p.parent,prefix='.transport-compose-')
try:
    with os.fdopen(fd,'w') as stream:yaml.safe_dump(data,stream,sort_keys=False);stream.flush();os.fsync(stream.fileno())
    os.chmod(name,0o600);os.replace(name,p)
finally:
    if os.path.exists(name):os.unlink(name)
PY
docker compose --project-directory "$project" --env-file "$project/.env" -f "$project/compose.yml" config --quiet
docker compose --project-directory "$project" --env-file "$project/.env" -f "$project/compose.yml" up -d --no-build --pull never --no-deps app >/dev/null
for attempt in $(seq 1 60); do
  if [[ $(docker inspect --format '{{.State.Health.Status}}' project-paper-prod-app-1) == healthy ]] && curl -fsS --max-time 5 http://127.0.0.1:8000/ready >/dev/null; then
    status=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8000/observability/v1/mini-status)
    [[ $status == 400 || $status == 401 ]] || { echo 'unexpected enabled response' >&2; false; }
    printf 'ingest=enabled\nhealth=healthy\nrollback=not-needed\n' > "$release/result"
    trap - ERR
    printf 'ingest_enabled=yes rollback_directory=%s\n' "$release"
    exit 0
  fi
  sleep 1
done
false
