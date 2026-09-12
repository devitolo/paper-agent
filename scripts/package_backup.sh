#!/usr/bin/env bash
# Offline full-state backup or restore into a fresh Compose project.
set -euo pipefail
umask 077
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$root"
action=${1:-}
archive=${2:-}
[[ "$action" == backup || "$action" == restore ]] || { echo 'Usage: bash scripts/package_backup.sh backup|restore /absolute/path/archive.tar.gz'; exit 1; }
[[ "$archive" == /* && -f .env ]] || { echo 'Provide an absolute archive path and configure .env first'; exit 1; }
# Require an explicit identity, never fall back to Compose directory defaults.
if [[ -f .paper-install ]]; then
  project=$(sed -n 's/^project=//p' .paper-install)
else
  [[ "$action" == restore ]] || { echo 'Backup requires an installed package'; exit 1; }
  project=${PAPER_RESTORE_PROJECT:-}
fi
[[ "$project" =~ ^paper-[a-z0-9-]+$ ]] || { echo 'Set PAPER_RESTORE_PROJECT to a new paper-... project name for a fresh restore'; exit 1; }
mkdir .paper-install.lock || { echo 'Installer or backup operation is already active'; exit 1; }
trap 'rmdir .paper-install.lock' EXIT
compose() { docker compose --project-name "$project" --env-file "$root/.env" -f "$root/docker-compose.yml" "$@"; }
# Fail rather than interrupt an active customer job.
[[ -z "$(compose ps --status running -q app)" ]] || { echo "Stop app first: docker compose -p $project stop app"; exit 1; }
compose create --no-build --no-recreate app >/dev/null
container=$(compose ps -aq app)
[[ -n "$container" ]] || exit 1
image=$(docker inspect --format '{{.Config.Image}}' "$container")
[[ "$image" =~ @sha256:[a-f0-9]{64}$ ]] || { echo 'Backup/restore requires an immutable release image digest'; exit 1; }
# Refuse any running container sharing either authoritative volume.
for volume in $(docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' "$container"); do
  [[ -z "$(docker ps -q --filter "volume=$volume")" ]] || { echo 'A container still uses the state volume; stop it first'; exit 1; }
done
directory=$(dirname "$archive")
[[ -d "$directory" ]] || { echo 'Create the private backup directory first'; exit 1; }
# The selected app image supplies Python; no host Python installation is needed.
docker run --rm -i --network none --user 0:0 --volumes-from "$container" \
  --mount "type=bind,src=$directory,dst=/backup" --entrypoint python "$image" - "$action" "$(basename "$archive")" "$image" < scripts/package_backup/archive.py
if [[ "$action" == restore && ! -f .paper-install ]]; then
  platform=$(docker inspect --format '{{.Os}}/{{.Architecture}}' "$image")
  ollama=$(awk '/^PAPER_OLLAMA_IMAGE=/ {v=substr($0,20)} END {print v}' .env)
  version=$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$image")
  printf 'project=%s\ndirectory=%s\nplatform=%s\nmode=release\napp_image=%s\nollama_image=%s\nrelease_version=%s\n' "$project" "$root" "$platform" "$image" "$ollama" "$version" > .paper-install
  echo "Restore completed into project $project. Run the installer to prepare Qwen and verify readiness."
  echo "Run: bash scripts/install_project_paper.sh"
else
  echo "Start with: docker compose -p $project up -d app ollama"
fi
