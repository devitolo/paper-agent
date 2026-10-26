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
