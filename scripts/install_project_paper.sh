#!/usr/bin/env bash
# M1.1 development installer. Never sources customer configuration or deletes volumes.
set -euo pipefail
umask 077
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$INSTALL_DIR"
fail() { echo "Project Paper install failed: $*" >&2; exit 1; }
build=0
requested_ollama=
requested_app=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build) build=1; shift ;;
    --ollama-image|--app-image)
      [[ $# -ge 2 ]] || fail "Missing image reference after $1"
      if [[ "$1" == --ollama-image ]]; then requested_ollama=$2; else requested_app=$2; fi
      shift 2 ;;
    *) fail "Usage: bash scripts/install_project_paper.sh [--build] [--ollama-image REFERENCE] [--app-image REFERENCE]" ;;
  esac
done
for requested in "$requested_ollama" "$requested_app"; do
  [[ -z "$requested" || "$requested" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*$ ]] || fail "Image references must be literal Docker references"
done
case "$(uname -s)/$(uname -m)" in
  Darwin/arm64) platform=linux/arm64 ;;
  Linux/x86_64) platform=linux/amd64 ;;
  *) fail "Initial package targets macOS Apple Silicon or Linux x86-64 only; this host is unqualified" ;;
esac
command -v docker >/dev/null || fail "Install and start Docker Desktop (Mac) or Docker Engine with Compose (Linux), then rerun"
daemon=$(docker info --format '{{.OSType}}/{{.Architecture}}' 2>/dev/null) || fail "Docker daemon is unavailable; start Docker and verify your existing context"
case "$daemon" in linux/x86_64) daemon=linux/amd64 ;; linux/aarch64) daemon=linux/arm64 ;; esac
[[ "$daemon" == "$platform" ]] || fail "Docker daemon platform does not match this host's supported container platform ($platform)"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
[[ -w "$INSTALL_DIR" ]] || fail "Installation directory is not writable"
mkdir .paper-install.lock 2>/dev/null || fail "Another installer is running, or an interrupted installer left .paper-install.lock; verify no installer is active before removing that empty directory"
cleanup() { rmdir "$INSTALL_DIR/.paper-install.lock" 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM
if [[ ! -e .env ]]; then
  cp .env.example .env
  # The template is trusted, but customer .env is never executed.
  [[ -z "$requested_ollama" ]] || printf '\nPAPER_OLLAMA_IMAGE=%s\n' "$requested_ollama" >> .env
  [[ -z "$requested_app" ]] || printf '\nPAPER_APP_IMAGE=%s\n' "$requested_app" >> .env
fi
chmod 600 .env
read_setting() {
  awk -v key="$1" 'index($0,key "=")==1 { value=substr($0,length(key)+2); sub(/\r$/, "", value) } END { print value }' .env
}
for key in PAPER_APP_IMAGE PAPER_OLLAMA_IMAGE PAPER_PORT PAPER_MODEL_PREPARE_TIMEOUT PAPER_MODEL_PULL_ATTEMPTS PAPER_MODEL_CHECK_TIMEOUT PAPER_MIN_MEMORY_MB PAPER_MIN_DISK_MB; do
  value=$(read_setting "$key")
  [[ -n "$value" ]] || fail "Set $key in .env to a literal value; optional provider keys are not needed"
  case "$key" in
    PAPER_APP_IMAGE|PAPER_OLLAMA_IMAGE)
      [[ "$value" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*$ ]] || fail "$key must be a literal Docker image reference" ;;
    *) [[ "$value" != *[!0-9]* && "$value" -gt 0 ]] || fail "$key must be a positive integer" ;;
  esac
  export "$key=$value"
done
[[ "$PAPER_PORT" -le 65535 ]] || fail "PAPER_PORT exceeds 65535"
[[ -z "$requested_ollama" || "$requested_ollama" == "$PAPER_OLLAMA_IMAGE" ]] || fail "Existing .env selects another Ollama image; installer preserves it"
[[ -z "$requested_app" || "$requested_app" == "$PAPER_APP_IMAGE" ]] || fail "Existing .env selects another app image; installer preserves it"
memory=$(docker info --format '{{.MemTotal}}')
[[ "$memory" -ge $((PAPER_MIN_MEMORY_MB * 1024 * 1024)) ]] || fail "Docker has less than the configured memory preflight threshold"
disk=$(df -Pm "$INSTALL_DIR" | awk 'NR==2 { print $4 }')
[[ "$disk" -ge "$PAPER_MIN_DISK_MB" ]] || fail "Installation filesystem has less than the configured free disk preflight threshold"
record=.paper-install
if [[ -f "$record" ]]; then
  recorded() { sed -n "s/^$1=//p" "$record"; }
  identity=$(recorded project)
  [[ "$identity" =~ ^paper-[a-z0-9-]+$ ]] || fail "Invalid saved installation identity; do not create replacement volumes"
  [[ "$(recorded directory)" == "$INSTALL_DIR" ]] || fail "Installation directory moved; explicit relocation is required to preserve volume identity"
  [[ "$(recorded platform)" == "$platform" ]] || fail "Saved installation platform differs; migration is separate explicit work"
  [[ "$(recorded app_image)" == "$PAPER_APP_IMAGE" && "$(recorded ollama_image)" == "$PAPER_OLLAMA_IMAGE" ]] || fail "Image selections changed; an installer rerun is not an upgrade"
  echo "Repairing existing installation $identity at its recorded image selection."
