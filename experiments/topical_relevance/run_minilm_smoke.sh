#!/usr/bin/env bash
# SDLC execution only, after independent QA and commit/push. Never pulls images.
set -euo pipefail
umask 077
if [[ $# != 2 ]]; then
  echo "Usage: bash experiments/topical_relevance/run_minilm_smoke.sh CHECKOUT EXPECTED_COMMIT" >&2
  exit 2
fi
checkout=$(realpath "$1")
expected_commit=$2
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]]
[[ $(git -C "$checkout" rev-parse HEAD) == "$expected_commit" ]]
git -C "$checkout" diff --quiet HEAD -- experiments
[[ -z $(git -C "$checkout" ls-files --others --exclude-standard -- experiments) ]]
# Require the entry point to belong to the verified commit.
git -C "$checkout" ls-files --error-unmatch experiments/topical_relevance/smoke_container.py >/dev/null
image_id=$(docker image inspect paper-agent-minilm-smoke:onnx-1.23.2 --format '{{.Id}}')
[[ "$image_id" == sha256:7d8b960220e3c6f60292e6d40a8f300ff19c5ee05cd97cf5f725a76673e2d5c2 ]]
docker volume inspect paper-agent-minilm-model-cache >/dev/null
mkdir -p "$HOME/paper-agent-private"
output=$(mktemp -d "$HOME/paper-agent-private/minilm-integration-XXXXXXXX")
printf '%s\n' "$expected_commit" > "$output/deployed-commit.txt"
printf '%s\n' "$image_id" > "$output/image-id.txt"
date -u +%Y-%m-%dT%H:%M:%SZ > "$output/capture-date.txt"
container_id=''
cleanup() {
  if [[ -n "$container_id" ]]; then
    docker kill "$container_id" >/dev/null 2>&1 || true
    docker logs "$container_id" > "$output/container.stdout" 2> "$output/container.stderr" || true
    docker inspect "$container_id" --format '{{json .State}}' > "$output/container-state.json" || true
    docker rm -f "$container_id" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
container_id=$(docker create --pull=never --network=none --read-only \
  --cpus=2 --memory=4g --memory-swap=4g --pids-limit=128 \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --user "$(id -u):$(id -g)" --workdir /harness \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --mount "type=bind,src=$checkout/experiments,dst=/harness/experiments,readonly" \
  --mount type=volume,src=paper-agent-minilm-model-cache,dst=/models,readonly \
  --mount "type=bind,src=$output,dst=/output" \
  --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 --env HF_HOME=/tmp/huggingface \
  --env TOKENIZERS_PARALLELISM=false --env PYTHONDONTWRITEBYTECODE=1 \
  --env OMP_NUM_THREADS=2 --env OPENBLAS_NUM_THREADS=2 \
  --env "PAPER_SMOKE_IMAGE_ID=$image_id" \
  --entrypoint python3 "$image_id" -m experiments.topical_relevance.smoke_container)
printf '%s\n' "$container_id" > "$output/container-id.txt"
docker start "$container_id" >/dev/null
if ! timeout --signal=TERM --kill-after=5s 900s docker wait "$container_id" > "$output/exit-code.txt"; then
  echo "Container exceeded the 900-second wall limit or wait failed. Results: $output" >&2
  exit 1
fi
[[ $(cat "$output/exit-code.txt") == 0 ]]
echo "Synthetic integration smoke succeeded. Private results: $output"
