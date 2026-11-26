# Changelog

## 0.1.2-alpha.2

- Switch Codex 6.2 Sol and Astra directly from the Telegram quota screen. Menu navigation keeps one active message.
- Show remaining quota separately from personal account budgets. Five-hour reset alerts require at least 90% usage.
- Add opt-in Codex account pooling, usage budgets, conversation recovery, and an English interface preference.
- Update an original v0.1.1 install from its existing Telegram menu when dependencies match. The one-time signed SSH migration from v0.1.0 can select the newest release in a chosen channel.
- Stage dependency changes for managed upgrades and restore the previous version and environment if startup fails.

## 0.1.2-nightly.20261126

- Switch between Codex 6.2 Sol and Astra directly from the Telegram quota screen. Navigating the menu keeps a single active message.
- Show actual remaining quota separately from personal Codex pool budgets. Send five-hour reset alerts only after at least 90% use.
- Select an update channel during one-time SSH migration from the original installer. The migration uses the newest eligible signed release.

## 0.1.2-nightly.20261125

- Add an opt-in Codex account pool with four routing orders, per-account quota budgets, safe conversation continuation, reset notifications, and optional weekly warm-up.
- Let the original v0.1.1 Telegram updater adopt compatible code while keeping the chosen update behavior. The original v0.1.0 needs its first signed upgrade over SSH.
- For managed installations, stage new Python dependencies for signed upgrades, verify a complete local snapshot, and restore the prior source and environment if startup fails.
- Add an English preference for the Telegram menus, release updates and Codex pool.

- Group models by provider in Telegram. Add Antigravity Carbon and Hydrogen when available in the provider account.
- Show the active provider's quota in the menu and status. The optional Codex account switcher can display quota for saved accounts with a compatible codex-auth CLI.
- Add a close button to the main menu and show Antigravity progress from the start of a request.

## 0.1.2-alpha.1

- SemVer release selection with Stable, RC, Beta, Alpha and Nightly channels; existing installs retain the legacy update path until their owner chooses a channel.
- Release installs and updates verify GitHub asset digests, Sigstore provenance from the official workflow, the tag commit and per-file hashes.
- Telegram update controls include channel/version selection, pins, changelog, pause, optional unattended upgrades, and backup creation. New installs default to automatic updates off.
- Fresh installation selects the newest eligible published release before downloading, with exact version pins and explicit development installation.
- Dispatch release preparation and publication separately, with verified promotion and a protected Stable environment. Add notes, checksums and a direct-dependency SPDX SBOM.
- Add private SQLite-consistent backups, explicit provider-data opt-in, size and retention settings, optional age encryption, and verified out-of-place restore.

- Persist update transactions before changing source, detect disk exhaustion early, recover interrupted updates on startup, and require separate approval for downgrades.

- Signed peer conversations over existing SSH, Telegram panel, optional text-only Mercury replies, replay protection and per-peer limits.
- One-command GitHub bootstrap and interactive provider presets with a configuration preview.
- Illustrated SVG banner in both READMEs and peer setup documentation.

## 0.1.0 — early preview

- Initial Nook source release under MIT, with English and Russian documentation.
- Telegram assistant with Codex, Antigravity, and Mercury support.
- Mercury continuation button and Gemini process cleanup on capacity errors, timeout, and cancellation.
- Configurable answer and main-menu buttons, premium or ordinary icons, colors, layouts, and interface presets.
- Separate empty runtime templates, private data exclusions, and an interactive Linux installer.
- Optional root helper, Python mods, SSH nodes, schedules, and monitoring.
- Reproducible source bundles, integrity manifest, and automated checks.
- Optional multi-account switching source included but disabled; the external codex-auth utility is not bundled.

The interface and installation wizard are Russian. Full fresh-VPS installation of this release remains to be verified.
