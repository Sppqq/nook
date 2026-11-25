# Release and update-channel policy

`VERSION`, `MANIFEST.json` and the `CHANGELOG.md` release heading must agree
with the tag `v<VERSION>`. The enabled publication path is a manual
**Verified release** dispatch on protected `main` in `Sppqq/nook`. Pushing
a tag does not launch the workflow.

1. Run the workflow with Publish off to prepare the branch or an existing
   tag. It checks Python, the installer, runtime smoke tests, checksums and the
   tracked manifest. Download the resulting candidate from the workflow run.
   GitHub must first see this workflow on the default branch to offer manual
   dispatch.
2. After review, an authorized maintainer creates the exact version tag on a
   commit reachable from protected `main`. Verify the appropriate promotion
   predecessor when dispatching for a tag.
3. Dispatch from `main` with the tag and Publish on. A separate job attests
   the same checked artifacts via Sigstore. The publish job only downloads
   artifacts, checks the tag still resolves to the prepared revision, verifies
   integrity/provenance and publishes through the channel's environment.

`0.1.2-alpha.1` is the one bootstrap Alpha allowed without an existing
Nightly. Future tagged candidates follow Nightly → Alpha → Beta → RC →
Stable within the same major/minor/patch. Set `promote_from` to the published
tag from the previous stage. The workflow verifies its attested ZIP and
requires its commit to be an ancestor of the candidate, with identical Git
paths, file modes and contents apart from VERSION, the generated manifest and
changelogs. Each promotion builds and tests a new versioned archive.

## Repository setup by the owner

See [release operations](docs/RELEASE_OPERATIONS.md) for the verified PR
state, permission setup, and clean-host acceptance steps.

- Protect `main` with pull-request review and required CI checks. Restrict
  changes to the release workflow to trusted maintainers.
- Create a ruleset for tags `v*`, restricting creation to release
  maintainers and disallowing tag updates/deletion in normal operation.
- Create environments `release-prerelease` and `release-stable`, with
  deployment branches limited to `main`. Require an independent reviewer
  for Stable and enable prevention of self-review. Add environment secret
  `STABLE_RELEASE_ENABLED` with value `true` only to `release-stable`.
  The Stable job fails closed when the secret is absent. Test approval and
  rollback on a disposable host before approving the first Stable run.
- Consider GitHub immutable releases for future publication so that the
  server locks each published tag and its assets. Historical releases keep
  their original tags, metadata and files.

No release job uses a maintainer PAT. Verification has `contents: read`,
attestation adds `id-token: write` and `attestations: write`, and only the
environment-gated publisher has `contents: write`.

## Delivery and channel eligibility

Installers and managed channel updates use an official release ZIP and
`provenance.jsonl`. GitHub API digests guard both downloads. GitHub CLI
verifies the Sigstore bundle against the ZIP bytes, the Nook release
workflow on `main`, and a GitHub-hosted runner. The archive binds its version
and source commit to the tag and its manifest checks each source file.
Install/update hosts need a GitHub CLI that supports these attestation flags
and access to Sigstore trust metadata. No user GitHub login is used.

| Channel | Example | Also eligible |
|---|---|---|
| Stable | `1.2.0` | |
| RC | `1.2.0-rc.1` | Stable |
| Beta | `1.2.0-beta.1` | Stable, RC |
| Alpha | `1.2.0-alpha.1` | Stable, RC, Beta |
| Nightly | `1.2.0-nightly.20261124` | Stable, RC, Beta, Alpha |

Version selection compares major/minor/patch first. Within one version
series, promotion order takes precedence across channels and SemVer orders
revisions within each channel. Selection excludes drafts, inconsistent
metadata and releases without the required assets. Choose a channel before
downloading. `--version` installs and pins an exact published release.
`--dev` explicitly installs a development checkout or snapshot. Older
instances with no channel selected keep their existing `main` update path
until the owner chooses a channel. See [update and recovery](docs/UPDATES.md).

`SBOM.spdx.json` lists direct pinned Python dependencies from the
requirements files. It does not list transitive packages, OS dependencies or
provider tools.
