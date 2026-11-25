# Modernization log

## Iteration 8 — existing Telegram updater

- Reused the frozen v0.1.1 worker for first adoption from main. The startup
  guard joins the original health check or restores an interrupted update.
- Verified the exact tag and new artifact with a disposable user systemd
  service. Fixed installer checks in linked Git worktrees.

## Iteration 7 — Codex account pool

- Started from main after #7 and #8 merged. Ported opt-in quota routing,
  budgets, reset notices and optional warm-up from the private deployment.
- Included saved logins in opt-in provider backups and checked that a quota
  rejection resumes the same thread with no global credential switch.

## 2026-11-24 — Iteration 5

- Added staged Python mods/tools and text skills, with content-addressed
  versions, root-owned approval policy, API v1 health checks, Telegram
  controls and automatic rollback after failed invocation. Optional
  legacy hash pinning preserves the existing `setup(m)` interface.
- Verified the new sandbox under an unprivileged user in Ubuntu WSL using
  real Bubblewrap. Host files, the host loopback listener and the fixture
  environment secret were unavailable from the extension.
- Exercised an actual root-owned policy in disposable WSL. Activation was
  denied without approval, a broken second revision rolled back to the
  first, and policy revocation prevented further calls. The full stage
  passed 157 tests and 3 owner API checks.
- Extracted provider-compatible session selection and kept the existing
  session file formats. Added real CLI fixture tests for Codex tool/limit
  events and Antigravity conversation resume. Mercury already has direct
  runtime coverage with an HTTP fixture and actual local subprocesses.

## 2026-11-24 — Iteration 4

- Moved Telegram identity checks into `telegram_gateway.py` and scheduled
  queue transitions into `job_scheduler.py`. Preserved the old JSON queue
  format and `later` commands and added explicit unresolved-claim review.
- Tested aiogram dispatch through installed channel callbacks and the queue
  through concurrent workers, the real CLI and an interrupted journal commit.
- Confirmed the two GitHub check suites still report `action_required` before
  jobs. Added an owner runbook, and taught the installer/updater to
  distinguish an outdated GitHub CLI without relaxing provenance checks.
- Extracted the Mercury shell executor and added real subprocess tests
  for timeout, bounded capture and replay protection, plus direct runtime
  checks for provider wait and persisted sessions.
- Added a disposable Linux benchmark for startup, CPU, peak RSS and
  sustained durable queue work.
- Made the optional root helper claim request IDs once in root-owned
  storage, anchored its spool I/O to a directory descriptor, and tested
  concurrent consumption and unsafe links without running root commands.
- Ran the WSL benchmark under 512 MiB, 1 GiB and 2 GiB cgroup limits
  with swap disabled. Also verified that the official GitHub CLI 2.45.0
  is rejected by the Sigstore capability check.
- Kept claims unresolved when the actual scheduled provider run reports
  a failed or uncertain operation, including the Codex limit path that
  normally queues a retry for interactive tasks.
- Passed 149 tests, 3 owner API checks, real chart rendering, installer
  checks, reproducible Alpha composition and local actionlint. Direct bot
  coverage reached 15% and the measured total reached 38%.

## 2026-11-24 — Iteration 3

- Restored the exact PR #7 base. The attached 108-file source archive
  matches the branch tip byte for byte. Created a verified local Git bundle.
- Split release preparation, signing and environment-gated publishing.
  Bootstrap and the updater verify the signed release archive, workflow
  identity, GitHub asset digest, tag commit and source manifest.
- Made the installer resolve the published release before opening the
  installation wizard. Added an explicit development-source path.
- Checked the public GitHub state. Existing Runs returned `action_required`
  before jobs were created. Local actionlint passed. No release was published
  and no repository settings or working instance were modified.
- Added SQLite WAL backup and restore, offline locking, optimistic mutation
  checks for other files, owner-only restore modes, optional age encryption,
  configurable limits and rotation after successful verification.
- Ubuntu 24.04 passed all 11 backup tests, including a live WAL with an
  uncommitted transaction, restoring a customized instance, and an age
  encrypt/verify/restore round trip with a disposable key.
- Added a crash-durable journal, frozen updater startup recovery, low-space
  preflight, explicit downgrade approval, and owner-controlled unattended
  upgrades. New configurations default to off.
- The Ubuntu suite passed 131 unit tests, 3 API tests, simulated runtime
  checks and the Alpha archive checks. Local actionlint passed and pip-audit
  reported no known advisories for the two requirement files at run time.
- Extracted bounded process-group teardown and diagnostics to
  `agent_process.py` in the first incremental runtime refactor.
- Ran the standalone systemd drill in a disposable Ubuntu WSL instance.
  Historical-source upgrade, synthetic Stable return, failed launch and
  rollback, and recovery after environment restart each succeeded.
  Separate filesystem/network drills covered disk capacity, disconnected
  networking and a truncated ZIP.
- Removed hard-coded instance paths from the restored project/owner tools
  and OpenBot memory. Backups now refuse an unfinished update and exclude
  pending request files that depend on ephemeral staging.
- Recorded the first branch-aware coverage and smoke timing in the current
  audit, including the weak direct coverage of the main Telegram runtime.

## Historical baseline — 2026-11-24, iteration 2

Working branch: `feature/road-to-10`. No production instance or remote GitHub
resource was changed. A file-level backup of the touched implementation and
test files was created under `/tmp/nook-modernization-20261124.*` before edits.

Implemented in the working tree:

- Added semantic-version ordering and explicit Stable, RC, Beta, Alpha and
  Nightly eligibility in `release_channels.py`. More experimental channels
  include more stable versions; Stable excludes all prereleases.
