# Explicit local-image rehearsal identity

This opt-in route enables the isolated SDLC build/save/load rehearsal without a
registry publish. It does not change production validation: the base migration
contract still requires `repository@sha256:<registry-manifest-digest>` for app
and Ollama. A Docker config image ID is a different identity and is never put
behind a fabricated repository name.

The explicit `docker-compose.mini-rehearsal.yml` overlay selects
`local-rehearsal`. Both images must then be full `sha256:<64 lowercase hex>` config
IDs. Tags, short IDs, mixed registry/config routes, unknown modes, missing or
mismatched receipts and mismatched source provenance fail validation. Every
service retains `pull_policy: never`; there is no Docker socket inside the app.
This is an operational provenance record, not a signed supply-chain attestation.

## Refresh and build from the exact source bundle

SDLC must refresh the candidate archive and its SHA256 after this correction.
The old prepared archive/receipt must not be reused. Extract that refreshed
archive into the build context and use its actual SHA256 as the additional build
argument:

```sh
docker build --target migration-gemini \
  --build-arg SOURCE_BUNDLE_SHA256="$SOURCE_BUNDLE_SHA256" \
  --build-arg VCS_REF="$CANDIDATE_BASE_REVISION" \
  --build-arg PYTHON_BASE_IMAGE="$APPROVED_PYTHON_BASE" \
  --build-arg NODE_BASE_IMAGE="$APPROVED_NODE_BASE" \
  --build-arg VERSION="$CANDIDATE_VERSION" \
  --build-arg BUILD_DATE="$BUILD_DATE" \
  -t paper-mini-rehearsal .
```

The full40-character base revision identifies the starting Git commit; the source
bundle SHA256 identifies the actual candidate, including its uncommitted changes.
Both are recorded in `/app/image-runtime-inputs.json`; the bundle hash is also
an image label alongside the existing OCI revision. Capture actual resulting
config image IDs and retain save-archive checksums separately. Save/load and any
image build remain SDLC operations, not actions performed by this code change.

## Inspect the loaded identities and issue the receipt

After loading the approved app and Ollama images on the rehearsal Docker host,
run this from the refreshed source checkout using the exact config IDs:

```sh
python3 -m paper_agents.rehearsal_images \
  --source-bundle "$REFRESHED_SOURCE_BUNDLE" \
  --app-image "$APP_CONFIG_ID" \
  --ollama-image "$OLLAMA_CONFIG_ID" \
  --output "$REHEARSAL_IDENTITY_FILE"
```

This hashes the actual source archive, performs bounded `docker image inspect`
reads for both IDs, requires inspected `Id` equality, and checks the app's source
hash and full revision labels. It does not build, pull, tag, load or start images.
It refuses to overwrite an existing receipt. The nonsecret receipt is read-only
and readable by container UID10001.

Populate the private rehearsal env file from the inspected receipt:

| Variable | Exact value |
| --- | --- |
| `PAPER_MIGRATION_APP_IMAGE` | receipt `app_image` config ID |
| `PAPER_MIGRATION_OLLAMA_IMAGE` | receipt `ollama_image` config ID |
| `PAPER_REHEARSAL_SOURCE_SHA256` | receipt `source_bundle_sha256` |
| `PAPER_REHEARSAL_IDENTITY_FILE` | absolute receipt file path |

Retain the other isolated project/volume/port/credential settings from the
candidate runbook. Select **both** files for rehearsal creation and model checks:

```sh
docker compose --env-file "$PAPER_MIGRATION_ENV_FILE" \
  -f docker-compose.mini-migration.yml -f docker-compose.mini-rehearsal.yml config --quiet
docker compose --env-file "$PAPER_MIGRATION_ENV_FILE" \
  -f docker-compose.mini-migration.yml -f docker-compose.mini-rehearsal.yml up -d app ollama
```

Do not alter the inspected image IDs between issuing the receipt and starting
Compose. App/parked scheduler/model helper receive the same read-only receipt.
Startup compares the selected IDs and source hash with the receipt and with the
app's baked inventory. Selecting only the base file deliberately rejects config
IDs; selecting the rehearsal overlay deliberately rejects registry references.
The parked scheduler stays disabled. Existing host cron `exec` calls address the
already-created app; they do not recreate it or replace its identity environment.

This identity route removes a packaging compatibility obstacle only. The Linux
lifecycle tests, real image/CPU compatibility, auth/model policy, copied-state and
recovery rehearsals remain SDLC qualification requirements. No result here is
production clearance.

Reference interfaces: [Docker image inspect](https://docs.docker.com/reference/cli/docker/image/inspect/)
and [Compose services](https://docs.docker.com/reference/compose-file/services/).
