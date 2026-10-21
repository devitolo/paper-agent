#!/usr/bin/env bash
set -euo pipefail

IMAGE=${PAPER_MINI_ACCEPTANCE_IMAGE:?Set PAPER_MINI_ACCEPTANCE_IMAGE}
REPORT_DIR=${PAPER_MINI_ACCEPTANCE_REPORT_DIR:-release-acceptance-results/mini-registry}
mkdir -p "$REPORT_DIR"
[[ "$IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || { echo "Image must be immutable" >&2; exit 1; }

raw=$(docker buildx imagetools inspect "$IMAGE" --raw)
printf '%s\n' "$raw" > "$REPORT_DIR/manifest.json"
PAPER_MINI_EXPECTED_PLATFORMS=${PAPER_MINI_EXPECTED_PLATFORMS:-linux/amd64} \
python3 - "$IMAGE" "$REPORT_DIR/manifest.json" "$REPORT_DIR/status.txt" "$PAPER_MINI_EXPECTED_PLATFORMS" <<'PY'
import json, sys
image, manifest_path, status_path, expected_text = sys.argv[1:]
data = json.load(open(manifest_path))
manifests = data.get("manifests", [])
platforms = {
    ((m.get("platform") or {}).get("os"), (m.get("platform") or {}).get("architecture"))
    for m in manifests
}
required = {tuple(item.split("/", 1)) for item in expected_text.split(",") if item}
if any(len(item) != 2 for item in required):
    raise SystemExit(f"invalid expected platform specification: {expected_text}")
if not required.issubset(platforms):
    raise SystemExit(f"missing required platforms: {sorted(required - platforms)}")
with open(status_path, "w") as out:
    out.write(f"image={image}\nresult=pass\nplatforms={sorted(platforms)}\n")
PY
echo "Mini registry acceptance passed for $IMAGE"
