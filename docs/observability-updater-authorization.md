# Mini updater release authorization

Decision recorded October 27, 2026. Scope: authorization design and policy only;
no bootstrap, production permission change, tag movement, or deployment.

## Verified gap

SDLC's read-only GitHub API inspection found that the actual observability
environment, `observability-mini-production`, has `protection_rules=[]` and no
reviewers, `can_admins_bypass=true`, and `deployment_branch_policy=null`.
Its deployment branch policies request returned 404. The separate
`mini-production` environment's branch policy does not protect observability
releases. SDLC also queried repository rulesets and found none. The legacy tag
protection API returned 404; that response alone would not establish the
absence of rulesets.

A build attestation is provenance, not production approval. The candidate's
current pre-approval package attestation and root verification policy must not
be activated as production authorization.

## Accepted design

SRE accepts the following design subject to implementation and real proof:

1. Build and test on GitHub-hosted runners without production authority. Bind
   build provenance to the exact package archive, external manifest and source.
2. Call a reusable authorizer at an immutable reviewed commit. The authorizer
   contains the literal `observability-mini-production` environment, hosted
   runner selection and all checks. No caller-supplied scripts execute there.
3. Fetch fresh protection configuration using GitHub's API. Require configured
   reviewers with the exact human-selected User/Team ID set pinned in the
   authorizer, self-review prevention, disabled administrator bypass, and a
   deployment policy allowing only tag `observability-mini`. Missing fields,
   inaccessible APIs or unexpected policies fail closed. Check before waiting
   for approval and again before signing; never substitute caller assertions.
4. Require the production environment approval before issuing authorization.
   Independently verify the completed build's identity, successful validation,
   archive/manifest provenance and immutable source; do not trust caller claims
   that tests passed. Approval must identify the immutable artifacts being
   authorized. Tag movement cannot substitute a different commit or package.
5. Authorization binds repository, Mini target, tag push, source SHA, archive
   digest, external manifest digest and the manifest's immutable AMD64 image.
6. Root verifies both subjects and pins the reusable authorizer's workflow path
   and signer commit separately from the changing release source SHA. Reject
   pre-approval build signatures, other workflows/signers/refs, changed bytes
   and mismatched image/source. Retain existing replay and rollback protections.

`scripts/observability_authorization_policy.py` implements the initial pure
context and environment checks. It does not fetch configuration, integrate a
signer, or grant authority; invoking it from mutable caller code is insufficient.

## Remaining evidence before activation

- Configure a human-selected reviewer and required environment protections;
  no reviewer is inferred and no settings changed in this step.
- Implement and review the immutable authorizer and caller integration, then
  record the actual signer commit in the bootstrap/root policy.
- Prove real hosted OIDC positive verification and rejection of pre-approval,
  alternate signer/ref/source, modified artifacts, missing approval rules and
  tag movement. Local synthetic verification does not satisfy this requirement.
- Complete independent transaction/bootstrap QA and final bundle review.

The existing Docker-capable runner is already root-equivalent if compromised.
The narrow sudo interface does not remove that existing trust limitation.

References: [GitHub reusable-workflow OIDC identity](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-with-reusable-workflows),
[GitHub CLI verification and signer/source digest controls](https://cli.github.com/manual/gh_attestation_verify).
