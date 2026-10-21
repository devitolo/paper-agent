#!/usr/bin/env bash
set -euo pipefail

IMAGE=${PAPER_MINI_ACCEPTANCE_IMAGE:?Set PAPER_MINI_ACCEPTANCE_IMAGE}
REPORT_DIR=${PAPER_MINI_ACCEPTANCE_REPORT_DIR:-release-acceptance-results/mini-registry}
mkdir -p "$REPORT_DIR"
[[ "$IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || { echo "Image must be immutable" >&2; exit 1; }

raw=$(docker buildx imagetools inspect "$IMAGE" --raw)
printf '%s\n' "$raw" > "$REPORT_DIR/manifest.json"
python3 - "$IMAGE" "$REPORT_DIR/manifest.json" "$REPORT_DIR/status.txt" <<'PY'
import json, sys
image, manifest_path, status_path = sys.argv[1:]
data = json.load(open(manifest_path))
manifests = data.get("manifests", [])
platforms = {
    ((m.get("platform") or {}).get("os"), (m.get("platform") or {}).get("architecture"))
    for m in manifests
}
requested = __import__('os').environ.get('PAPER_MINI_REQUIRED_PLATFORMS', 'linux/amd64').split()
required = {tuple(item.split('/', 1)) for item in requested}
if not required.issubset(platforms):
    raise SystemExit(f"missing required platforms: {sorted(required - platforms)}")
with open(status_path, "w") as out:
    out.write(f"image={image}\nresult=pass\nplatforms={sorted(platforms)}\n")
PY
echo "Mini registry acceptance passed for $IMAGE"