else
  identity="paper-$(date +%s)-$$"
fi
export COMPOSE_PROJECT_NAME="$identity"
compose() { docker compose --project-name "$identity" --project-directory "$INSTALL_DIR" --env-file "$INSTALL_DIR/.env" -f "$INSTALL_DIR/docker-compose.yml" "$@"; }
compose config --quiet || fail "Compose configuration is invalid; requires a compatible Compose v2"
# Never rebuild a previously selected local image implicitly on an ordinary rerun.
if ! docker image inspect "$PAPER_APP_IMAGE" >/dev/null 2>&1; then
  if [[ "$build" == 1 ]]; then compose build app || fail "Application image build failed";
  else compose pull app || fail "Selected app image unavailable; no public release is currently published. Use --build for local development"; fi
fi
docker image inspect "$PAPER_OLLAMA_IMAGE" >/dev/null 2>&1 || compose pull ollama || fail "Selected Ollama image could not be obtained"
for selected in "$PAPER_APP_IMAGE" "$PAPER_OLLAMA_IMAGE"; do
  actual=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$selected")
  [[ "$actual" == "$platform" ]] || fail "Selected image has an incompatible platform; emulation is not a supported fallback"
done
# Commit this invocation's identity only after image acquisition/validation, but
# before creating any services or volumes. Failed first pulls remain correctable.
if [[ ! -f "$record" ]]; then
  {
    printf 'project=%s\ndirectory=%s\nplatform=%s\n' "$identity" "$INSTALL_DIR" "$platform"
    printf 'app_image=%s\nollama_image=%s\n' "$PAPER_APP_IMAGE" "$PAPER_OLLAMA_IMAGE"
  } > "$record.tmp"
  mv "$record.tmp" "$record"
fi
# Compose reports occupied loopback ports without terminating another process.
compose up -d --no-build --pull never app ollama || fail "Service startup or loopback port binding failed; inspect Compose output (change PAPER_PORT if occupied)"
echo "Waiting for app/database readiness (up to 90 seconds)."
ready=0
for ((i=0; i<30; i++)); do
  if compose exec -T app python -m paper_agents.package_runtime check-app >/dev/null 2>&1; then ready=1; break; fi
  sleep 3
done
[[ "$ready" == 1 ]] || fail "App/database readiness failed; inspect: docker compose -p $identity logs app"
helper=$(compose ps -aq prepare-model)
if [[ -n "$helper" && "$(docker inspect --format '{{.State.Running}}' "$helper")" == true ]]; then
  echo "Waiting for the existing model preparation; no parallel pull is started."
else
  compose up -d --no-deps --force-recreate --pull never prepare-model || fail "Could not start model preparation"
  helper=$(compose ps -aq prepare-model)
fi
echo "Preparing the default model; progress: docker compose -p $identity logs -f prepare-model"
deadline=$((SECONDS + PAPER_MODEL_PREPARE_TIMEOUT + 15))
while [[ "$(docker inspect --format '{{.State.Running}}' "$helper")" == true ]]; do
  [[ "$SECONDS" -lt "$deadline" ]] || fail "Model preparation timed out; app and volumes retained. Inspect prepare-model logs and rerun installer"
  sleep 3
done
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$helper")" == 0 ]] || fail "Model preparation failed; inspect prepare-model logs and rerun installer to retry"
compose exec -T app python -m paper_agents.package_runtime check-model || fail "Bounded model inference failed; app remains available; inspect Ollama logs and rerun installer"
printf 'Project Paper package is ready: http://127.0.0.1:%s\n' "$PAPER_PORT"
echo "Open Topics, add an enabled arXiv topic, then choose Run Scout in the Review Queue."
echo "Status: http://127.0.0.1:$PAPER_PORT/runtime"
echo "Logs (from $INSTALL_DIR): docker compose -p $identity logs --tail 100 app ollama prepare-model"
echo "Restart/repair/retry: bash $INSTALL_DIR/scripts/install_project_paper.sh"
