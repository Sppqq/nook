# Changelog

## Unreleased

- Group models by provider in Telegram. Add Antigravity Carbon and Hydrogen when available in the provider account.
- Show the active provider's quota in the menu and status. The optional Codex account switcher can display quota for saved accounts with a compatible codex-auth CLI.
- Add a close button to the main menu and show Antigravity progress from the start of a request.

## 0.1.2-alpha.1

- SemVer release selection with Stable, RC, Beta, Alpha and Nightly channels; existing installs retain the legacy update path until their owner chooses a channel.
- Release checks use GitHub Release asset digests, pinned tag commits, safe archive validation and explicit downgrade confirmation.
- Telegram update controls include channel/version selection, pins, changelog, pause, notification or confirmed install checks, and backup creation.
- Add private integrity-checked instance backups and verified out-of-place restore; fresh installation can select a channel and matching source tag.
- Add tag-validated release workflow, release notes, SHA-256 checksums and a direct-dependency SPDX SBOM.

- Conservative GitHub source updater, Telegram confirmation, local-edit conflicts, preserved runtime data, independent restart supervision and source rollback.

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
