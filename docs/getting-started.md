# Getting started

This guide takes a Mac from zero to a verified devkit setup, explains how to onboard another developer and how to
share a handoff repository, and lists every file devkit touches.

## Requirements

- macOS on Apple silicon. `install.sh` installs Homebrew if it is missing.
- A GitHub account with read access to the private `fireflysoftwaresolutions/devkit` repository.
- A Claude account for Claude Code, a ChatGPT account for Codex, or both.

## Install on a new Mac

```bash
# 1. The GitHub CLI (install.sh installs it too: brew install gh)
gh auth login -h github.com -s repo,read:org,workflow,gist

# 2. devkit itself, in its canonical place
gh repo clone fireflysoftwaresolutions/devkit ~/Development/fireflysoftwaresolutions/devkit

# 3. The setup wizard (safe to run again at any time)
~/Development/fireflysoftwaresolutions/devkit/install.sh

# 4. Load the shell integration and verify
exec zsh
dev doctor
```

`install.sh` makes sure Homebrew, Python, and the GitHub CLI are present and signed in, then starts the setup wizard,
`dev setup`. Pass `--yes` to accept every default without questions, or `--skip-packages` when you only want to change
the configuration:

```bash
~/Development/fireflysoftwaresolutions/devkit/install.sh --yes
~/Development/fireflysoftwaresolutions/devkit/install.sh --skip-packages
```

### The setup wizard

In a terminal, `dev setup` is a full-screen wizard that you drive with the arrow keys. Nothing on the Mac changes until
you choose **Apply** on the review screen.

1. **Welcome.** The wizard looks at the Mac and lists what it found: macOS, CPU, memory, and Homebrew; your GitHub
   sign-in; whether Claude Code and Codex are installed and signed in; the stacks used by the repositories in
   `~/Development`; the git hosts and clouds this Mac already shows; and Docker (Colima). Then choose **Recommended
   setup**, which uses what was found and goes straight to the review, or **Customize**, which walks through the
   sections. If GitHub is not signed in, the wizard offers to run `gh auth login` first.
2. **Sections.** Each section is one step, with a progress bar (`Step 3 of 8 · Stacks`). Every answer starts at the
   recommended or detected value, and the focused option shows a one-line explanation under it.
3. **Review.** The plan, section by section, and the packages it installs: how many per group, with the groups that
   are already installed marked `✓`. Choose **Apply**, **Change a section** (edit one section, then come back to the
   review), or **Quit without changes**.
4. **Apply.** Each step shows a spinner while it runs and `✓`, `✗`, or `!` when it ends: save
   `~/.config/devkit/config.json`, install the Homebrew and npm packages, install Claude Code if it is missing, run
   `dev install`, check the agents' sign-in (and offer to sign you in), start Docker, lock the settings if you chose
   to, and run `dev doctor`. A failed step shows the last lines of its output and the command that fixes it, and the
   wizard goes on with the next step. It ends with the checks that need attention and three next steps.

| Section | What you choose |
| --- | --- |
| Workspace | The development folder; the private handoff repository (`<you>/handoff`, or a shared one) |
| Agents | Claude Code, Codex, or both; Claude Code's permissions (no prompts, the recommended `bypassPermissions`; `acceptEdits`; `default`); Codex's autonomy (`full`, recommended, or `workspace`, sandboxed) |
| Stacks | Java and Spring, Python, PHP, Node and TypeScript (Angular, React); on a first run, the stacks found in your repositories are preselected |
| Git hosts and CI/CD | GitHub (always), GitLab, Bitbucket, Azure DevOps; the ones this Mac already shows are preselected |
| Clouds | AWS, Microsoft Azure, Google Cloud, DigitalOcean, Kubernetes and Helm, OpenTofu or Terraform; preselected from the config folders and CLIs it finds |
| Docker | VM CPUs, memory, and disk (checked against this Mac); start Colima at login? |
| Automation | Reclaim disk space automatically? Background sync interval; lock Claude Code settings with a managed-settings file? |

| Key | Action |
| --- | --- |
| `↑` `↓` (or `j` `k`, `Tab`) | Move between options |
| `1`–`9` | Jump to an option |
| `Enter` | Choose, confirm, or keep the value shown |
| `Space` | Select or clear an option in a list with checkboxes |
| `a` | Select all options, or none, in a list with checkboxes |
| `y` / `n` | Answer a yes-or-no question at once; `←` `→` switch the highlighted answer |
| Typing | Replaces the value shown in a text field; `←` `→`, `Backspace`, and `Ctrl-U` edit it |
| `Esc` | Go back one question; on a section's first question, go to the previous section (or the review) |
| `Ctrl-C` | Quit; before you apply, nothing has changed |

