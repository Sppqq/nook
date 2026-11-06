# Architecture

`bot.py` receives Telegram messages, checks the owner's identity, queues tasks, selects a provider, and sends results. Codex and Antigravity run as CLI subprocesses. Mercury uses an HTTP API and a shell-tool loop; its private conversations live under `spool/mercury/`.

`ui_settings.py` defines button visibility, appearance preferences, and presets. Preferences live in private runtime state and do not change provider configuration or monitoring. Owner checks and privileged approval actions are outside the visibility filters.

`mods.py` loads trusted Python extensions from `mods/`. `mods/_example.py` documents the extension API. Mods can add buttons, handlers, and periodic jobs. Core restarts are deferred until active tasks finish.

`guard.py` compares local and remote state with saved baselines. `bin/watchfix` registers monitoring after a change; `bin/later` creates scheduled tasks. The bot's main process runs the checks.

`bin/` contains agent and owner tools. `setup/` contains an optional root helper and owner API clients. `antigravity_limits.py` reads provider quota information. `.agents/skills/` contains tool instructions.

`templates/runtime/` contains only empty initial files. The installer creates local data from those templates, renders installation paths, and writes private configuration. Git ignores working data. Provider credentials are configured independently.

`tools/build_release.py` reads Git's explicit file list, checks the composition, rebuilds `MANIFEST.json`, and writes ZIP/JSON bundles and checksums to ignored `dist/`. `bootstrap.sh` optionally downloads a JSON bundle from an explicitly supplied HTTPS URL; the primary installation instructions use a Git clone.
