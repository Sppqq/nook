# Changelog

## 0.1.2-alpha.2

- Switch Codex 6.2 Sol and Astra directly from the Telegram quota screen. Menu navigation keeps one active message.
- Show remaining quota separately from personal account budgets. Five-hour reset alerts require at least 90% usage.
- Add opt-in Codex account pooling, usage budgets, conversation recovery, and an English interface preference.
- Update an original v0.1.1 install from its existing Telegram menu when dependencies match. The one-time signed SSH migration from v0.1.0 can select the newest release in a chosen channel.
- Stage dependency changes for managed upgrades and restore the previous version and environment if startup fails.

## 0.1.2-nightly.20261126 — candidate

- Switch between Codex 6.2 Sol and Astra directly from the Telegram quota screen. Navigating the menu keeps a single active message.
- Show actual remaining quota separately from personal Codex pool budgets. Send five-hour reset alerts only after at least 90% use.
- Select an update channel during one-time SSH migration from the original installer. The migration uses the newest eligible signed release.

## 0.1.2-nightly.20261125 — candidate

- Add optional Codex account pooling with per-account quotas and safe conversation continuation.
- Adopt the updater from the original v0.1.1 Telegram menu and preserve the current channel.
- Stage Python dependency changes in signed upgrades and roll back code and environment if startup fails.
- Offer English for the main menus, release updates and Codex account controls through Telegram settings.

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
