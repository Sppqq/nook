# Modernization log

## Iteration 21 — close the Codex CLI startup cancellation race

- Keep the process handle when cancellation arrives during startup. Stop
  the CLI and its descendants before account recovery proceeds.
- Exercise the race by cancelling while a real child is already running
  and the wrapper's process handle has not reached the caller.

## Iteration 20 — localize more of the owner workflow

- Translate server status, model choice and fallback, the task list and
  cancellation confirmation. The Telegram dispatcher test exercises both
  confirmation and repeated cancellation with the production queue.
- Record the live promotion checklist for historical migration, sandbox
  rollback and multi-account quota recovery.

## Iteration 19 — make manual account changes recoverable

- Account imports run under the core login lock after successful device
  authorization. A failed sign-in keeps the previous account and quota
  cache. Other providers keep their current conversation.
- A failed or cancelled account switch restores the previous credential and
  registry contents. The CLI wrapper and its subprocesses stop before the
  restoration. Legacy Codex homes with a local user-private group remain
  usable with their existing directory mode.

## Iteration 18 — exercise the oldest installation and the owner handoff

- Run the original v0.1.0 bot and its declared dependencies before the
  migration in a disposable Linux user service. Exercise the deployed
  candidate's Telegram dispatcher, preview and confirmation of the official
  channel. The fixture uses local release input and a fake Telegram transport.
- Cover owner approval and rollback for a data-only legacy function in the
  actual Bubblewrap runner, using fixture inputs and a simulated approval.
- Translate manual Codex operations, device-login notices and local extension
  controls. Exercise a successful and a rejected device login with test
  credentials, including session state and private backup permissions.
- Run 206 local tests with one optional `age` test skipped. Measured line
  coverage is 29.4% for `bot.py` and 87.8% for `codex_pool.py`.

## Iteration 17 — protect pooled credentials and recovery

- Reserve a Codex account while reading its quota so a probe and a turn
  cannot use the same saved login concurrently. Manual login, account switch
  and account-list quota refresh wait for pool activity to finish.
- Reject hard-linked credential files and registry paths writable by other
  users. Preserve the existing Codex root and alternate-home layout.
- Keep the requested session when the CLI reports a different thread.
  Suppress automatic replay after partial output, malformed stream events,
  completed turns or possible tool effects. Cover these cases through the
  real CLI subprocess and Telegram task dispatch with fixture data.

## Iteration 16 — localize Codex quota and recovery notices

- Use the owner's saved language for the Codex quota panel, low-quota footer,
  pool availability, and pause and resume notices. The Telegram dispatch test
  exercises an unavailable pool account, optional Mercury fallback, and
  the original thread's recovery after a quota check.

## Iteration 15 — verify the exact v0.1.1 update path

- Exercise the historical menu check and confirmation, pinned main archive,
  and worker on Linux user systemd. Reuse the v0.1.1 installation's own
  environment across cutover. The fixture installs its declared base
  dependencies and simulates only the remote download and Telegram traffic.

## Iteration 14 — localize the Telegram update path

- Add an owner-only language switch that preserves existing preferences and
  defaults to Russian for old installations. Translate the main menu, interface
  settings, update screens, scheduled release notifications, and the
  Codex pool's account controls and reset notifications.
- Test persistence, private owner authorization, and the unchanged separate
  downgrade confirmation. Installer, core diagnostics, and mod coverage remain
  incomplete.

## Iteration 13 — prepare the next Nightly candidate

- Exercise the real bot entry point with a pending environment update. It
  records candidate health after Telegram responds and waits for supervisor
  approval before polling or scheduling work. A Telegram startup failure
  leaves the candidate unapproved and starts no work.
- Assign a distinct Nightly version and render release notes that describe
  signed dependency upgrades. The installer test now runs on the candidate's
  own channel. The published Alpha remains available.
- Give successful cold starts time to report health in the disposable
  systemd exercise. A busy runner exceeded the earlier test-only deadline
  and the fixture reverted to its old service as intended.

## Iteration 12 — switch dependencies in a signed update

- Prepare and validate a separate Python environment before stopping the
  service. Verify a full stopped-installation snapshot before exchanging it.
- Use the frozen updater to restore the original environment and source.
  Keep the current systemd unit, channel choice, and local runtime files.
- Scan the resolved base and voice Python dependencies with `pip-audit`.

## Iteration 11 — verify queued Codex recovery

- Exercised the background resume loop with one distant reset and one
  recovered account. It clears the persisted fallback state and resumes
  the same thread. The digest reports that work can continue.
- Codex-only installations keep their configured engine while tasks are
  paused. Automatic Mercury routing requires a configured Mercury key.

## Iteration 10 — Alpha onboarding and contribution guides

- Updated both READMEs and release operations to use the published Alpha.
- Added issue and PR templates with concrete verification and compatibility
  fields. Documented staged migration of a trusted mod's pure logic while
  retaining its existing data, permissions, and rollback path.
- Checked the CLI-generated quota protocol schema against the pool reader
  without reading credentials or making provider calls.

## Iteration 9 — pool recovery and migration interlock

- Corrected Codex pool retry timing and error classification with aiogram
  integration coverage. An available account triggers a prompt retry.
- A queued code update now blocks a simultaneous full migration even if
  the previous update journal already reports success.
- Downloaded and verified the existing public Alpha ZIP and its GitHub
  provenance with the production release client at source commit beef3a4.

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
