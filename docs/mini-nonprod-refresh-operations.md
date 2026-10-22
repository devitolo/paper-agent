# Mini nonprod access and explicit refresh

SDLC is the sole Mini deployment/refresh operator. Developer owns source and
GitHub Actions; Tester verifies sequentially after SDLC ends its operations.
This procedure never deploys to AWS or production and never installs a schedule.

At qualification time the copied database contained 1,523 papers and 321
recommendations, with 263 recommendations visible in the UI. The UI is labeled
`MINI NON-PROD -- DATA COPY`. Scheduling, sync, Gemini, telemetry, and MiniLM
remain disabled; production is not modified by refresh.

## Scope and prerequisites

The user approved replacing nonprod test data with a snapshot of authoritative
Mini production and making the nonprod UI reachable privately. Each execution is
an explicit operator action, within an applicable SSH/change authorization.

Use a reviewed main commit and successful GitHub Actions immutable AMD64 image.
Verify run URL, commit, digest and source-bundle label before staging. Pull only
that exact image. Do not build an image on Mini or deploy a local branch image.
Stage `scripts/mini_nonprod_refresh.py` from that same release, with its checksum,
at `/home/devitolo/workspace/paper-mini-nonprod/operator/scripts/mini_nonprod_refresh.py`.
The controller extracts ingress Compose from the exact image. It does not pull.

The controller is fixed to Mini `clarkmini`, user `devitolo`, LAN
`192.168.1.110/24`, the existing production pipeline container and three known
source volumes. It refuses unexpected role/mount identities, aliased paths,
nonlocal volume drivers, writable source mounts, shared role volumes and unknown
destination namespaces. It checks at least 5 GiB disk and 1.5 GiB available RAM;
these are operation prerequisites, not load qualification.

First invocation, after the exact image is available locally:

```sh
python3 /home/devitolo/workspace/paper-mini-nonprod/operator/scripts/mini_nonprod_refresh.py \
  --image ghcr.io/devitolo/paper-agent@sha256:APPROVED_DIGEST \
  --revision APPROVED_MAIN_COMMIT --source-bundle APPROVED_BUNDLE_SHA256
```

That is a read-only plan. Add `--apply` to perform the refresh. After the first
successful refresh the approved image identity is recorded in
`/home/devitolo/workspace/paper-mini-nonprod/current-refresh.json`, so subsequent
explicit refreshes use:

```sh
python3 /home/devitolo/workspace/paper-mini-nonprod/operator/scripts/mini_nonprod_refresh.py --apply
```

Omit `--apply` for inspection. Reusing this command does not upgrade production or
nonprod code; a code upgrade requires explicitly qualified image/revision/bundle
arguments. There is no source/destination argument and no reverse copy mode.

## Capture and switching semantics

1. Acquire the nonprod refresh lock and existing production deploy lock. Recheck
   identities under the locks. Existing production cron does not use the deploy
   lock, so that lock alone is not treated as a writer fence.
2. Acquire the existing scheduled-job and scout lock inodes through read-only
   mounts, drain other runtime lease holders, and keep production PID 1 running.
   Abort on occupied locks or a writer that fails to drain. The fence has a
   bounded lifetime; no cron bytes, production configuration or lock contents
   are changed. Run away from scheduled job boundaries: the existing scheduler
   may skip a job when its scheduled-job lock is occupied.
3. Use SQLite backup, validate the supported schema/integrity/foreign keys, and
   verify source logical rows before/after capture. Copy `profile.json`,
   `topics.yaml`, `industry.json` and DB-referenced artifacts only. Config and
   artifact hashes must remain stable. Enforce a 1 GiB payload cap. Release the
   production fence as soon as capture is verified.
4. Create new data/configuration volumes for UI and pipeline. Install the same
   verified snapshot independently into each, with distinct installation/role
   provenance and UID 10001 ownership. Existing role logs, lifecycle and generated
   volumes remain distinct and are retained. No model pool or production secret
   mount is present in any nonprod container.
5. Stop only nonprod roles/ingress. Preserve all old data/config volumes offline;
   additionally save a consistent SQLite backup, config/profile/provenance and
   full retained-file hashes in the snapshot volume under `/backups/{ui,pipeline}`.
   Old referenced artifacts remain in the retained old data volume. Never prune.
6. Create the two new role containers, verify image, mounts, environment and
   network isolation before startup, then start the dedicated ingress proxy.
   Both roles stay on the internal-only Docker network. Only the proxy also
   joins a separate bridge and binds `192.168.1.110:18080`.
7. Verify readiness, actual host HTTP access, NON-PROD watermark, snapshot
   provenance, expected paper/recommendation counts, separate storage and
   unchanged production container identities/cron. Then independently verify
   access from the user's Mac and visible review cards before declaring ready.

The production source image/revision recorded in the snapshot is taken from the
running production container. It can differ from the new nonprod release image.
The snapshot includes Mini's persisted feedback, not unconfirmed AWS feedback.
It does not establish Mini/AWS or nonprod UI/pipeline synchronization.

## User access and behavior

Private URL: `http://192.168.1.110:18080` from the same LAN. No public/router/AWS
change is involved. The proxy enforces LAN source addresses, exact Host and
same-origin browser requests. It has one fixed upstream, no state mounts or
credentials. Nonprod is deliberately not reachable through arbitrary hostnames.

The UI shows NON-PRODUCTION and snapshot provenance. The real snapshot mode
retains disabled scheduling, Gemini, MiniLM inference, Ollama access, telemetry
and sync. Local feedback changes nonprod only. UI and pipeline databases are
independent forks; feedback in one does not propagate to the other or production.
Refreshing replaces nonprod edits with a new production snapshot; backups retain
the previous nonprod state. Never copy nonprod feedback back to production.

Referenced PDFs are bounded copies, not live shared production mounts. Models,
unreferenced artifacts, old production DB backups, import receipts, runtime
locks/status, provider keys, HMAC secrets and auth identities are excluded.

## Evidence, failure and rollback

Each execution writes a private result under
`/home/devitolo/workspace/paper-mini-nonprod/refreshes/<refresh-id>/result.json`.
It records source/release identities, counts/time, new and previous volumes,
backups, exact old/new Compose paths, verification and rollback outcome.
Snapshot provenance is `data/NONPROD_SNAPSHOT.json` in each destination and in
the staging volume's `/new/data`. Its file hashes are capture/install evidence;
mutable nonprod feedback does not need to preserve the initial DB byte hash.

Failure before switching leaves the old roles in place. A failure after stopping
or switching restores the previous nonprod image/Compose/data/config and leaves
all new volumes for inspection. Production never participates in rollback.
The report explicitly flags incomplete rollback or fence cleanup for SDLC.

After a successful refresh, a later rollback is a separate explicit operator
action: use the recorded old Compose/env paths and hashes, stop current nonprod
ingress/roles, restore each recorded old role using explicit project/service and
`create --no-build --pull never --force-recreate`, check its image/mounts, then
start it. Restore the old ingress if one existed; otherwise leave new ingress
stopped. Update the current-refresh record only after the restored roles pass
their applicable readiness check. Never use `down --volumes`, volume removal,
prune, restore-to-production, or production Compose for rollback.

Before Tester handoff, SDLC verifies the URL from the Mac, closes SSH activity,
and supplies the release identity, report path, counts, snapshot IDs and backup
locations. Tester then validates real cards/topics, badges, local feedback,
disabled external actions and isolation without discovery/provider/model work.
