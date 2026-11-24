# Nook technical audit

## Current findings

- Iteration 5 introduces staged local extensions. Draft code never loads into
  the Telegram process. Linux namespace isolation, explicit owner approvals,
  and read-only artifact snapshots protect the isolated execution path.
  Secure operation against the service UID itself still depends on OS
  ownership of the broker and the absence of broad sudo grants.
- Codex may have performed a tool action before reporting a provider
  limit. Stream events now gate automatic retry and turn this into an
  explicit review result. Existing scheduled claims remain held when an
  operation fails. Provider session families and transfer context are
  validated separately from request execution.

- Iteration 4 confirmed that the PR and push took place in the same
  repository under the same user. Both check suites failed before jobs
  started. The public data does not justify attributing this to fork approval.
- Scheduled work records durable claims before dispatch. Interrupted or
  ambiguous occurrences remain for operator review. A recurring job stays
  held until its unresolved claim is closed. A provider limit or other
  failed result leaves the claim unresolved and skips automatic replay.
- A real aiogram dispatch test covers owner-only channel switching, rejection
  of group/foreign callbacks, and an empty release channel.
- Mercury previously buffered the full stdout and could exceed its request
  budget within a tool batch or provider wait. Now it retains bounded output,
  constrains the request budget, and rejects repeat attempts when a previous
  shell call timed out. The protection covers identical commands within one
  request and scheduled occurrences across restart. It cannot determine
  whether different commands have the same effect.
- The legacy source updater still trusts GitHub HTTPS and the selected
  `main` revision and has no independent publisher identity. Managed
  channels verify provenance. A prompt and the installed mod registry do
  not isolate arbitrary code from secrets accessible to the service UID.
- The optional root helper now consumes request IDs in a root-owned audit
  journal, rejecting duplicate invocations and anchoring spool writes to an
  open directory. It still treats the service account as fully trusted.
  Existing privileged installations were not changed.
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
- Backups removed implicit export of external provider authorization. SQLite
  uses a transaction-consistent snapshot. File replacement, links, and
  destructive restore targets are refused. Offline capture is required for a
  common consistency point across independent writers.
- Applying an update now checks free space, syncs the rollback journal, and
  preserves a frozen recovery process. Automatic installs must be enabled by
  the owner, and the request path rejects a downgrade without explicit consent.
- Codex could hang beyond its requested timeout while a child process
  retained a pipe. Its full request now has a deadline and bounded
  diagnostics. Codex requests and quota checks and Mercury shell tools share
  process cleanup with a bounded termination sequence.
- A restored installation's project catalog, OpenBot memory and owner API
  now resolve paths from the restored source location. Backups refuse a
  half-applied update and omit queued requests tied to excluded staging.

### Disposable Linux recovery evidence

- Ran the real user-systemd updater from historical `v0.1.1` files to the
  Alpha source, then back to historical files via a synthetic published
  Stable feed. Local memory, a mod, a skill and test authorization survived.
- A failing service process triggered automatic rollback. Terminating and
  restarting the isolated WSL environment during `awaiting_health` verified
  recovery through the installed pre-start guard.
- A 12 MiB tmpfs triggered disk preflight before live source writes. A
  network namespace with no routes triggered staging cleanup. A truncated
  ZIP was rejected.
- The systemd fixture emitted realistic health records and never contacted
  Telegram. The release signature and protected-environment approval still
  need a GitHub Actions run for this repository.

### Coverage and timing baseline

- At `72794e1`, `coverage run --branch --source=. -m unittest discover -s tests`
  reported 28% total, 69% for the updater, 79% for backup and 1% for
  `bot.py`. Tests that execute extracted AST fixtures do not trace the
  original bot file. This measurement is the direct unit-suite coverage.
- Three isolated runs of the bot import and chart smoke program took
  4.04, 3.56 and 3.70 seconds. This measures startup/import and chart work
  in Ubuntu WSL, with Telegram polling and provider requests disabled.

### Iteration 4 controlled resource measurements

