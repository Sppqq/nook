# Nook technical audit

## Current findings

- PR #7 is open against `dev` at `8e70a28`. Its two Checks runs reported
  `action_required` with no jobs in the public API. GitHub reports two
  historical releases and marks both as prereleases.
- The previous workflow could publish Stable from a tag push. The replacement
  uses manual dispatch on protected `main`, an attested candidate and a
  separate Stable environment gate. The repository settings still require
  owner setup and live verification.
- The previous bootstrap downloaded `main` before selecting a channel. The
  installer now resolves and verifies the published release first. Both the
  installer and updater require the signed workflow provenance for releases.
- Nook runtime GitHub access remains unconfigured in this clone. The desktop
  CLI reports an invalid token. Public repository reads and local Git identity
  verification succeeded independently.

## Historical Stage 0 and iteration 2 baseline

The subsequent sections retain the 2026-11-24 audit snapshots as history.
They describe repository states before the changes listed above.

## Repository and release state

- Local branch at inspection: `dev`, tracking `origin/dev`; the checkout is
  shallow and exposes one commit (`6cb719e`). There are no local tags.
- Working tree was clean before creating `feature/road-to-10`. No remote
  branch, release, or repository settings were changed.
- GitHub currently lists `v0.1.0` and `v0.1.1`; both are pre-releases.
- `tools/build_release.py` hard-codes version `0.1.1`; `MANIFEST.json` repeats
  the generated version. `CHANGELOG.md` records `0.1.1`.
- CI has a Python 3.11/3.12 test matrix, smoke tests and source bundle checks.
  A dependency-audit workflow is scheduled weekly. No release-publishing
  workflow or channel-selection implementation was found in the inspected
  files.

## Existing capabilities observed

- `bot.py` is the Telegram entry point and contains provider execution,
  conversation/task handling and many UI/administration functions.
- `mods.py` loads extensions; `mods/self_update.py` integrates update requests
  and waits for active work before reserving maintenance lanes.
- `nook_update.py` handles pinned-source download, manifest/path checks,
  local-change conflict refusal, backups, persistent transaction state,
  rollback, systemd supervision and startup health checks.
- `docs/UPDATES.md` documents which runtime files are preserved and where
  updater support is limited.
- `tools/build_release.py` builds source bundles and checksums from Git's
  explicit file list; CI verifies the manifest remains reproducible.
- Existing security material documents Telegram owner checks, trusted Python
  mods, root-helper trust, peer authentication, credential handling and the
  limits of the updater's checksums.
- Tests cover installer boundaries, owner authorization, peer replay/expiry,
  update conflicts and rollback, release composition, provider process
  cancellation and settings behavior.

## Prioritized findings

### P0

No P0 issue was established in this preliminary pass. That is not evidence
that a complete security audit found none.

### P1

1. **Release version is not a single source.** `tools/build_release.py`
   hard-codes `VERSION`; the manifest is generated from it, while the
   changelog and GitHub tags/releases are separate. A future version change
   can drift unless release validation ties these together.
2. **No channel-aware update selection was observed.** The current updater
   checks a source revision on `main`; it does not select GitHub Releases by
   Stable/RC/Beta/Alpha/Nightly, compare prerelease versions, or store a
   channel per installation. Do not add a silent channel migration.
3. **Updater authenticity has a documented trust limitation.** The manifest
   and checksums detect inconsistency, not a compromised upstream source.
   The updater trusts GitHub over HTTPS; no independent release signature or
   attestation was observed.
4. **Baseline test suite is not green in this environment.** With declared
   dependencies installed in an isolated virtual environment, 90 of 91 tests
   passed; `test_git_identity_and_credentials_are_local` errored while creating
   a temporary commit. Diagnosis showed the execution environment enforces
   signed commits under its own author identity, conflicting with this test's
   synthetic `test-agent` author. The test now verifies both Git author and
   committer identities directly, without producing a commit; the complete
   updated suite passes.
5. **Current verification does not establish operational readiness.** The
   reviewed CI does not run coverage, type/lint checks, full clean-install
   matrix, real update/rollback on a disposable installed service, or
   performance benchmarks.

### P2

1. **Large responsibility surface in `bot.py`.** The entry point contains
   Telegram transport/UI, provider execution, task lanes, settings, node
   operations and other concerns. Refactor incrementally behind compatibility
   tests; a wholesale split would risk existing mod and handler behavior.
2. **Updater intentionally has a narrow compatibility envelope.** It refuses
   dependency changes and source removals and requires matching update API.
   This is safer than silently mutating environments, but means dependency
   upgrades and migrations need a separately tested, owner-approved path.
3. **Checksums are not signatures.** Release output includes SHA-256 sums, but
   no independent signing/provenance step was observed.

### P3

1. No measured RAM/CPU/latency benchmarks or coverage numbers were found in
   the inspected artifacts.
2. Existing public operator documentation is primarily Russian/English
   mixed; a complete English/Russian operations and migration set remains
   future work.
