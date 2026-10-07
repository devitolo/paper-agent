#!/usr/bin/env bash
# Run the opt-in paired pre-Curator MiniLM shadow experiment after a successful pipeline.
set -euo pipefail

started_at=$(date -u +%FT%TZ)
started_epoch=$(date +%s)
printf 'MiniLM Shadow started_at=%s\n' "$started_at"
report_completion() {
  status=$1
  printf 'MiniLM Shadow completed_at=%s status=%s elapsed_seconds=%s\n' \
    "$(date -u +%FT%TZ)" "$([[ "$status" == 0 ]] && printf success || printf failed)" \
    "$(( $(date +%s) - started_epoch ))"
  trap - EXIT
  exit "$status"
}
trap 'report_completion $?' EXIT

if [[ $# -ne 3 ]]; then
  echo "Usage: minilm_shadow_after_pipeline.sh SOURCE APP_CONTAINER REPO" >&2
  exit 64
fi
source_name=$1
app_container=$2
repo=$3
image_ref="${PAPER_MINILM_IMAGE:-paper-agent-minilm-smoke:onnx-1.23.2}"
expected_image="sha256:7d8b960220e3c6f60292e6d40a8f300ff19c5ee05cd97cf5f725a76673e2d5c2"
model_volume="${PAPER_MINILM_MODEL_VOLUME:-paper-agent-minilm-model-cache}"
input_limit="${PAPER_MINILM_SHADOW_INPUT_LIMIT:-10}"
output_limit="${PAPER_MINILM_SHADOW_OUTPUT_LIMIT:-3}"

image_id=$(docker image inspect "$image_ref" --format '{{.Id}}')
[[ "$image_id" == "$expected_image" ]] || { echo "MiniLM Shadow skipped: image identity mismatch." >&2; exit 1; }
docker volume inspect "$model_volume" >/dev/null
network_name=$(docker inspect "$app_container" --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' | head -n 1)
[[ -n "$network_name" ]] || { echo "MiniLM Shadow skipped: app network unavailable." >&2; exit 1; }
ollama_url=$(docker inspect "$app_container" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^PAPER_AGENT_OLLAMA_URL=//p' | head -n 1)
[[ -n "$ollama_url" ]] || ollama_url=http://ollama:11434/api/generate

runtime_dir=$(mktemp -d "${TMPDIR:-/tmp}/paper-minilm-shadow.XXXXXXXX")
cleanup_runtime() { rm -rf "$runtime_dir"; }
finish_with_cleanup() { status=$?; cleanup_runtime; report_completion "$status"; }
trap finish_with_cleanup EXIT
docker cp "$app_container:/app/paper_agents" "$runtime_dir/paper_agents"
docker cp "$app_container:/app/sql" "$runtime_dir/sql"
chmod -R a+rX "$runtime_dir"

docker run --rm --pull=never --network "$network_name" --read-only \
  --cpus=2 --memory=4g --memory-swap=4g --pids-limit=128 \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --user 10001:10001 --workdir /workspace \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m \
  --volumes-from "$app_container" \
  --mount "type=bind,src=$runtime_dir,dst=/workspace,readonly" \
  --mount "type=volume,src=$model_volume,dst=/models,readonly" \
  --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 \
  --env HF_HOME=/tmp/huggingface --env TOKENIZERS_PARALLELISM=false \
  --env PYTHONDONTWRITEBYTECODE=1 --env OMP_NUM_THREADS=2 --env OPENBLAS_NUM_THREADS=2 \
  --env "PAPER_AGENT_OLLAMA_URL=$ollama_url" \
  --entrypoint python3 "$image_id" \
  -m paper_agents.minilm_shadow --db /app/data/paper_agent.db --source "$source_name" \
  --input-limit "$input_limit" --output-limit "$output_limit"