Ubuntu 24.04 WSL2, Python 3.12.3, systemd scopes with swap disabled.
Each row used three runtime smoke launches and a 10-second queue load:

| MemoryMax | Import and 3 charts, median | Runtime peak RSS | CPU over 3 launches | Durable jobs/s | Claim p95 |
|---|---:|---:|---:|---:|---:|
| 512 MiB | 4.37 s | 225.7 MiB | 12.38 s | 180.86 | 7.73 ms |
| 1 GiB | 4.42 s | 225.9 MiB | 13.10 s | 219.46 | 5.17 ms |
| 2 GiB | 4.03 s | 225.8 MiB | 12.04 s | 224.75 | 5.03 ms |

Queue peak RSS was 22.9–23.0 MiB. These are workload measurements with
temporary files, no external provider calls and no long-running VPS.
They test memory ceilings for these scenarios alone.

A separate 60-second run under 512 MiB processed 13,952 queue events
at 232.25 jobs/s, with 5.19 ms p95, 9.62 CPU seconds and 23.2 MiB peak
queue RSS. This exercised claim and completion I/O without provider work.

The iteration 4 unit suite passed 149 tests. Branch-aware measurement
reported 38% overall, 15% in `bot.py`, 100% in
`telegram_gateway.py`, 80% in `job_scheduler.py`, and 83% in
`tool_executor.py`. The optional root helper measured 39%. The prior
baselines were 28% overall and 1% in `bot.py`. The 90% core and 95%
critical-security targets remain open.

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

## Historical next-work notes

Track implementation and acceptance gates in [ROADMAP.md](ROADMAP.md).
Priority is to diagnose the single remaining test error, then establish a
reproducible green baseline before changing release selection or updater
behavior. No production system or remote repository was modified.

## Iteration 5 verified status (2026-11-24)

PR #7 remains the delivery branch. The repository/head ownership and write
permission were verified with the owner account; commit `9890457` was pushed
normally. GitHub Actions for that commit passed on Python 3.11 and 3.12
([workflow](https://github.com/Sppqq/nook/actions/runs/45476912057)); the PR
workflow passed on 3.11 and was pending its 3.12 job at last observation.
These current results supersede the earlier historical “write blocked” notes.

Two local stages are now checked. The extension lifecycle adds staged,
content-addressed versions, owner-controlled permissions, isolated execution,
compatibility checks, controlled activation, health checks, rollback and a
bounded history. The second stage extracts provider session selection and
prevents automatic Codex limit failover after observable tool activity.
Validation in disposable WSL `Nook-Test-01a20e56` passes 161 unit tests, 3
owner API tests, runtime and renderer smoke checks, installer/manifest checks,
and locally built archive checksum verification. Branch coverage measured on
the current working tree is 41% overall and 19% for `bot.py`; extension
sandbox/lifecycle coverage is 75%/63%, and `provider_sessions.py` is 100%.
These targets (50%/30%) remain open. The lifecycle stage CI commit above has
green Python 3.11/3.12 jobs; iteration 5 session changes still require their
own push and CI run.

The WSL sandbox was exercised with host file, socket, environment and home
access probes, and with a temporary owner policy in the disposable test
distribution. No VPS endurance, real provider-account load, live Telegram,
production, or release publication test was performed. Isolated execution
depends on Linux Bubblewrap and kernel namespaces and fails closed where
unavailable; compatibility mode remains for existing trusted modules and
must be explicitly hardened by an owner policy. Full privileged service
accounts remain a trust-boundary risk. The local Alpha archive is a build
artifact only; no tag or release was published.

P0 legacy migration is not certified. The updater intentionally refuses
dependency changes and stops on local source conflicts. Exact `v0.1.0` and
`v0.1.1` trees were fetched by pinned tag; their original suites passed (22/81)
in isolated WSL copies. Full upgrade/rollback was not tested. `v0.1.0` has no
`self_update`. See `docs/UPDATES.md`; do not claim universal compatibility.
