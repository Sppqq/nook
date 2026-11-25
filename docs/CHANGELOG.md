# Changelog

## 0.1.2-nightly.20261125 — candidate

- Add optional Codex account pooling with per-account quotas and safe conversation continuation.
- Adopt the updater from the original v0.1.1 Telegram menu and preserve the current channel.
- Stage Python dependency changes in signed upgrades and roll back code and environment if startup fails.

## 0.1.2-alpha.1 — published

- Add Stable, Release Candidate, Beta, Alpha and Nightly channel selection with per-instance settings and SemVer version pins.
- Verify GitHub Release asset digests and source manifests before staging updates; keep explicit downgrade confirmation.
- Add Telegram controls for release review, update checks, pause and confirmation modes.
- Add local integrity-checked backups and restore only to a new directory.
- Add tag-validated release publishing, notes, checksums and a direct-dependency SPDX inventory.

## 0.1.1 — 2026-11-07

- Gemini conversations now resume with their saved CLI conversation ID. Tool progress appears while a request runs, without tool arguments.
- Switching between Codex, Gemini and Mercury rejects conversation IDs from another provider, including stale paired conversations.
- Mercury shows Continue only when the agent reaches its tool-step limit.
- Codex account login now shares the account-switch guard, waits for active tasks to finish, preserves Mercury/Gemini conversations, and refreshes cached limits. The optional `codex-auth` binary is resolved from PATH or the user's npm global directory.
- Gemini streaming retains capacity-error detection and process cleanup on timeout and cancellation.

The account-switching mod still needs a separately installed compatible `codex-auth` utility and is disabled by default. Private runtime configuration, provider keys and owner-specific prompts are not included.
