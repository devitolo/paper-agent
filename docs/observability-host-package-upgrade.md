# Mini host-package upgrade contract (not activated)

The fixed root-owned `/usr/local/sbin/project-paper-observability-package-install`
accepts only `--target mini-production --bundle <sha256> --source-sha <commit>`
or the fixed target with `--recover`. It cannot update AWS. The sudoers example
is for review; it is not installed by a runner or by this package.

GitHub OIDC artifact attestations authorize BOTH `artifact.tar` and the external
`manifest.json`, using `attestation.jsonl`. No separate signing key is generated,
read or managed. A root-provisioned GitHub CLI verifier is required at
`/usr/local/libexec/project-paper-observability/gh`. Its identity policy pins:

- repository `devitolo/paper-agent`;
- signer workflow `.github/workflows/observability-release.yml`;
- exact certificate identity for `refs/tags/observability-mini`;
- GitHub Actions OIDC issuer and SLSA provenance v1;
- matching source and signer commit, rejecting self-hosted build attestations.

Reference: https://cli.github.com/manual/gh_attestation_verify

The runner stages three bounded files under the fixed root-owned incoming
parent `/var/lib/project-paper-observability/mini-production-incoming/<sha256>-<source-sha>`.
The incoming parent is root-owned with a sticky group-writable mode (1770,
group devitolo) provisioned by the administrator so the runner can create
bundles; its ancestors must not be writable. The bundle directory may be
runner-owned. Directory-relative NOFOLLOW opens
snapshot regular-file bytes; no tar extraction is performed. Root-owned staging
holds the snapshot during verification. The manifest is not trusted until its
own attestation passes. Strict schema, target, source commit, timestamps,
installer version, archive digest and every allowlisted file digest/type/mode
are then checked. The archive is an uncompressed tar (maximum 1 MiB), containing
only the Mini wrapper, image-update helper and collector launcher as root:root
0755. No configuration, secrets, publisher state, business data or scheduling
files are accepted from the archive.

Deployment and collector locks serialize changes. Exact existing combined cron
is required and preserved. Prior executable bytes/modes, cron and audit receipt
are saved in a root-only transaction directory; a durable pending marker is
written before replacement. Each file replacement uses fsync and atomic rename.
Collector-only qualification verifies a newly produced, timestamped Mini host
status. The publisher is not invoked during qualification. A successful update
records source/digest/prior hashes/result/time in a root-only audit receipt.
Failure restores prior files. Interruption leaves the pending marker, blocking
new upgrades until `--recover` validates all backups, restores them and checks
health. Invalid recovery backups keep the marker and require trusted repair.
Multiple files are transactional under locks; a crash between renames is handled
by recovery rather than claiming a multi-file atomic filesystem operation.

Synthetic-verifier Linux tests cover the verification argument policy, tamper,
wrong target/schema/path/hash/owner/mode, archive links, FIFO/symlink inboxes,
expiry/future timestamps, trust ownership, serialization, qualification rollback,
interrupted recovery and corrupt backups. They do not prove live OIDC signing or
real GitHub CLI verification. Those need the hosted release workflow and an
independently bootstrapped verifier before production acceptance.

Activation is NOT complete: SRE found no existing unattended root provisioning
route, and Mini does not have GitHub CLI installed. The root installer, trusted
verifier binary, incoming parent and narrow sudo rule must first be provisioned
through an independently authorized administrative mechanism. Neither a runner
script nor Docker host mounts may bootstrap that authority. The existing
collect-publish cron remains active without this upgrade mechanism.

SRE crash review: the trusted bootstrap must install pending guards in the cron
wrapper, collector launcher and image-only deployment helper before enabling the
package installer. Both existing and packaged entrypoints require guard version
v1. While pending, scheduled runs and image deployments refuse with exit 75.
The fixed installer uses an explicit clean environment for collector-only health
qualification; only that root-controlled invocation sets the qualification flag.
The narrow sudo rule has no SETENV permission. Recovery uses the same locks.
Crash-after-each-file-replacement tests execute the entrypoint guards and verify
refusal until recovery completes. Successful audit history is bounded to 20
transactions; failed and pending evidence is never pruned. Control-directory
fsync makes transaction creation and pending-marker removal durable.

Read-only production policy probe on 2026-10-26: invoking the existing fixed
helper with an invalid `--probe` argument and an explicit qualification-variable
assignment was rejected by sudo before executing the helper:
`sudo: sorry, you are not allowed to set the following environment variables: PROJECT_PAPER_HOST_PACKAGE_QUALIFY`.
This made no host changes. SRE accepted the clean-environment qualification
exception subject to this effective-policy check. Guard markers remain version
compatibility checks; signed executable content still requires review.

The repository visibility API reports public; artifact-attestation eligibility is
therefore not blocked by private-repository plan restrictions. No repository
visibility setting was changed. A real hosted positive/negative attestation test
is still required; fixture verification is not a replacement.


The inbox entry includes both digest and source commit so unchanged host content
can be attested by a later approved release without overwriting a prior bundle.
Once a signed package is verified, matching installed files plus a successful
receipt make the operation a no-op; image-only releases do not rewrite or
requalify unchanged host code. The candidate release workflow removes its own
inbox directory only after a successful fixed-installer result. Failed inputs
remain for audit. This production workflow candidate is not merged or activated.

## Hosted verification evidence

Run `40404108157` succeeded on source
`c752a795c6e20a5d5e811e55c70cc81a8c8b7af9` using GitHub CLI 2.102.0.
Artifact SHA256 was
`b9e497d6c464b1576d34e3f9f88891b3ed34067946da548d83a4b5db936abd2a`.
Both archive and manifest verified; wrong source commit, tampered bytes and the
test workflow claiming production identity were rejected. This workflow has no
deployment job. Its branch identity is intentionally rejected by the production
installer's exact tag/workflow certificate policy.

An additional local verification of that real downloaded bundle succeeded with
an empty HOME/GH_CONFIG_DIR and a clean environment without GH_TOKEN, proving
that this public bundle can be verified without root reading runner credentials.
The initial run `40403910614` exposed mutually exclusive CLI selectors. The fix
retains exact certificate identity (which pins workflow and ref) and removes the
redundant `--signer-workflow` flag. The issuer, repository, source/signer commit,
source ref, predicate and hosted-runner checks remain enforced.
