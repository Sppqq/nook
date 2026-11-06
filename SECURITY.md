# Security

Nook is an autonomous server administration assistant, intended for the owner of the machine. It can read files and execute commands with its Linux user's permissions. Model instructions and confirmation prompts are not an operating-system sandbox.

## Trust boundaries

- Run it under a dedicated user or on a test server first. Do not expose other people's data to that account.
- Your Telegram owner ID controls access to the bot. Protect that Telegram account and your bot token.
- Your requests, supplied files, logs, and command output may be sent to the selected model provider. Each provider has its own policies and terms.
- Provider agents run with autonomous tool permissions. Untrusted messages, documents, logs, and websites can contain prompt injections.
- Python mods are trusted executable code with the bot user's permissions. Review them before enabling them.
- Optional owner API and OpenBot integrations are disabled by default and require separate configuration.

## Privileged operations

The root helper is optional and installed separately. Its sudoers policy allows specified maintenance commands without a password. Other root requests are sent to Telegram for approval; temporary full-access mode bypasses individual approvals.

The request spool is controlled by the bot's Linux user. This approval flow is not a security boundary against an attacker who has already compromised that account. Avoid installing the helper unless you understand and accept that trust model.

## Credentials and public reports

Keep `config.json`, `secrets.env`, provider sessions, SSH keys, memory, runtime state, and backups private. Do not post an installed directory in an issue. Redact logs and rotate any exposed credentials. `.gitignore` and pattern checks do not guarantee that every possible secret will be detected.

Checksums check integrity, not the identity of a download source. Inspect installers and use trusted sources. Keep your own backups: monitoring and rollback depend on the bot being able to run.

## Reporting vulnerabilities

Do not disclose credentials or exploitable details in a public issue. If GitHub's **Report a vulnerability** option is available in this repository's Security tab, use it. Otherwise, open a minimal issue requesting a private reporting channel without publishing the exploit or sensitive data. A dedicated private reporting address has not yet been configured.

This early preview has not had an independent security audit.
