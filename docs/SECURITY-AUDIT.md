# Maintainer security review — 2026-11-07

This is a review by the assistant maintaining Nook, not an independent audit or a security certification. Scope: the public Nook source, Telegram owner and guest boundaries, installer/bootstrap, source updater, peer transport, optional owner API, credential handling and Python dependencies. It does not cover the owner's VPS, other projects, provider infrastructure, or the original private Domovoy deployment.

## Fixed findings

- **Administrative callbacks outside the owner's private chat.** Callback authorization previously checked the sender without requiring a matching private chat. An owner pressing an administrative button in a group could cause actions or disclose responses there. Callbacks now require a private chat whose ID matches the sender; owner checks still apply. Guest charts are private too. This was not an arbitrary user's owner-ID bypass.
- **Fail-open initial pairing configuration.** An unset owner together with an empty configured pairing code could accept an empty start argument. Pairing now requires a nonempty code, a private matching sender/chat and constant-time comparison. Normal installer deployments already generate a random code and set the owner explicitly.
- **Token disclosure in CLI errors.** A network exception from `bin/tgapi` could include the complete Telegram request URL, which contains the bot token. Unexpected failures now print only the exception class. Telegram JSON output retains token redaction. Tests cover token-bearing network and malformed-response failures.
- **Installer source verification gaps.** A manifest previously verified listed files without rejecting additional executable source. Verification now checks the complete distributed file set, canonical relative paths and symlink ancestors. Bootstrap downloads have compressed/extracted size and entry limits, reject duplicate destinations and refuse nonregular archive entries. These checks detect malformed downloads; they cannot authenticate a compromised upstream installer.
- **Service-resume path validation.** The resume path now passes the same control-character, quote and backslash checks before generating a systemd unit.
- **Peer expiry and concurrent quotas.** Signed timestamps now require actual integers, excluding NaN and infinity. SQLite serializes quota checking and insertion, preventing simultaneous receivers from exceeding the hourly limit. The inbox database is explicitly mode 0600.
- **Optional API fail-closed credentials.** An existing empty or malformed bearer-token file now prevents the API from starting. The API remains disabled by default and bound to loopback when enabled.
- **Runtime file permissions.** The core sets umask 077 even when launched manually, before importing local runtime modules.
- **Dependencies.** Updated aiogram 3.20.0 → 3.31.0 and aiohttp 3.11.18 → 3.14.5. Added explicit Pillow 13.0.0 and optional filelock 4.2.9 constraints after auditing their resolved transitive versions. Python 3.11+ is now required because fixes are unavailable for the resolved older Python 3.10 dependency line. Ubuntu 24.04 / Debian 12+ are recommended. Upstream examples: [aiohttp header/trailer memory exhaustion advisory](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-w2fm-2cpv-w7v5), [Pillow security advisories](https://github.com/python-pillow/Pillow/security/advisories), [filelock security advisories](https://github.com/tox-dev/filelock/security/advisories). A package advisory does not establish that Nook exposes every affected code path.

GitHub Actions are pinned to commit IDs. A weekly dependency scan covers both normal and optional voice requirements; Dependabot proposes pip and Actions updates. These checks report known advisories and do not prove the absence of vulnerabilities.

## Validation and reproducibility

Run with Python 3.11+ in an isolated environment:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python setup/nook-api/test_api.py
python tools/smoke_runtime.py
python tools/build_release.py
bash -n bootstrap.sh install.sh setup/install-root.sh
bash install.sh --check
python -m pip install pip-audit==2.10.1
python -m pip_audit -r requirements.txt -r requirements-voice.txt
```

On 2026-11-07, all 69 tests passed, including ten security regression tests. A full pip-audit resolution of normal plus optional voice requirements on Python 3.11 checked 47 packages, skipped none and reported no known vulnerabilities. These are time-specific database results. Three additional real HTTP tests of the optional owner API passed (authentication/origin rejection, validation, delivery/idempotency and restart handling). The runtime smoke check imports the real bot and registered handlers, closes its session, and renders/verifies a PNG without polling Telegram or calling a model provider. It does not verify every feature against Telegram or run a complete installation/update/recovery cycle on a fresh VPS.

Bandit findings were reviewed as triage, not blindly suppressed. The scan flagged intentional subprocess/exec administration code, HTTP monitoring and urllib use. Such capabilities are part of Nook's trust model and require care at each input boundary. A credential-pattern scan of the pre-review reachable Git history (103 unique blobs) found no matches for the release builder's key/token patterns; this is not an exhaustive secret-detection guarantee.

## Remaining trust and deployment risks

- **Installing the optional root helper grants the bot account full root capability.** Passwordless maintenance commands and a user-controlled approval spool mean Telegram prompts do not isolate a compromised Linux account. This is now stated explicitly in setup and SECURITY.md. The helper remains a separate opt-in; this review did not change an existing server's sudoers policy.
- **Models and mods can execute code as the bot user.** Treat documents, websites, logs and peer messages as potential prompt injections. Model instructions, OpenBot risk/trust scores and UI confirmation prompts are not OS isolation. Use a dedicated account/server, minimum permissions and backups.
- **Peer SSH credentials grant the remote user's SSH privileges.** HMAC authenticates messages, not the safety of their text. Each pair needs its own key. No public network listener is introduced. Review transport credentials and the remote account's scope.
- **Supply-chain trust remains.** HTTPS, pinned commits and checksums do not independently authenticate a compromised GitHub/PyPI account. Manifests are integrity checks, not independent signatures. Transitive dependencies are resolved at install time, not a complete hash-locked supply chain.
- **The updater's recovery is code recovery.** It preserves runtime/configuration and refuses conflicts or dependency changes; it cannot undo arbitrary side effects of trusted code or replace full data backups. Its own supervision requires working systemd and sufficient resources.
- **Upgrading existing installations needs care.** This release changes Python/dependency requirements. The conservative updater deliberately refuses this transition. Review and migrate the existing environment with backups; never rerun the fresh installer over existing data, reset local modifications, or manually bypass the baseline guard. The private Domovoy instance has not been replaced by this public release.

See [SECURITY.md](../SECURITY.md), [peer setup](PEERS.md), and [update/recovery behavior](UPDATES.md). No independent penetration test, full host hardening audit or provider-agent sandbox audit was performed.