Run `dev setup` again at any time. On a configured Mac it starts from your saved choices (adding the hosts and clouds
it newly finds) and offers **Keep current setup**; the review marks each section that would change and shows its old
value. Applying an unchanged plan runs `dev install` again, which repairs anything that drifted.

The full output of the apply steps is in `~/.local/state/devkit/logs/setup.log`, readable only by you.

#### Line prompts and `--yes`

When the input or output is not a terminal (a pipe or a script), when `TERM` is `dumb`, or with `--yes`, the wizard
uses plain line prompts instead: each question shows its default in brackets, and Enter keeps it. `--yes` accepts
every default without asking and writes the same configuration as earlier versions. To get the line prompts in a
terminal, run `TERM=dumb dev setup`. Unlike the interactive **Recommended setup**, `--yes` keeps every stack selected
unless your saved configuration lists fewer.

| Package group | Installed | Contents |
| --- | --- | --- |
| core | always | git, gh, jq, ripgrep, fd, ast-grep, difftastic, git-cliff, fzf, Python, Node |
| docker | always | Colima, Lima, the Docker CLI, Compose, Buildx, the keychain credential helper |
| agents | always | Codex; Claude Code through its official installer when it is missing |
| docs | always | Vale, lychee, markdownlint-cli2, the Manrope font |
| security | always | gitleaks, osv-scanner, Trivy, zizmor, Semgrep, Syft, cosign |
| python | with the Python stack | uv, pyright |
| node | with the Node stack | pnpm, TypeScript, typescript-language-server, the Angular CLI, the Playwright CLI (with its skills) |
| java | with the Java stack | OpenJDK, Maven, Gradle, jdtls |
| php | with the PHP stack | PHP, Composer, Intelephense |
| gitlab | with GitLab | glab |
| azure-devops | with Azure DevOps | the Azure CLI and its `azure-devops` extension |
| aws · azure · gcp · digitalocean | with that cloud | the AWS CLI · the Azure CLI · the Google Cloud CLI · doctl |
| kubernetes · terraform | with that tool | kubectl and Helm · OpenTofu |

The groups are marked `# @group` in the [Brewfile](../Brewfile) and in
[`config/npm-globals.txt`](../config/npm-globals.txt).

### What `dev install` configures

`dev install` is idempotent: it changes only what differs. Each step can run alone with `dev install --only <step>`.

| Step | Effect |
| --- | --- |
| `config` | Creates `~/.config/devkit/config.json` if it is missing, and the workspace folders |
| `tools` | Installs the Homebrew and npm packages that are missing for your stacks, git hosts, and clouds, and the Azure DevOps CLI extension (only reports them when `packages.auto` is off) |
| `cli` | Links `~/.local/bin/dev` and adds a managed block to `~/.zshrc` that sources `shell/devkit.zsh` |
| `git` | Sets `user.name` and `user.email` from your GitHub account, makes `gh` git's credential helper, includes the shared git defaults, and the global ignore file |
| `handoff` | Clones your handoff repository, or creates it as a private GitHub repository and pushes the first commit; writes the multi-machine merge rules |
| `instructions` | Generates `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` from `agents/AGENTS.md` (plus your personal rules) |
| `skills` | Fetches third-party skills at pinned commits (your optional groups included) and links them into both agents |
| `claude` | Merges settings, hooks, plugins, and MCP servers into Claude Code; reinstalls the devkit plugin when the installed copy is stale |
| `codex` | Configures Codex through its own app-server API, writes and trusts the hooks, writes the profiles and the `scout` role, installs plugins and MCP servers, reinstalls a stale devkit plugin |
| `docker` | Adds the Compose and Buildx plugin folder and the keychain credential store to `~/.docker/config.json`; registers Colima at login if you chose that |
| `docs` | Checks markdownlint, lychee, and Vale, and syncs the Google and write-good Vale styles |
| `launchd` | Loads devkit's seven scheduled jobs, `com.devkit.job.<name>`: sync (hourly by default), sleep, gc, repair, audit, doctor and lessons ([jobs and audit](jobs-and-audit.md)) |

Details are in [Agents](agents.md) and [Configuration](configuration.md).

### First run of each agent

- **Claude Code:** run `claude` once to sign in (the wizard offers it). Claude Desktop's Code tab reads the same
  `~/.claude` settings, so it is configured too.
- **Codex:** run `codex` once to sign in. The Codex app inside ChatGPT reads the same `~/.codex` files.
- **Docker:** `dev docker up` creates and starts the Colima VM the first time (see [Docker](docker.md)).
- **A live check:** `dev agents test` runs a short real session with each agent and checks that the hooks, the guard,
  the status block, the global instructions, and the devkit skills all work.

## Onboarding another developer

1. **Access.** Make sure they are a member of the `fireflysoftwaresolutions` GitHub organization with read access to
   the private `fireflysoftwaresolutions/devkit` repository.
