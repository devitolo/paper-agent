# Mini observability updater release contract

The user explicitly confirmed on October 27, 2026 that the existing automatic
release flow is intentional. No new reviewer, manual approval, administrator
bypass restriction, reusable authorizer or tag-governance change is required.
The previously proposed approval policy is removed.

The release continues to use tag `observability-mini` and environment
`observability-mini-production`. This environment has no required reviewers;
its name is not evidence of human approval.

## Integrity boundary

- Hosted validation succeeds before hosted image/package build and signing.
- Both the exact archive and external manifest receive GitHub OIDC provenance.
- Root verifies repository `devitolo/paper-agent`, exact workflow
  `.github/workflows/observability-release.yml`, exact tag ref
  `refs/tags/observability-mini`, matching source and signer commit, GitHub OIDC
  issuer and hosted runner origin. Other workflows and changed bytes fail.
- The manifest binds Mini target, source SHA, archive/file hashes and immutable
  AMD64 image digest. Root also checks source label and architecture.
- Only fixed approved host files and literal existing config are accepted.
  Image and files update together under locks, with backups and rollback.

This establishes trusted automatic release provenance, not independent human
approval. Repository release writers and the reviewed release workflow remain
trusted. The existing Docker-capable runner is already root-equivalent if
compromised; narrow sudo does not remove that existing trust limitation.

## Readiness and activation

Qualification uses the candidate branch only, with no production tag, image
publication or deployment. Hosted verification must pass for archive and
manifest and reject wrong commits, bytes and production signer identity.
Disposable Linux tests cover installation, bootstrap, recovery and rollback.

A deliberate one-time bootstrap is necessary because the current Mini helper
can replace only the image. Prepare and independently verify the exact pinned
bundle before activation. Qualification does not run bootstrap. After bootstrap,
normal releases update the three approved scripts and image through one package
transaction without repeated manual sudo. Updating the root updater itself is
outside that narrow package scope.