3. Competitor comparison and independent review have not been performed.

## Baseline checks

| Check | Result |
|---|---|
| `python3 -m unittest discover -s tests -v` using system interpreter | 89 pass, 2 errors; `aiohttp` missing and Git identity test errors |
| Same suite in `/tmp/nook-upgrade-audit-venv` with `requirements.txt` | 90 pass, 1 error; test commit rejected by environment signing policy (test adjustment in progress) |
| Repository CI configuration review | Python 3.11/3.12; tests, API/runtime smoke checks, release builder, shell syntax and manifest check |
| `gh release list --repo Sppqq/nook` | `v0.1.0` and `v0.1.1`; both marked pre-release |
| Local Git history/tags | Shallow checkout, one visible commit, no local tags |

## Changes and verification in this branch

- Added `VERSION` as the canonical SemVer source; the release builder now
  validates it and uses it for archive names, bundles and manifests.
- Installer validation rejects runtime paths in the release manifest and
  ignores private runtime paths during source-tree checks and copying. This
  prevents local memory, credentials, state and provider sessions adjacent to
  a checkout from being treated as distributable source.
- Updated the Git identity test to check configured author and committer
  identities without requiring the restricted environment to create a signed
  commit.
- `python -m unittest discover -s tests -v`: 93 tests passed in the isolated
  environment with declared dependencies.
- `bash -n bootstrap.sh install.sh setup/install-root.sh`,
  `bash install.sh --check`, owner API tests (3), runtime smoke test and
  release-manifest reproducibility check passed.

No coverage, performance, installation, live Telegram, destructive recovery,
or independent penetration-test result is claimed here.

## Iteration 2 — implementation and current verification

The following baseline findings are addressed in this branch:

- `VERSION` is the canonical SemVer input; the builder and installer verify
  that it matches the generated manifest.
- Added Stable, RC, Beta, Alpha and Nightly release filtering with SemVer
  precedence, per-installation settings, exact version pins, and explicit
  downgrade confirmation. Installations without channel settings retain the
  legacy `main` behavior until their owner opts in.
- Added a Telegram mod for channel selection, release/change review, update
  checks, update pause and confirmation modes, local backup creation and
  rollback instructions. Initial installation offers channel selection and an
  optional pin to the exact checked source version.
- Added GitHub Release asset digest, URL, ZIP, tag-commit, version-manifest and
  file-hash checks before a channel update can be staged.
- Added private local archives with per-file hashes, archive validation and
  restore only into a new directory. The archive includes an explicit
  allowlist of external Codex/Antigravity credentials and Codex session files;
  restore keeps those under `provider-home/` for owner-reviewed placement and
  never overwrites active credentials.
- Added a tag-gated release workflow, generated notes, checksums and a
  correctly scoped SPDX inventory of direct pinned Python requirements.

The first Alpha release is prepared as `0.1.2-alpha.1` in `VERSION` and
`CHANGELOG.md`; it has **not** been tagged or published. The existing public
tags/releases are not modified. The previously observed `v0.1.0` and `v0.1.1`
are marked prerelease in GitHub despite stable SemVer tags, so the new strict
channel selector excludes them rather than guessing their intended channel.

Current local verification: 121 unit tests pass in an isolated environment
with `requirements.txt`; shell syntax checks, `bash install.sh --check`,
Python compilation, reproducible manifest generation, release archive member
checks and SHA-256 verification pass. The archive excludes private runtime
data. The owner API integration tests (3) and runtime smoke test pass. CI runs
the smoke test as a module to preserve the installed dependency path. A real
Telegram/systemd update,
fresh-OS install, recovery drill, cross-version matrix, performance run,
coverage measurement and independent signature verification have not been
performed. GitHub writing remains blocked because the Nook-selected profile
is unavailable in this checkout; the authenticated `gh` CLI is not used as a
substitute.

### Remaining risks and limitations

- Release digests and manifests are integrity checks rooted in GitHub HTTPS
  and authorized maintainers, not a detached signature or independent
  provenance attestation.
- The release workflow is tag-triggered; branch protections and required
  reviewers still need owner-level GitHub configuration.
- Same-artifact channel promotion is not implemented. Dependency migrations,
  source deletions and data migrations remain outside the automatic updater.
- A complete backup contains credentials and must remain private. Restore is
  out-of-place; `.venv` is rebuilt. This is not a tested production cutover.
- The repository's large `bot.py` responsibility surface remains a
  high-risk architectural constraint; no broad refactor was attempted.

## Scores

No numeric overall or category scores are assigned at baseline: the requested
evidence across the full system has not yet been collected. In particular,
security, performance, compatibility across independently modified
installations and competitor comparisons require further work and/or
independent verification.

## Next work

Track implementation and acceptance gates in [ROADMAP.md](ROADMAP.md).
Priority is to diagnose the single remaining test error, then establish a
reproducible green baseline before changing release selection or updater
behavior. No production system or remote repository was modified.
