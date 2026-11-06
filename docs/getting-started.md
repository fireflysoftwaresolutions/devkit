# Getting started

This guide takes a Mac from zero to a verified devkit setup, explains how to onboard another developer, and lists
every file devkit touches.

## Requirements

- macOS on Apple silicon. Homebrew is installed by `install.sh` if it is missing.
- A GitHub account with read access to the private `ancongui/devkit` repository.
- A Claude account for Claude Code and/or a ChatGPT account for Codex.

## Install on a new Mac

```bash
# 1. GitHub CLI (Homebrew installs it if you do not have it yet: brew install gh)
gh auth login -h github.com -s repo,read:org,workflow,gist

# 2. devkit itself, in its canonical place
gh repo clone ancongui/devkit ~/Development/ancongui/devkit

# 3. Toolchain + configuration (safe to re-run at any time)
~/Development/ancongui/devkit/install.sh

# 4. Load the shell integration and verify
exec zsh
dev doctor
```

`install.sh` runs these steps:

| Step | What happens |
|---|---|
| Homebrew | Installed if missing |
| Toolchain | `brew bundle` with the [Brewfile](../Brewfile): git, gh, jq, ripgrep, fzf, Python, uv, Node, pnpm, OpenJDK, Maven, Gradle, jdtls, PHP, Composer, Colima, Lima, Docker CLI, Compose, Buildx, credential helper, Codex |
| npm globals | Packages from [`config/npm-globals.txt`](../config/npm-globals.txt): TypeScript, pyright, typescript-language-server, intelephense, Angular CLI, Playwright CLI (then `playwright-cli install --skills`) |
| Claude Code | The official native installer, only if `claude` is not on the PATH |
| GitHub CLI | `gh auth login` if you are not signed in |
| devkit | `dev install` (see below) |

Pass `--skip-packages` to skip the Brewfile and npm steps, for example when you only changed configuration:

```bash
~/Development/ancongui/devkit/install.sh --skip-packages
```

### What `dev install` configures

`dev install` is idempotent. Each step can be re-run alone with `dev install --only <step>`:

| Step | Effect |
|---|---|
| `config` | Creates `~/.config/devkit/config.json` (GitHub user and `<user>/handoff` detected through `gh`) and the workspace folders |
| `cli` | Links `~/.local/bin/dev` and adds a managed block to `~/.zshrc` that sources `shell/devkit.zsh` |
| `git` | Sets `user.name` / `user.email` from your GitHub account, makes `gh` git's credential helper, includes the shared git defaults and the global ignore file |
| `handoff` | Clones your handoff repository, or creates it as a private GitHub repository and pushes the first commit |
| `instructions` | Generates `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` from `agents/AGENTS.md` (+ your personal rules) |
| `skills` | Fetches third-party skills at pinned commits and links them, with devkit's own skills, into both agents |
| `claude` | Merges settings, hooks, plugins and MCP servers into Claude Code |
| `codex` | Configures Codex through its own app-server API, writes and trusts the hooks, installs plugins and MCP servers |
| `docker` | Adds the Compose/Buildx plugin folder and the keychain credential store to `~/.docker/config.json` |
| `launchd` | Loads the hourly background sync job `com.devkit.sync` |

Details: [Agents](agents.md), [Configuration](configuration.md).

### First run of each agent

- **Claude Code:** run `claude` once to sign in. Claude Desktop's Code tab reads the same `~/.claude` settings, so it
  is configured too.
- **Codex:** run `codex` once to sign in. The Codex app inside ChatGPT reads the same `~/.codex` files.
- **Docker:** `dev docker up` creates and starts the Colima VM the first time (see [Docker](docker.md)).

## Onboarding another developer

1. **Access.** Add them as a collaborator on the private `ancongui/devkit` repository (read access is enough).
2. **Install.** They follow [Install on a new Mac](#install-on-a-new-mac) with their own GitHub account.
3. **Their handoff repository is created automatically.** `dev install` detects their login and creates a private
   `<their-login>/handoff` repository. Nobody shares a handoff repository by default; to use a different name, set
   `handoff_repo` in their `~/.config/devkit/config.json` before installing.
4. **Personal rules (optional).** Anything specific to them goes in `~/.config/devkit/AGENTS.personal.md`, which
   `dev install` appends to the generated instruction files. The shared rules in `agents/AGENTS.md` stay identical
   for everyone.
5. **Verify.** `dev doctor` must end with every check passing.

## First-day checklist

```bash
dev doctor                       # every check ✓
dev                              # dashboard: projects, live sessions, attention items
dev clone <owner>/<repo>         # bring your repositories into the layout
dev adopt ~/somewhere/old-repo   # move repos that are not in the layout yet (close sessions in them first)
dev docker up                    # if you use containers or Testcontainers
dcd <repo> && claude             # or codex: the session starts with the project's context
```

Ask the agent for a quick task in a repository and check that its final message starts with `DevKit ·`. That
proves the global instructions are loaded (see [Concepts › Instruction canary](concepts.md#instruction-canary)).

## Updating

```bash
dev update          # git pull --ff-only in the devkit repo, then dev install
```

Restart running Claude Code and Codex sessions afterwards; they read instructions and hooks at session start.

## What devkit touches (and how to undo it)

devkit backs up every file it rewrites to `~/.local/state/devkit/backups/<name>.<timestamp>` first.

| Path | Change | To undo |
|---|---|---|
| `~/.claude/settings.json` | Merged: permission mode, attribution, `cleanupPeriodDays`, `env`, devkit hooks, `enabledPlugins` | Restore a backup or remove the keys |
| `~/.claude/CLAUDE.md` | Generated (a pre-existing hand-written file is backed up) | Delete it |
| `~/.claude/skills/<name>` | Symlinks to devkit-managed skills | Delete the symlinks |
| `~/.codex/AGENTS.md` | Generated | Delete it |
| `~/.codex/hooks.json` | devkit's handlers merged with any others | Remove devkit's handlers |
| `~/.codex/config.toml` | Written through Codex's API: `approval_policy`, `sandbox_mode`, trusted `projects`, `shell_environment_policy`, `mcp_servers`, hook trust state | Restore a backup or edit in Codex settings |
| `~/.agents/skills/<name>` | Symlinks (Codex's user skill folder) | Delete the symlinks |
| `~/.local/bin/dev` | Symlink to `bin/dev` | Delete it |
| `~/.zshrc` | Block between `# >>> devkit >>>` and `# <<< devkit <<<` | Delete the block |
| `~/.gitconfig` | `user.name`, `user.email`, `include.path` (shared defaults), `core.excludesFile`, gh credential helper | `git config --global --unset …` |
| `~/.docker/config.json` | `cliPluginsExtraDirs`, `credsStore` | Remove the keys |
| `~/Library/LaunchAgents/com.devkit.sync.plist` | Hourly background job | `launchctl bootout gui/$UID/com.devkit.sync` and delete the file |
| `~/.config/devkit/` | Your configuration and personal rules | Delete it |
| `~/.local/state/devkit/` | Caches, locks, session records, logs, canary log, backups | Delete it |
| `~/.local/share/devkit/` | Fetched third-party skills (`skills-src/`, `skills/`) | Delete it |
| Claude Code / Codex plugins and MCP servers | Installed and enabled | `claude plugin disable <name>`, `claude mcp remove -s user <name>`, `codex plugin remove <name>` |

Your repositories, the handoff repository and the workspace folders under `~/Development` are never deleted by
uninstalling.

See also: [Concepts](concepts.md) · [Configuration](configuration.md) · [Troubleshooting](troubleshooting.md)
