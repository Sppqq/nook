# GitHub: owner or agent account

The installer offers three options: your account, a separate agent account, or connect later. The selected token is verified against the login you enter, then stored only in `secrets.env` with mode 600. Account type and verified commit identity go in `config.json`; these files are never published. Model-provider login is separate from GitHub login.

For an **owner account**, create a [fine-grained PAT](https://github.com/settings/personal-access-tokens/new) scoped to selected repositories. Grant Contents and Pull requests read/write and Metadata read. Add other permissions only when required by your task. Identity verification does not verify write permissions for every repository.

For a **separate agent account**, its human owner must [register it](https://github.com/signup) and verify the email. Nook does not automate signup or request the account password. GitHub documents this as a [machine user](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys). Invite the account to any private repository it needs and accept that invitation. For public contributions, it can use forks and pull requests.

[Fine-grained PAT limitations](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) affect contributions to other users' repositories and collaborator access. For these tasks use a [classic PAT](https://github.com/settings/tokens/new) from the agent account: `public_repo` for public projects; `repo` only when private access is needed. A classic `repo` token covers **all repositories that account can access**, so give the agent account access only where needed. Changing workflow files additionally needs `workflow` for classic tokens or Workflows write for fine-grained tokens. Use an expiration date and keep the token private.

Configure later or switch accounts without reinstalling or restarting the bot:

```bash
python3 install.py --github-only --target "$HOME/nook"
# Explicit choice; login and token will be requested:
python3 install.py --github-only --github-account agent --target "$HOME/nook"
# Read a token from a private file instead of the hidden prompt:
python3 install.py --github-only --github-account owner --github-login YOUR_LOGIN --github-token-file /private/token-file --target "$HOME/nook"
```

`--github-account owner|agent|skip` also works at initial installation. `--yes` defaults to skipping GitHub unless a mode is supplied. Selecting skip preserves existing credentials when reconfiguring. Failed identity verification preserves existing settings. No token is passed as a command-line argument or printed in the setup log. Existing copies need source files from the normal updater before using this feature.

Use the supplied tools so the account choice also applies to commits and pushes:

```bash
bin/github-api /user
bin/github-git clone https://github.com/OWNER/REPO.git
bin/github-git -C /path/to/repo commit -m "Describe the change"
bin/github-git -C /path/to/repo push origin HEAD
```

`github-git` sets commit name and GitHub noreply email per invocation and uses a temporary credential helper that answers only for HTTPS `github.com`. It does not edit global Git configuration, SSH keys, or the host user's stored credentials. Use HTTPS remotes; SSH uses its own keys. Ordinary `git` and `gh` keep their existing authentication. The helper reads the token from disk each time; account switching does not need a service restart. The agent can open PRs through `github-api` once its token and repository permissions allow it.