- Added per-instance update settings in `spool/updates/settings.json`.
  Existing installs without settings stay on the old `main` behavior; new
  installs default to Stable. Channel changes do not install or downgrade.
- Added release selection from GitHub Releases. Selected updates require the
  exact expected GitHub asset URL, GitHub's SHA-256 asset digest, a resolved
  tag commit, safe ZIP members, matching manifest version and file hashes.
  Update plans are invalidated if the channel, pin or pause state changes.
- Added downgrade detection and a second Telegram confirmation step.
- Added Telegram update management in `mods/update_manager.py`: channel
  selection, releases, pins, changelog, update checks, pause, notification or
  confirm-before-install schedule, local backup, and rollback instructions.
- Added `backup_manager.py` and `bin/nook-backup`: private archives with a
  per-file SHA-256 manifest, archive validation and restore only to a new
  directory. `.git`, `.venv`, staged downloads and previous backup archives
  are excluded. The archive also includes an allowlist of external Codex and
  Antigravity credential/session files under `provider-home/`; restore never
  overwrites active credentials.
- Added install flags `--channel` and `--version`. Bootstrap can fetch an
  exact `v<version>` tag; the installer requires it to match `VERSION` and
  `MANIFEST.json`.
- Added tag-triggered release workflow, generated release notes, checksums and
  a SPDX 2.3 inventory of direct pinned Python requirements. The SBOM does
  not claim to include transitive or operating-system packages.

Final local validation for this iteration: 121 unit tests and three owner API
tests pass in an isolated environment with declared dependencies; the runtime
smoke test passes via module execution. Shell syntax, Python compilation,
`bash install.sh --check`, manifest reproducibility, archive composition and
all generated SHA-256 sums pass. The initial-install wizard now offers the
channel and an optional pin to the exact verified source version. The first
Alpha candidate is `0.1.2-alpha.1`; it is prepared in `VERSION` and the
changelog but is not tagged or published.

Known limitations: no detached artifact signature or GitHub attestation is
configured; release digest trust is rooted in GitHub HTTPS/API and authorized
maintainers. Same-artifact promotion is not implemented. Verified restore is
out-of-place only; `.venv` is intentionally rebuilt. No active production
service is available here for Telegram or systemd update/recovery testing.

GitHub access: `bin/github-api /user` failed because this checkout has no
`secrets.env` profile; `gh auth status` showed a logged-in CLI identity. Per
the repository GitHub skill, do not use that identity as a substitute for the
selected Nook profile. Local commits, pushes, PR creation and release
publication remain blocked until the profile is connected and verified.

Still pending: validate the workflow on GitHub; exercise a disposable
Telegram/systemd install and rollback; independent signature or attestation;
coverage and performance measurements. Existing GitHub releases are
prereleases despite stable SemVer tags and are excluded by the strict channel
rules. No attempt was made to rewrite them.

## Iteration 5 — verified local extension lifecycle and session recovery

- Added `extension_sandbox.py` and `local_extensions.py`: content-addressed
  SemVer staging, isolated Linux execution, API/compatibility probes,
  owner-rooted permissions, explicit activation, health checks, rollback and
  bounded local history. Added owner-only Telegram controls and
  `docs/LOCAL_EXTENSIONS.md`. Existing trusted modules retain legacy loading
  unless an owner enables hash policy.
- Extracted provider-compatible session selection to `provider_sessions.py`.
  Session transfer uses a bounded prior answer and rejects incompatible twin
  IDs. Codex limit handling no longer automatically replays a task after a
  tool event indicates that side effects may have occurred.
- Verified 161 unit tests, 3 API tests, runtime and renderer smoke, installer
  source checks, release archive scope and checksums in disposable WSL.
  Measured branch coverage: 41% total, 19% `bot.py`, 75% sandbox, 63% local
  extensions, 100% provider session helper.
- Commit `9890457` is on PR #7. Its Python 3.11/3.12 Actions workflow passed;
  the PR-specific Python 3.12 job was pending at the last status check.
  Session changes in this iteration have not yet been pushed or checked by CI.
- No active instances, production data or credentials were changed. No Alpha
  tag/release or provenance claim was created. Long-duration/VPS and real
  provider-account testing remain unavailable.

## Legacy migration check

P0 migration remains open. The initial updater still refuses dependency
changes and local conflicts; this iteration adds a separate transaction path
without weakening those checks.

## Iteration 6 — legacy migration groundwork (in progress)

- Added `legacy_migration.py`: exact historical commit recognition, source
  hash validation, conflict-safe source planning, signed-release fetch,
  complete verified snapshots, side-by-side staging, a separate venv,
  install-state initialization, atomic directory exchange, journal recovery,
  startup health checks and an independent recovery CLI.
- Exact v0.1.0 and v0.1.1 Git archives were staged in disposable WSL copies;
  the plans touched 85 and 72 managed files respectively and retained a
  private runtime fixture. All 174 unit tests and 3 API tests pass; runtime,
  installer, bootstrap and manifest checks pass. Measured branch coverage is
  52% overall, 19% in `bot.py`, and 58% in `legacy_migration.py`. Thirteen focused
  tests cover no-space, failed health check, atomic switch and simulated loss.
- Still open: deterministic Telegram bootstrap for v0.1.0, invocation through
  v0.1.1's existing self-updater, and the isolated systemd/dependency/network/
  power-loss compatibility matrix. P0 is not complete and no live service was
  changed. Commit `9bf3f79` is on PR #7; both Actions runs passed:
  [45493890673](https://github.com/Sppqq/nook/actions/runs/45493890673) and
  [45493878506](https://github.com/Sppqq/nook/actions/runs/45493878506).
