# Infrastructure QA fixes — October 26, 2026

These are review candidates only. No release tags, production cron, host files,
containers, or data were changed while preparing them. Daily peaks remain out of scope.

## Findings addressed

- F1 (AWS): attribute CPU deltas to their ending observation, clip intervals to
  the declared window, weight averages by covered time, deduplicate retry overlap,
  and report actual coverage. Accept delayed observations up to the current
  reference time, including delays over five minutes. Unmeasured one-minute and
  sustained peaks remain null. This ports the existing Mini interval calculation
  with an AWS delayed-observation correction.
- F2 (Mini): sample disk every collection. Keep disk value and sample time paired.
  The allowlisted transport preserves optional latest.disk_observed_at; the reader
  suppresses missing/expired provenance independently of the host/receipt age.
  Unknown legacy disk provenance is unavailable, never refreshed using CPU time.
- F3 (UI): grid children can shrink and each table retains its own horizontal
  scrollbar. No page-wide overflow at tested widths 320, 390, 768, 1280.
- F4 (UI): each container table shows its own Observed timestamp and age in
  Pacific time. These are sampled observations, not synchronous runtime checks.
- F5 (both launchers): explicit config/target takes precedence; without either,
  select the sole readable installed role. Multiple roles or an unsupported
  target fail closed. No automatic AWS fallback on a Mini installation.

## Required release order and rollback

1. Retest these candidates independently; obtain the user's release approval.
2. Release the AWS app/reader first, before enabling the updated Mini publisher.
   The old strict reader rejects the new disk_observed_at key. The new reader
   accepts legacy payloads and hides their disk values when provenance is absent.
3. Release AWS collector and approved Mini package separately using target-specific
   protected release flows. Ensure launcher installation is included; image-only
   substitution does not update an already installed host launcher. Mini activation
   stays on hold until explicitly approved. Do not use Docker to bypass permissions.
4. Observe natural collection, delivery, per-field timestamps, CPU coverage and
   both tables; preserve existing files/data/identity/locks/logs/rollback state.

For rollback to an old strict AWS reader, revert Mini's producer first and confirm
an accepted legacy-format receipt before rolling back the AWS reader. Retain the
telemetry state rather than deleting or replacing it. Otherwise old readers may
reject the persisted new-format snapshot. No production rollback was exercised here.

## Candidate verification

- UI/reader focused native suite: 51 tests passed before additional provenance cases.
- New disk provenance cases: 3 passed (TTL boundary, independent expiry, legacy,
  malformed/future field time and rejection of raw sensitive-shaped time strings).
- Independent tester acceptance: all 12 passed with both candidate source paths supplied.
- Combined Linux/Python 3.12 UI/transport/provenance/independent suite: 66 run,
  65 passed, 1 skipped (Docker Compose CLI unavailable in the isolated container).
- Mini collector/transport/projection native suite: 49 passed, including HTTPS.
- AWS collector/wrapper/reader suite: 46 run, 45 passed, 1 existing platform skip.
- Installer/recovery in disposable root Linux: 34 passed against each package.
- Additional direct-launcher role cases against Mini: 4 passed (including the two
  formerly failing direct-launcher methods). The same launcher change is in AWS.
- Browser DOM geometry: document widths exactly 320, 390, 768, 1280 at matching
  viewports. Wider table content stays inside the panel scrollbar. Phone preview
  evidence: /private/tmp/health-resource-cards-preview/qa-phone-contained.png.
- Shell syntax and git diff --check passed. No builds were published or deployed.

Independent tests were copied unchanged from tester's shared checkout into the
UI candidate. Run the cross-release probes from that UI worktree with:

```sh
QA_AWS_SOURCE=/private/tmp/paper-aws-container-collector \
QA_MINI_SOURCE=/private/tmp/paper-mini-container-monitoring \
python3 -m unittest tests.test_infrastructure_release_20261026_qa -v
```

This is developer verification, not independent sign-off. Tester should recheck
candidate SHAs, phone overflow with the actual page wrapper, delayed/overlapping
intervals, transport rollout/rollback compatibility and direct target selection.
