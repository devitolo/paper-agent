# One observability image, two isolated invocations

Routine releases update one immutable AMD64 observability image through the
existing image-only deployment helper. Both collector and publisher use that
same configured digest. No general host updater, attestation bootstrap, signing
key, new sudo permission or additional cron is introduced.

The stable host boundary starts the default collector with no network and the
existing host metrics read mounts. It then starts the fixed publish.sh entrypoint
as UID10002 with network enabled, export/key read-only and publisher state
writable. The publisher receives no host metrics, Docker socket or host-root
mounts. Credentials are never mounted into collection. HTTPS authentication,
retry, pending payload preservation and reporting remain in versioned Python
code inside the image. The host only retains fixed mounts, isolation, budgets,
timeouts and sequencing required to launch two containers without giving either
container Docker access.

The currently installed wrapper selects a separately pinned publisher image.
After the new image is built and qualified, one narrowly reviewed wrapper-only
adjustment is required to select the existing collector image variable for both
roles and use the fixed publisher entrypoint. The existing cron, configuration,
transport key, pending state, data and deployment sudo rule are preserved. The
already installed image-only helper retains the combined wrapper on later
releases. This one-time adjustment is not a general package-install capability.

Migration order: qualify the new image; release its collector role through the
existing helper while the old publisher image remains active; atomically replace
only the wrapper; verify the next natural15-minute run and matching AWS receipt.
If migration fails, restore the saved wrapper. For later image rollbacks, both
roles move together to a prior single-image digest; retain export/key/state and
never import a stale application snapshot. The first migration can roll back to
the saved two-image wrapper before restoring the old collector-only image.

AWS Health UI is an independent serving release and must not wait for this Mini
migration. No Mini application or paper-data synchronization change is required.


The one-time wrapper installer checks a pre-existing immutable AMD64 image and
runs only the publisher entrypoint's help command with network disabled and no
mounts. It does not pull images or run collection/publication. It snapshots the
reviewed wrapper bytes and pins their digest, holds the existing two locks,
saves a root-only prior wrapper, fsyncs rollback evidence, and atomically
replaces exactly that one file. A crash leaves a complete old or complete new
wrapper. Cron, image configuration, keys, state and sudo permissions are not
modified. Five disposable Linux tests cover rollback, interruption, source
validation, unchanged configuration and repeated installation. The broader
host-package/bootstrap prototype is not part of this release.

Build-only candidate evidence: sourcec1e8fda67f3a21f1757f0036ce6625e695e444a6,
run40406949665 passed both isolated role smoke tests using
`ghcr.io/devitolo/paper-agent/observability:single-image-c1e8fda67f3a21f1757f0036ce6625e695e444a6@sha256:25036aa663c412f72be6e3d69a645e04a3066fdd146b9219dee77c39318f3743`.
The test build did not deploy or move production tags.


## Mini container observations

The existing combined 15-minute wrapper collects only fixed Docker status, health,
running image ID/registry digest and sampled CPU/memory facts for the production
app, MiniLM, Ollama, Phoenix, observability collector and publisher. Non-production
and retired containers are excluded. The host Docker CLI formats these fields;
no inspect environment, labels, mounts, configuration or logs are exported.
The publisher receives bounded input over stdin and reuses authenticated telemetry
delivery. Neither image role mounts the Docker socket. No extra cron is installed.

The Health receiver accepts old snapshots during rollout. New container readings
have their own observation time and are hidden after 45 minutes. A missing health
check is not healthy; an absent scheduled collector/publisher is not evidence of a
failed job. Registry digests and local image IDs are labeled separately.

Activate only after the AWS receiver supports the additive container fields.
Deploy the qualified single image through the existing image-only helper first,
then use the reviewed one-file wrapper installer. The installer checks the image's
container-input capability, saves the old wrapper, and preserves cron, credentials,
application state and helper permissions. Until the wrapper is updated, the existing
publisher continues to run; an image-only update cannot modify this root-owned file.