2. **Install.** They follow [Install on a new Mac](#install-on-a-new-mac) with their own GitHub account.
3. **Choose the handoff repository.** By default the wizard proposes a private `<their-login>/handoff`. To work as a
   team on one shared handoff repository, they enter its name instead (see
   [Teams and machines](teams.md#set-up-a-shared-handoff-repository)).
4. **Personal rules (optional).** Anything specific to them goes in `~/.config/devkit/AGENTS.personal.md`, which
   `dev install` appends to the generated instruction files. The shared rules in `agents/AGENTS.md` stay the same for
   everyone.
5. **Verify.** `dev doctor` must end with every check passing, and `dev agents test` should pass for each agent they
   use.

A second Mac of your own is the same as another developer: install, and enter the same handoff repository.

## First-day checklist

```bash
dev doctor                       # every check ✓
dev                              # home: projects, live sessions, attention items
dev clone <owner>/<repo>         # bring your repositories into the layout
dev adopt ~/somewhere/old-repo   # move repositories that are not in the layout yet (close sessions in them first)
dev docker up                    # if you use containers or Testcontainers
dev overview                     # initiatives and projects at a glance
dev team                         # who is working on what, if you share a handoff repository
dev delivery                     # in each project: its release policy and gaps (dev delivery apply records one)
dcd <repo> && claude             # or codex: the session starts with the project's context
```

Ask the agent for a quick task in a repository and check that its final message starts with the status block, a bold
`[Firefly> DevKit]` header followed by Done, Check, and Next. That proves the global instructions are loaded (see
[Concepts › The status block](concepts.md#the-status-block)).

## Updating

```bash
dev update          # git pull --ff-only in the devkit repository, then dev install
```

Restart running Claude Code and Codex sessions afterward; they read instructions and hooks at session start. To change
the choices you made in the wizard, run `dev setup` again.

## What devkit touches (and how to undo it)

devkit backs up every file it rewrites to `~/.local/state/devkit/backups/<name>.<timestamp>` first (the newest 10 of
each).

| Path | Change | To undo |
| --- | --- | --- |
| `~/.claude/settings.json` | Merged: permission mode, attribution, `cleanupPeriodDays`, `env`, devkit hooks, `enabledPlugins`, `fastModePerSessionOptIn` | Restore a backup or remove the keys |
| `~/.claude/CLAUDE.md` | Generated (a hand-written file is backed up first) | Delete it |
| `~/.claude/skills/<name>` | Symlinks to devkit-managed skills | Delete the symlinks |
| `/Library/Application Support/ClaudeCode/managed-settings.d/50-devkit.json` | Only with `dev config lock` | `dev config unlock` |
| `~/.codex/AGENTS.md` | Generated | Delete it |
| `~/.codex/hooks.json` | devkit's handlers merged with any others | Remove devkit's handlers |
| `~/.codex/config.toml` | Written through Codex's API: `approval_policy`, `sandbox_mode`, trusted `projects`, `shell_environment_policy`, `agents.max_concurrent_threads_per_session`, `mcp_servers`, hook trust state | Restore a backup or edit it in Codex settings |
| `~/.codex/fast.config.toml`, `deep.config.toml`, `review.config.toml` | The Codex profiles | Delete them |
| `~/.codex/agents/scout.toml` | The `scout` agent role | Delete it |
| `~/.agents/skills/<name>` | Symlinks (Codex's user skill folder) | Delete the symlinks |
| `~/.local/bin/dev` | Symlink to `bin/dev` | Delete it |
| `~/.zshrc` | The block between `# >>> devkit >>>` and `# <<< devkit <<<` | Delete the block |
| `~/.gitconfig` | `user.name`, `user.email`, `include.path` (shared defaults), `core.excludesFile`, the gh credential helper | `git config --global --unset …` |
| `~/.docker/config.json` | `cliPluginsExtraDirs`, `credsStore` | Remove the keys |
| `~/Library/LaunchAgents/com.devkit.job.*.plist` | The scheduled jobs | `dev jobs` lists them; for each, `launchctl bootout gui/$UID/com.devkit.job.<name>` and delete the file |
| `~/.config/devkit/` | Your configuration and personal rules | Delete it |
| `~/.local/state/devkit/` | Caches, locks, session records, runs and timing, the latest status per project, logs, the canary log, the dashboard, the `dev rm` manifest (`trash.jsonl`), backups | Delete it |
| `~/.local/share/devkit/` | Fetched third-party skills (`skills-src/`, `skills/`) and the Vale styles | Delete it |
| Claude Code and Codex plugins and MCP servers | Installed and enabled | `claude plugin disable <name>`, `claude mcp remove -s user <name>`, `codex plugin remove <name>` |

Uninstalling never deletes your repositories, the handoff repository or the workspace folders under `~/Development`.

See also [Concepts](concepts.md), [Configuration](configuration.md), and [Troubleshooting](troubleshooting.md).
