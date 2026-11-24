# Nook modernization roadmap

## Current work

- Restored PR #7 exactly at `8e70a28` and retained its history in a verified
  local Git bundle. The owner-supplied source ZIP matches all 108 tracked files.
- Implemented manual candidate preparation, Sigstore provenance checks in
  the installer/updater, a separate publisher and a fail-closed Stable gate.
  Promotion verifies published predecessors before comparing application trees.
- Fresh installers resolve the channel or exact published version before
  downloading. The development checkout has its own explicit mode.
- Remaining owner setup: protect `main` and version tags, configure the
  release environments and approve a new Actions run. Existing CI runs for
  PR #7 reported `action_required`.
- Backups use SQLite snapshots, reject changed non-SQLite files, and support
  offline capture, age encryption, retention and explicit provider opt-in.
- The next implementation steps are the disposable update/recovery matrix and
  the remaining architecture work below.

## Principles

- Preserve each installation's data, identity, configuration, local code and
  selected update behavior. A conflict is a stop condition, not permission to
  overwrite.
- Keep owner authorization, local-first operation and opt-in privileged access.
- Extend the existing source updater and mod architecture where possible;
  avoid a wholesale rewrite of `bot.py`.
- Test on disposable installations. Do not update production or publish
  releases without explicit owner approval.

## Historical baseline (before iteration 2)

- The checkout is on `dev`, has a shallow Git history, and has no local tags.
  The working tree was clean before this modernization branch was created.
- The GitHub Releases page currently lists `v0.1.0` and `v0.1.1`, both marked
  pre-release. No local tag history is available in this shallow checkout.
- `tools/build_release.py` defines `VERSION = '0.1.1'`; the generated
  `MANIFEST.json` repeats that version. This is not yet a unified version
  source or a channel-aware release policy.
- Source updates already pin a commit, validate archive paths and a manifest,
  preserve runtime/private files, refuse conflicting local edits, journal
  writes, and support offline rollback and supervised health checks. See
  `docs/UPDATES.md` and `tests/test_updates.py`.
- The updater currently fetches `main`; it has no per-instance release
  channel, semantic-version selection, GitHub Release artifact verification,
  or downgrade policy.
- CI runs unit tests, API/runtime smoke checks, release composition checks and
  shell syntax checks on Python 3.11 and 3.12. A separate workflow audits
  dependency advisories. Both workflows use pinned Actions and read-only
  repository permissions.
- Initial baseline: 91 tests ran. In the system interpreter, two errored:
  missing `aiohttp` and a Git identity integration test. In an isolated
  environment with declared dependencies, 90 passed and the Git identity
  test hit the execution environment's commit-signing author restriction.
  The test now verifies configured Git author/committer identities without
  creating an unrelated commit; the updated suite passes 93 tests.
- No production instance was inspected or modified. No performance benchmark,
  test-coverage measurement, fresh-OS install, real Telegram test, independent
  penetration test or full migration/rollback drill has been performed.

## Priorities

## Completed in this iteration

- Added root `VERSION` as the canonical SemVer input for release bundles and
  manifests, with validation for stable and prerelease forms.
- Made installer validation and source copying exclude local runtime state,
  credentials and provider sessions. A regression test verifies these remain
  private while custom source files are still copied.
- Added the initial audit/roadmap and documented the source/runtime boundary.

### P0 — Preserve data and authority

- Implemented: updater fails closed on unknown install state, local conflicts,
  unsupported update APIs and incompatible data changes.
- Implemented: regression coverage preserves private runtime data and
  user-owned extensions; backups restore only to a new directory.
- Documented: confirmation boundaries and the fact that prompts are not OS
  isolation. Live service and disaster-recovery behavior still needs a
  disposable host drill.

### P1 — Versioning and safe release selection

- Implemented: canonical `VERSION`, SemVer selection, five channels, exact
  pins and per-install settings; existing instances keep legacy behavior.
- Implemented: tag/version/prerelease validation, release notes, checksums and
  direct-dependency SPDX. Independent signatures/attestation remain open.
- Implemented: tests cover channel eligibility, downgrade confirmation,
  GitHub digest/URL checks, unsafe artifacts, interrupted writes and rollback.
  Live GitHub outage and service recovery still need an external drill.

### P1 — Establish a green, reproducible baseline

- Implemented: identity tests no longer require a commit rejected by the
  runner's signing policy; 121 tests and declared-dependency API/runtime
  checks pass locally.
- Remaining: coverage measurement and clean VPS / recovery drills.

### P2 — Reliability, architecture and security

- Extract cohesive subsystems from `bot.py` incrementally, preserving handlers,
  provider behavior and extension contracts.
- Add focused reliability tests for timeouts, retries, shutdown, task
  deduplication and provider failure; do not retry operations with unknown
  external outcomes.
- Review command execution, filesystem boundaries, prompt-injection exposure,
  mod trust, network access and root-helper policy. A prompt is not a sandbox.
- Add reproducible startup, idle/load resource and message-latency benchmarks
  before optimizing.

### P2 — Release engineering and operator experience

- Implemented: tag-gated release workflow, least-privilege permissions,
  Telegram controls and operator documentation.
- Remaining: branch protections and required reviewers need owner authority;
  Alpha workflow execution and production-adjacent recovery need GitHub and a
  disposable host.

### P3 — Broader ecosystem

- Add plugin/peer protocol compatibility tests, additional localization,
  performance comparisons and release SBOMs where supported by the actual
  build and distribution model.
- Evaluate external agent projects only for concrete design comparisons; do
  not copy features or claim competitive benchmarks without verification.

## Iteration 2 — implemented locally

- Added SemVer-aware Stable, RC, Beta, Alpha and Nightly channel eligibility,
  per-install settings, version pins, update pause and downgrade checks.
- Added Telegram controls for channel/release review, update confirmation,
  backups and rollback help. Existing instances remain on their prior updater
  mode until an owner selects a channel.
- Added GitHub Release asset digest and archive validation before staging a
  release update.
- Added private integrity-checked backup archives and out-of-place restore.
- Included only the known external Codex/Antigravity auth and session paths;
  restore contains them under `provider-home/` instead of overwriting active
  credentials.
- Added install-time channel and exact-source-version options, plus a
  tag-validated release workflow, generated notes, checksums and a direct
  dependency SPDX inventory.
- Prepared `0.1.2-alpha.1` as the first Alpha candidate. No tag or release has
  been published; the selected GitHub profile is unavailable in this checkout.
- Verified 121 unit tests, three owner API tests, runtime smoke, shell and
  Python syntax, installer source checks,
  reproducible manifest, archive scope and checksums locally.

## Next acceptance gates

- Validate the release workflow on GitHub with the owner-selected profile;
  test a real disposable Telegram/systemd installation and rollback before
  publishing the Alpha tag.
- Decide and document how to resolve the existing GitHub releases whose
  prerelease flags disagree with their stable SemVer tags; do not silently
  rewrite those releases.
- Add independent release signing/attestation, same-artifact promotion,
  Python 3.11 CI evidence, measured coverage and reproducible performance /
  reliability tests.
- Continue small compatibility-preserving refactors only after establishing
  focused tests for the affected bot/provider behavior.

## Exit criteria for a Stable release

A Stable release is blocked until channel selection and prerelease filtering,
artifact authenticity/integrity policy, existing-install preservation,
upgrade and rollback tests, migration policy, required CI checks, operator
documentation and explicit owner authorization are all implemented and
verified. This roadmap is not itself a release approval.
