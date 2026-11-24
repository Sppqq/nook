# Modernization log

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
