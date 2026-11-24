# Architecture

`bot.py` receives Telegram messages, checks the owner's identity, queues tasks, selects a provider, and sends results. Codex and Antigravity run as CLI subprocesses. Mercury uses an HTTP API and a shell-tool loop; its private conversations live under `spool/mercury/`. `agent_process.py` bounds CLI diagnostics and closes process groups for Codex, its quota query and Mercury tool execution after timeouts or cancellation.

`telegram_gateway.py` owns the message and callback identity checks.
Handlers stay attached to the existing aiogram dispatcher and mod API.
`job_scheduler.py` shares queue storage with `bin/later`. It writes a private
claim before dispatching, keeps the existing `spool/jobs.json` list, and
holds recurring work when an older occurrence has an unresolved result.
Use `bin/later --claims` to inspect the journal and `--ack-claim ID` after
checking the result. An acknowledgment closes the record without replaying
it. During backup, the timer skips new dispatches. Older binaries ignore
the new claim journal. Review outstanding claims before rolling back to
one of those versions.

`tool_executor.py` enforces the Mercury shell contract. It bounds output
while the subprocess runs, caps execution by the remaining request time,
and de-duplicates repeated tool IDs in one request. A timeout preserves
the operation's uncertain status and suppresses identical retries in that
request. Operator approval and OS permissions still define what tools can
do. Trusted Python mods run under the bot's account and share its access.

The reproducible Linux benchmark runs with `python -m tools.benchmark`.
It measures the import/chart smoke subprocess and the real persistent queue
on temporary data. Metrics include wall time, peak RSS, CPU time, queue
throughput and per-claim processing latency. `--seconds` controls the
sustained queue run. The reports record the effective cgroup memory limit
when it is available. Telegram and provider API timing require a separate
test with dedicated accounts.

`ui_settings.py` defines button visibility, appearance preferences, and presets. Preferences live in private runtime state and do not change provider configuration or monitoring. Owner checks and privileged approval actions are outside the visibility filters.

`mods.py` loads trusted Python extensions from `mods/`. `mods/_example.py` documents the extension API. Mods can add buttons, handlers, and periodic jobs. Core restarts are deferred until active tasks finish.

`guard.py` compares local and remote state with saved baselines. `bin/watchfix` registers monitoring after a change; `bin/later` creates scheduled tasks. The bot's main process runs the checks.

`bin/` contains agent and owner tools. `setup/` contains an optional root helper and owner API clients. `antigravity_limits.py` reads provider quota information. `.agents/skills/` contains tool instructions.

`templates/runtime/` contains only empty initial files. The installer creates local data from those templates, renders installation paths, and writes private configuration. Runtime and credential paths beside a source checkout are excluded from source validation and copying; they are not release assets. Git ignores working data. Provider credentials are configured independently.

`tools/build_release.py` reads Git's explicit file list, rebuilds
`MANIFEST.json`, and writes ZIP/JSON bundles and checksums to ignored
`dist/`. `bootstrap.sh` loads source-selection code pinned by Git blob and
SHA-256. `installer_source.py` selects and verifies the published artifact
before invoking the wizard. The explicit development path accepts a verified
development checkout or snapshot.

`release_channels.py` implements version and promotion precedence for each
channel. `nook_update.py` verifies release provenance, stages changes against
installed hashes, and writes rollback intent before touching live source.
`tools/start_guard.py` calls the frozen updater to recover an interrupted
transaction before startup. `mods/update_manager.py` exposes release and
backup controls. Legacy `main` updates stay available until a channel is
selected.

`backup_manager.py` creates private source/runtime archives with a per-file
manifest. SQLite has transaction-consistent snapshots, and other file
changes invalidate the backup. The operator can opt in to external
Codex/Antigravity credentials and sessions. They restore under
`provider-home/` inside the new instance directory.

`nook_peers.py` and `mods/peer_chat.py` exchange per-pair HMAC-signed envelopes over existing SSH. Receivers verify expiry, signatures, replay IDs, and rate limits before writing a private SQLite inbox. The mod batches owner notifications and can produce one text-only Mercury reply per original message. Incoming correspondence never enters the command queue. See `docs/PEERS.md`.
