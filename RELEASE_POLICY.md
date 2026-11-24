# Release and update-channel policy

## Current process

`VERSION` is the canonical SemVer version. A release tag must be `v` plus the
exact value of that file, and `MANIFEST.json` must carry the same version.
Pushing a matching version tag is the maintainer's explicit authorization to
run the release workflow. The workflow runs the test and smoke checks before
publishing source artifacts to GitHub Releases. It cannot run for a non-tag
ref. Stable releases are not marked as prereleases; alpha, beta, RC and
nightly tags are.

The workflow uses the GitHub-generated asset SHA-256 digest, and the updater
refuses missing/mismatched asset digests, unexpected download URLs, unsafe ZIP
members and invalid per-file manifest hashes. This trusts GitHub's HTTPS/API
and authorized maintainers. It is not a detached signature or a guarantee
against compromised maintainer credentials or GitHub.

`SBOM.spdx.json` records exact direct Python requirements from the two
requirements files. It does not claim to inventory transitive dependencies,
provider CLIs, operating-system packages or runtime-installed tools.

## Version channels

Supported versions:

- Stable: `1.2.0`
- Release Candidate: `1.2.0-rc.1`
- Beta: `1.2.0-beta.1`
- Alpha: `1.2.0-alpha.1`
- Nightly: `1.2.0-nightly.20261124`

Stable selects only stable tags. RC includes stable and RC releases. Beta
includes stable, RC and beta. Alpha includes those plus alpha. Nightly includes
all valid channels. Invalid prerelease identifiers, drafts and release/tag
metadata mismatches are excluded. SemVer precedence is used, not publication
date or the GitHub Latest marker.

Existing installations with no channel setting retain the previous `main`
source-update behavior. New installs choose Stable by default. A selected
channel, exact version pin, pause flag and update confirmation mode are stored
per installation in `spool/updates/settings.json`. Changing channels never
installs or downgrades a version; installation remains a separate explicit
action. A pin that does not match the selected channel is rejected at check
time, not silently cleared.

Automatic checks are off by default. `notify` reports a verified update;
`confirm` stages one and offers an install button. Both still require explicit
confirmation, idle task lanes, backup and updater health checks. Automatic
security-only or unattended channel installation is not supported.

## Promotion and Stable gate

Each channel tag is built and tested independently. Same-artifact promotion
between channels is not yet automated; maintainers must not describe a newer
tag as a promotion of an already-tested artifact unless its commit and bytes
are demonstrably identical. Stable requires a matching non-prerelease SemVer
tag and passing release workflow. Before adding a Stable tag, maintainers must
also verify migration and rollback on a disposable installation, review
compatibility notes, confirm no known critical unfixed issue, and obtain owner
approval. GitHub branch protection and required reviewers are repository
settings and must be configured by an authorized owner.

## Rollback

The updater journals source writes and restores the previous managed source
after failed health checks. This does not roll back arbitrary data migrations
or external side effects. Keep independent backups. Manual rollback is
documented in `docs/UPDATES.md`; full instance backups can be verified and
restored into a new directory with `bin/nook-backup`.
