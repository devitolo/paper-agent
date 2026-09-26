#!/usr/bin/env bash
# Run isolated MiniLM evaluation after a successful production Scout pipeline.
set -euo pipefail

started_at=$(date -u +%FT%TZ)
started_epoch=$(date +%s)
printf 'MiniLM Eval started_at=%s\n' "$started_at"
report_completion() {
  status=$?
  completed_at=$(date -u +%FT%TZ)
  elapsed_seconds=$(($(date +%s) - started_epoch))
  if [[ "$status" == 0 ]]; then
    result=success
  else
    result=failed
  fi
  printf 'MiniLM Eval completed_at=%s status=%s elapsed_seconds=%s\n' \
    "$completed_at" "$result" "$elapsed_seconds"
  trap - EXIT
  exit "$status"
}
trap report_completion EXIT

if [[ $# -ne 3 ]]; then
  echo "Usage: minilm_eval_after_pipeline.sh SOURCE APP_CONTAINER REPO" >&2
  exit 64
fi

source_name=$1
app_container=$2
repo=$3
image_ref="${PAPER_MINILM_IMAGE:-paper-agent-minilm-smoke:onnx-1.23.2}"
expected_image="sha256:7d8b960220e3c6f60292e6d40a8f300ff19c5ee05cd97cf5f725a76673e2d5c2"
model_volume="${PAPER_MINILM_MODEL_VOLUME:-paper-agent-minilm-model-cache}"

image_id=$(docker image inspect "$image_ref" --format '{{.Id}}')
if [[ "$image_id" != "$expected_image" ]]; then
  echo "MiniLM Eval skipped: image identity mismatch." >&2
  exit 1
fi
docker volume inspect "$model_volume" >/dev/null

docker run --rm --pull=never --network=none --read-only \
  --cpus=2 --memory=4g --memory-swap=4g --pids-limit=128 \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --user 10001:10001 --workdir /workspace \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --volumes-from "$app_container" \
  --mount "type=bind,src=$repo,dst=/workspace,readonly" \
  --mount "type=volume,src=$model_volume,dst=/models,readonly" \
  --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 \
  --env HF_HOME=/tmp/huggingface --env TOKENIZERS_PARALLELISM=false \
  --env PYTHONDONTWRITEBYTECODE=1 --env OMP_NUM_THREADS=2 --env OPENBLAS_NUM_THREADS=2 \
  --entrypoint python3 "$image_id" \
  -m paper_agents.minilm_eval --db /app/data/paper_agent.db --source "$source_name"
