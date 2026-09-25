#!/usr/bin/env bash
# CI-only exact-image acceptance for the Mini production runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

IMAGE=${PAPER_MINI_ACCEPTANCE_IMAGE:?Set PAPER_MINI_ACCEPTANCE_IMAGE to image@sha256:digest}
VERSION=${PAPER_MINI_ACCEPTANCE_VERSION:-unknown}
REPORT_DIR=${PAPER_MINI_ACCEPTANCE_REPORT_DIR:-$ROOT/release-acceptance-results/mini}

fail() {
  echo "Mini release acceptance failed: $*" >&2
  exit 1
}

[[ "$IMAGE" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*@sha256:[a-f0-9]{64}$ ]] \
  || fail "Mini acceptance requires an immutable image@sha256 reference"

mkdir -p "$REPORT_DIR"
rm -f "$REPORT_DIR/status.txt" "$REPORT_DIR/image-inspect.json" "$REPORT_DIR/imports.txt"

{
  printf 'image=%s\n' "$IMAGE"
  printf 'version=%s\n' "$VERSION"
  printf 'host=%s/%s\n' "$(uname -s)" "$(uname -m)"
  printf 'started_at=%s\n' "$(date -u +%FT%TZ)"
} > "$REPORT_DIR/status.txt"

docker pull "$IMAGE"
docker image inspect "$IMAGE" > "$REPORT_DIR/image-inspect.json"

python3 - "$REPORT_DIR/image-inspect.json" <<'PY'
from __future__ import annotations

import json
import sys

inspect = json.loads(open(sys.argv[1], encoding="utf-8").read())
if not isinstance(inspect, list) or len(inspect) != 1:
    raise SystemExit("unexpected docker inspect shape")
labels = inspect[0].get("Config", {}).get("Labels", {}) or {}
if labels.get("org.projectpaper.runtime") != "mini-production":
    raise SystemExit("image is not the Mini production target")
if labels.get("org.opencontainers.image.source") != "https://github.com/devitolo/paper-agent":
    raise SystemExit("image source label mismatch")
revision = labels.get("org.opencontainers.image.revision", "")
if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
    raise SystemExit("image revision label is not a full commit SHA")
bundle = labels.get("org.projectpaper.source-bundle-sha256", "")
if len(bundle) != 64 or any(char not in "0123456789abcdef" for char in bundle):
    raise SystemExit("source bundle SHA label is missing or malformed")
PY

docker run --rm --network=none --entrypoint python "$IMAGE" - <<'PY' > "$REPORT_DIR/imports.txt"
from __future__ import annotations

import importlib.metadata

modules = [
    "paper_agents.gemini_guardian",
    "paper_agents.gemini_runtime",
    "paper_agents.import_state",
    "paper_agents.migration_job",
    "paper_agents.migration_lifecycle",
    "paper_agents.package_runtime",
    "paper_agents.rehearsal_images",
    "paper_agents.scheduler",
    "paper_agents.scheduler_child",
]
for module in modules:
    __import__(module)
print("imports=ok")
print("opentelemetry-api=" + importlib.metadata.version("opentelemetry-api"))
print("opentelemetry-sdk=" + importlib.metadata.version("opentelemetry-sdk"))
PY

docker run --rm --network=none --entrypoint node "$IMAGE" --version >> "$REPORT_DIR/imports.txt"
docker run --rm --network=none --entrypoint sh "$IMAGE" -c 'test -d /opt/paper-gemini/node_modules/@google/gemini-cli' \
  || fail "Gemini CLI runtime dependency missing"

{
  printf 'completed_at=%s\n' "$(date -u +%FT%TZ)"
  printf 'result=pass\n'
} >> "$REPORT_DIR/status.txt"

echo "Mini release acceptance passed for $IMAGE"
