# Getting started

This guide takes a Mac from zero to a verified devkit setup, explains how to onboard another developer and how to
share a handoff repository, and lists every file devkit touches. When devkit is installed, the
[tutorials](surfaces.md) show your first task on the app or terminal you use.

## Requirements

- A Mac with Apple silicon (M1 or later). To check, open **About This Mac**, which shows a chip named Apple M…, or
  run `sysctl -n hw.optional.arm64`, which prints `1`, also in a terminal that runs under Rosetta; an Intel Mac prints
  an error instead. devkit installs its tools with Homebrew, and Homebrew's installer no longer runs on Intel Macs, so
  a new install needs Apple silicon. The installer installs Homebrew if it is missing. devkit does not run on Linux or
  Windows.
- An Intel Mac that already has Homebrew can still run devkit. Homebrew no longer supports Intel, so some tools may
  build slowly from source, and Homebrew plans to stop running on Intel in or after September 2027 (see Homebrew's
  [support tiers](https://docs.brew.sh/Support-Tiers)).
- A GitHub account. devkit's repository, `fireflysoftwaresolutions/devkit`, is public; your handoff repository stays
  private.
- A Claude account for Claude Code, a ChatGPT account for Codex, or both.

## Your first install, step by step

This walk-through is for a first install on your own Mac, even if you have never used a terminal. It takes one
command and a few presses of Return. Have your Mac's login password and your GitHub account at hand.

1. **Open Terminal.** Press Command-Space, type `Terminal`, and press Return. A window with a text prompt opens.
2. **Paste the install command, then press Return:**

   ```bash
   curl -fsSL https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh | bash
   ```

   The first line says `devkit installer · devkit is for Macs with Apple silicon`. Each step after it starts
   with `✦` and its name, then one sentence that says what the step does and why. On an Intel Mac without Homebrew,
   the first step stops and says why; nothing is changed.
3. **Homebrew**, only if your Mac does not have it yet. Homebrew is the standard installer for developer tools on a
   Mac, and devkit uses it for everything else. It asks for your Mac password: type it and press Return. Nothing
   appears while you type; that is normal. Then press Return to continue. This step takes the longest of all,
   because it also installs Apple's command line tools.
4. **git, Python, and the GitHub CLI.** Nothing to do here: each tool shows a line with `✓` when it is ready.
5. **devkit.** The installer downloads devkit into `~/Development/fireflysoftwaresolutions/devkit` and shows
   `✓ downloaded v…`.
6. **GitHub sign-in.** devkit keeps your project notes in a private GitHub repository and saves your work under your
   name, so it needs your GitHub account. Press Return at each question. The terminal shows a one-time code, such as
   `1A2B-3C4D`, and your browser opens GitHub: sign in, type the code, and choose **Authorize**. Back in Terminal, you
   see `✓ signed in to GitHub as <your name>`.
7. **The setup wizard.** A full-screen page shows what the wizard found on your Mac, with **Recommended setup**
   selected: press Return. On a Mac with no projects yet, it then asks **Stacks you work in**, the programming
   languages you plan to use, with Python and Node / TypeScript (JavaScript) already selected. Press Return to keep
   them, or add Java or PHP with the arrow keys and Space. The next page is the plan, with **Apply** selected: press
   Return. Each item then shows a spinner while it runs and `✓` when it is done. When the wizard offers to sign you
   in to Claude Code or Codex, press Return: your browser opens, and you sign in there with your Claude or ChatGPT
   account. When it offers to start Docker, press Return.
8. **The summary.** The installer ends with **What you have now** and **What to do first**. One line of **What you
   have now** says that the hooks and the guard run a read-only copy of devkit, for example
   `devkit 26.11.3 (4f2c1ab) in ~/.local/share/devkit/releases/26.11.3-4f2c1ab`. That copy is what keeps your agents
   safe, and nothing can change it by accident ([The pinned copy](#the-pinned-copy) explains it).
9. **Open a new Terminal window** (Command-N), so that the `dev` command works, and run:

   ```bash
   dev doctor
   ```

   Every line should show `✓`. A line with `✗` names the command that fixes it.
10. **Start your first project:**

    ```bash
    dev new my-app
    dcd my-app
    claude
    ```

    `dev new` creates the project folder, `dcd` goes into it, and `claude` starts Claude Code there (run `codex` for
    Codex). Ask it for something small, and check that its reply starts with a bold `[Firefly> DevKit]` line, which
    shows that devkit is active.
11. **Follow the tutorial for where you work.** Each one walks through a first task with devkit's crew and explains
    what you see. Pick [Claude Code](tutorials/claude-code.md) in Terminal or an IDE,
    [Claude Desktop's Code tab](tutorials/claude-desktop.md), [Codex CLI](tutorials/codex-cli.md), or
    [Codex in the ChatGPT app](tutorials/chatgpt-codex.md).

If a step fails, the installer names it and prints the command that runs it again. See
[Troubleshooting › Installing and updating](troubleshooting.md#installing-and-updating).

## Install on a new Mac

```bash
# 1. The installer: prerequisites, the newest devkit release, GitHub sign-in, and the setup wizard
curl -fsSL https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh | bash

# 2. Load the shell integration and verify
exec zsh
dev doctor
```

The installer prints each step with one sentence on what it does and why, and ends with a summary: where devkit is,
its version, what you have now, and what to do first. It is safe to run again. In order, it:

1. Checks that the computer is a Mac, and stops on any other system before it changes anything. On an Intel Mac, it
   goes on only when Homebrew is already there (on your `PATH` or in `/usr/local`), with one warning line; otherwise
   it stops, says that a new install needs Apple silicon, and changes nothing. In a terminal that runs under Rosetta
   on Apple silicon, it stops too and asks you to open a native terminal.
2. Finds Homebrew in `/opt/homebrew` (Apple silicon) or `/usr/local` (Intel), or installs it with Homebrew's official
   installer, which asks for your password. When Homebrew was not on your `PATH`, it adds Homebrew's `shellenv` line to
   `~/.zprofile`, as Homebrew's installer asks you to do, so new terminals find `brew` and what it installs.
3. Installs git, Python, and the GitHub CLI with Homebrew when they are missing.
4. Clones devkit into `~/Development/fireflysoftwaresolutions/devkit` and checks out the newest release, the highest
   `vYY.MM.PATCH` tag. If devkit is already there at a release, it moves to the newest one; a checkout on a branch, with
   local changes, or at another commit stays as it is. A folder there that is not a git checkout is never changed.
5. Signs you in to GitHub with the browser (`gh auth login` over HTTPS, so it asks no technical questions), unless
   the GitHub CLI is signed in already.
6. Starts the [setup wizard](#the-setup-wizard), `dev setup`, which asks its questions in the terminal even though
   the installer arrived through a pipe. Its `dev install` makes the [pinned copy](#the-pinned-copy) that the hooks
   and the guard run.

To read the script before you run it, download it first:

```bash
curl -fsSL https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh -o install.sh
less install.sh
bash install.sh
```

### Installer options

Pass options after `bash -s --`, and set variables for `bash`, not for `curl`:

```bash
curl -fsSL https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh | bash -s -- --yes
curl -fsSL https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh | DEVKIT_REF=v26.11.2 bash
```

| Option or variable | Effect |
| --- | --- |
| `-y`, `--yes` | Accept every default in the setup wizard |
| `--skip-packages` | Configure only; do not install the toolchain packages for your stacks (Homebrew, git, Python, and the GitHub CLI are still checked) |
| `--no-setup` | Get devkit and its prerequisites, but do not sign in or run the wizard; run `install.sh` from the checkout later to finish |
| `-h`, `--help` | Print the usage, the options, and the variables |
| Any other option | Passed to `dev setup` |
| `DEVKIT_HOME` | Where devkit goes (default `~/Development/fireflysoftwaresolutions/devkit`) |
| `DEVKIT_REPO` | The repository to clone, for example your fork (default `https://github.com/fireflysoftwaresolutions/devkit.git`) |
| `DEVKIT_REF` | A tag or branch to check out instead of the newest release |
| `GH_TOKEN` | A GitHub token, read by the GitHub CLI; signs in an install that has no terminal |

### Without a terminal

In CI or a provisioning script, nothing can answer questions, so the installer runs without them. Homebrew's
installer runs with `NONINTERACTIVE=1` (it needs passwordless `sudo` then), and the wizard gets `--yes`. The GitHub
sign-in needs a person, or a `GH_TOKEN`: without either, the installer downloads devkit, skips the sign-in and the
wizard, says how to finish, and exits with code `2`.

### From a clone

When you run `install.sh` from a clone of devkit (`./install.sh`), it sets up that checkout as it is, on whatever
branch or commit it is: it does not clone, fetch, or switch anything. This is how you install a branch you are working
on (see [Developing devkit](development.md)). A commit that is not a release is pinned only after its tests passed on
this Mac, so run `bin/dev ci --run` in the clone first; the installer says so when the checkout is not at a release.

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
   `dev install` (which pins devkit first), check the agents' sign-in (and offer to sign you in), start Docker, lock
   the settings if you chose to, and run `dev doctor`. A failed step shows the last lines of its output and the command that fixes it, and the
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
every default without asking and writes the same configuration as earlier versions. When nothing can answer (the input
is not a terminal), the line prompts say so at the start and take every default, as with `--yes`. To get the line prompts in a
terminal, run `TERM=dumb dev setup`. With `--yes`, the stacks are your saved ones, else the ones found in your
projects, and otherwise Python and Node.

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

`dev install` is idempotent: it changes only what differs. It first pins devkit (next section), then runs every step
below from that pinned copy, so every path the steps write names the copy, never the checkout. Each step can run alone
with `dev install --only <step>`, which re-applies it from the copy that is installed already, without pinning.

| Step | Effect |
| --- | --- |
| `config` | Creates `~/.config/devkit/config.json` if it is missing, and the workspace folders |
| `tools` | Installs the Homebrew and npm packages that are missing for your stacks, git hosts, and clouds, and the Azure DevOps CLI extension (only reports them when `packages.auto` is off) |
| `cli` | Links `~/.local/bin/dev` to the pinned copy and adds a managed block to `~/.zshrc` that sources its `shell/devkit.zsh` |
| `git` | Sets `user.name` and `user.email` from your GitHub account, makes `gh` git's credential helper, includes the shared git defaults, and the global ignore file |
| `handoff` | Clones your handoff repository, or creates it as a private GitHub repository and pushes the first commit; writes the multi-machine merge rules |
| `instructions` | Generates `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` from `agents/AGENTS.md` (plus your personal rules) |
| `skills` | Fetches third-party skills at pinned commits (your optional groups included) and links them into both agents |
| `claude` | Merges settings, hooks, plugins, and MCP servers into Claude Code; reinstalls the devkit plugin when the installed copy is stale |
| `codex` | Configures Codex through its own app-server API, writes and trusts the hooks, writes the profiles and the `scout` role, installs plugins and MCP servers, reinstalls a stale devkit plugin |
| `desktop` | When Claude Desktop is installed: builds the devkit extension for chat and Cowork and opens its installer when it is missing or out of date (choose **Install**); see [Claude Desktop chat and Cowork](tutorials/desktop-chat-and-cowork.md) |
| `docker` | Adds the Compose and Buildx plugin folder and the keychain credential store to `~/.docker/config.json`; registers Colima at login if you chose that |
| `docs` | Checks markdownlint, lychee, and Vale, and syncs the Google and write-good Vale styles |
| `launchd` | Loads devkit's seven scheduled jobs, `com.devkit.job.<name>`: sync (hourly by default), sleep, gc, repair, audit, doctor and lessons ([jobs and audit](jobs-and-audit.md)) |
| `prompt` | Keeps the [prompt helper](#the-prompt-helper) as you chose it: refreshes devkit's Starship block, or asks once when you have not answered yet (`dev setup prompt`) |
| `shell` | Keeps the [zsh helpers](#tab-completion-and-the-zsh-helpers) as you chose them, or asks once when you have not answered yet (`dev setup shell`) |

Details are in [Agents](agents.md) and [Configuration](configuration.md).

### The pinned copy

Your agents' hooks and the guard run devkit's code before every command an agent runs. If they ran the checkout in
`~/Development/fireflysoftwaresolutions/devkit`, one broken or careless edit there would switch the guard off for every
session at once. That is why `dev install` first makes a **pinned copy**: a read-only copy of the checkout's current commit in
`~/.local/share/devkit/releases/<version>-<commit>/`, with a list of the SHA-256 checksum of every file.

- **What runs it.** The hooks of Claude Code and Codex, devkit's status line, the `dev` command in your shell
  (`~/.local/bin/dev`), the scheduled jobs, and the Claude Desktop extension. `~/.local/share/devkit/install.json`
  records which copy is installed and which checkout it came from.
- **What it pins.** Only a commit that has earned it: a **release**, a `vYY.MM.PATCH` tag that GitHub has on the same
  commit (what the installer and `dev update` check out), or a commit whose **tests passed on this Mac**
  (`bin/dev ci --run` in the checkout). Anything else is refused with the reason and the way forward.
- **What it refuses.** Tracked files with changes that are not committed, because the copy is made from git and would
  not match what is on disk, and code that does not compile, because the hooks would then fail open.
- **Installing anyway.** `dev install --unverified --why "<reason>"` pins a commit that is neither a release nor
  tested. The reason goes to the audit trail (`dev audit`).
- **Checked.** `dev doctor` fails when a file in the copy changed after it was pinned, or when a hook runs something
  other than the pinned copy, and names the fix. `dev install` rebuilds a changed copy from git.
- **Rollback.** devkit keeps the newest three copies, and any copy still in use. To go back to an earlier one, run
  `dev install --version 26.11.2` (the version, or `<version>-<commit>`); it checks the copy against its checksums
  first.

### First run of each agent

The [tutorials](surfaces.md) walk through a first task on each surface.

- **Claude Code:** run `claude` once to sign in (the wizard offers it). Claude Desktop's Code tab reads the same
  `~/.claude` settings, so it is configured too.
- **Codex:** run `codex` once to sign in. The ChatGPT app's Codex reads the same `~/.codex` files.
- **Docker:** `dev docker up` creates and starts the Colima VM the first time (see [Docker](docker.md)).
- **A live check:** `dev agents test` runs a short real session with each agent and checks that the hooks, the guard,
  the status block, the global instructions, and the devkit skills all work.

## The prompt helper

Inside a devkit project, your prompt can show a small helper: the project's alias (the name `dev status` takes), the
crew members working, the checks of your branch, and a question that waits for you. Outside devkit projects it shows
nothing. This Mac's Starship prompt with the helper:

```text
 ana  …/acme/lending-core   release/26.11.3 ✦ devkit · 6 working  ! 1 question for you   ♥ 12:58
```

| Part | 100 columns and wider | Narrower | Shown while |
| --- | --- | --- | --- |
| The project | `✦ devkit` | `✦ devkit` | You are in one of its checkouts or worktrees; an alias over 16 characters ends in `…` |
| The crew | `· 6 working` | `▸6` | A member's heartbeat is newer than 15 minutes, in a run that reported in the last 30 minutes (the rule `dev status` uses) |
| The checks | `· checks ✓`, `✗` or `…` | `✓`, `✗` or `…` | Your branch has a run (passed, failed or running), and you are still on that branch |
| A question for you | `! 1 question for you` | `!1` | A crew decision waits for your answer; it goes the moment you answer |

When devkit has not refreshed the helper for 24 hours, only the project part stays. Without UTF-8 (`LANG=C`) the
glyphs become `*`, `>`, `ok`, `x` and `...`.

Add it with `dev setup prompt` (`dev install` also asks once), in your own terminal: run by an agent, it changes
nothing and exits with code 2, because only you can agree to it. It says what it changes, asks, and then:

- **Starship:** adds a marked block to `~/.config/starship.toml`, after saving a copy in
  `~/.local/state/devkit/backups/`. It puts `${env_var.DEVKIT_PROMPT}` and `${env_var.DEVKIT_PROMPT_ASK}` into your
  `format` right after `$git_status` (or `$git_branch`, or first), and gives the helper the background of the module
  it follows. A theme without backgrounds gets amber text and a red question.
- **Plain zsh** (none of the other prompts): turns on devkit's own zsh block in `shell/devkit.zsh`, which puts the
  helper in front of your `PROMPT`. `~/.zshrc` is not changed. With `NO_COLOR` set, the helper has no colors.
- **`oh-my-posh`** and **Powerlevel10k**: devkit sets the text; add the snippet for your theme, which follows.

`dev prompt` shows what the helper says in the folder you are in. `dev setup prompt --remove` takes it out again.

The block devkit adds to Starship's file:

```toml
# >>> devkit prompt (added by dev setup; dev setup prompt --remove takes it out) >>>
[env_var.DEVKIT_PROMPT]
variable = "DEVKIT_PROMPT"
style = "bg:#FCA17D fg:#10110F"
format = "[$env_value ]($style)"

[env_var.DEVKIT_PROMPT_ASK]
variable = "DEVKIT_PROMPT_ASK"
style = "bg:#C0392B fg:#F3F1EB bold"
format = "[ $env_value ]($style)"
# <<< devkit prompt <<<
```

For `oh-my-posh`, add two `text` segments to a block of your theme, right after the `git` segment:

```json
{
  "type": "text",
  "style": "plain",
  "foreground": "#FFB34A",
  "template": "{{ if .Env.DEVKIT_PROMPT }} {{ .Env.DEVKIT_PROMPT }} {{ end }}"
},
{
  "type": "text",
  "style": "plain",
  "foreground": "#F3F1EB",
  "background": "#C0392B",
  "template": "{{ if .Env.DEVKIT_PROMPT_ASK }} {{ .Env.DEVKIT_PROMPT_ASK }} {{ end }}"
}
```

For Powerlevel10k, add this function to `~/.p10k.zsh`, then add `devkit` to `POWERLEVEL9K_LEFT_PROMPT_ELEMENTS`
right after `vcs`:

```zsh
function prompt_devkit() {
  [[ -n $DEVKIT_PROMPT ]] && p10k segment -f 215 -t "${DEVKIT_PROMPT//\%/%%}"
  [[ -n $DEVKIT_PROMPT_ASK ]] && p10k segment -b red -f white -t "${DEVKIT_PROMPT_ASK//\%/%%}"
}
```

**What a prompt costs.** Nothing runs when the prompt is drawn. devkit writes the helper's facts ahead of time to
`~/.local/state/devkit/prompt/cache.zsh`: the hooks refresh the folder's project after a tool call (at most every 5
seconds) and at once when you send a prompt, a turn ends, or a crew member starts or stops, so an answered question
or a member that finished shows in your next prompt; `dev status`, `dev alias`, `dev prompt refresh`, and
`dev ci watch` refresh it too. A refresh from a hook reads a few files and runs no git, network call, or other
program. A zsh function reads the cache with builtins only, never Python, git, or another program, and only when the
cache is a regular file of yours, not a link, in a folder of yours, and neither is writable by anyone else; only
digits from it become numbers. Measured on an Apple silicon Mac
with 40 projects of 3 checkouts each: 0.13 ms per prompt, and 1.7 ms for a prompt right after devkit rewrote the
cache. The budget is 5 ms, and a test holds it. `DEVKIT_PROMPT` and `DEVKIT_PROMPT_ASK` hold plain text, because Starship
and `oh-my-posh` escape a `%` for zsh themselves; the preceding snippets escape it for zsh and Powerlevel10k.

## Tab completion and the zsh helpers

**Tab completion** works as soon as your shell sources `shell/devkit.zsh` (the `cli` step does that). Press
<kbd>Tab</kbd> after `dev` and zsh offers every command with a one-line description; after a command, its
subcommands, options and choices; and where an argument names something devkit knows, the live values:

```text
$ dev task done <Tab>
T-fy3k2m9q1ab  -- now · Fix the login timeout
T-fy3k2pa7c2d  -- next (blocked) · Ship the beta
$ dev progress step --step <Tab>
design  -- step 1 · done
api     -- step 2 · running · Backend engineer (Tank)
$ dcd devkit <Tab>
feat/zsh-completions  -- ~/Development/.worktrees/fireflysoftwaresolutions/devkit/feat-zsh-completions
```

The completion is generated from `dev`'s own commands, so it never falls behind them. Projects, worktrees, work items,
plan steps, crew members, plans, and branches come from devkit's records; the full list is in
[Commands › `dev completion`](commands.md#dev-completion-zsh--bash). A second <kbd>Tab</kbd> within 10 seconds costs
under a millisecond. A <kbd>Tab</kbd> waits at most 0.1 seconds for new values when it has older ones to show, and
0.5 seconds for the first values of a kind in a new shell; macOS's bash 3.2 counts only whole seconds, so it waits up
to 1 second. When devkit is slower than that, you get the last values, and the new ones on the next <kbd>Tab</kbd>.

In bash, add this line to `~/.bashrc`:

```bash
eval "$(dev completion bash)"
```

**`dcd` and `dwt`** move you around: `dcd widget` goes to a project by its alias, id, or part of its name, and
`dcd widget login` to its worktree for `feat/login`; `dwt feat/login` creates or reuses a managed worktree and goes
there.

**The zsh helpers** add fzf pickers, and change how your shell behaves, so they are off until you say yes. Run
`dev setup shell` in your own terminal (`dev install` also asks once): run by an agent, it changes nothing and exits
with code 2. Then, with fzf installed:

| Keys or command | What it does |
| --- | --- |
| <kbd>Ctrl</kbd>+<kbd>X</kbd> <kbd>i</kbd> | Insert a work item's id, picked from the project you are in |
| <kbd>Ctrl</kbd>+<kbd>X</kbd> <kbd>p</kbd> | Insert a project's alias |
| <kbd>Ctrl</kbd>+<kbd>X</kbd> <kbd>b</kbd> | Insert a branch of the repository you are in |
| `dpick task` (or `project`, `branch`, `worktree`, `step`, `plan`, `member`) | Print one, picked with fzf: `git switch "$(dpick branch)"` |
| `dcd` with no project | Pick a project or worktree on this Mac and go there |

zsh's own completion system already uses <kbd>Ctrl</kbd>+<kbd>X</kbd> <kbd>t</kbd>, so the work item picker is on
<kbd>i</kbd>. A key you bound yourself keeps your binding. `dev setup shell --remove` turns the helpers off again;
completion, `dcd`, and `dwt` stay.

## Onboarding another developer

1. **Access.** devkit's repository is public, so they need only a GitHub account. To share a team handoff repository,
   give them write access to it.
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
dcd <repo> && claude             # or codex: the session starts with the context of the project
```

Ask the agent for a quick task in a repository and check that its final message starts with the status block, a bold
`[Firefly> DevKit]` header followed by Done, Check, and Next. That proves the global instructions are loaded (see
[Concepts › The status block](concepts.md#the-status-block)).

## Updating

```bash
dev update --check  # read-only: is a newer release out, and what changes in it
dev update          # the newest release (or a pull of the branch), then dev install, which pins it
```

`dev update --check` changes nothing: it shows the installed and the newest release, the CHANGELOG headings in
between, and whether the checkout and the pinned copy are behind. Agents may run it. A daily job runs it at 09:15, and
the first session of the day and `dev status` then name a pending release (see
[Commands › `dev update`](commands.md#dev-update---yes----check---quiet---record)).

The installer leaves devkit at a release tag, so `dev update` follows releases: it fetches the tags and checks out the
newest release when it is newer, then runs `dev install`, which pins that release; when devkit is at the newest
release already, it says so and changes nothing. A checkout on a branch, which is how you work on devkit itself, gets
`git pull --ff-only` instead, and `dev install` pins the new commit only after `bin/dev ci --run` passes for it.
`dev update` refuses to start while tracked files have local changes. Running the installer again also moves a release
checkout to the newest release.

If a new release misbehaves, `dev install --version <previous version>` switches the hooks back to the copy pinned
before it.

Run `dev update` and `dev install` yourself, in Terminal. They change what every hook and the guard run, so the guard
refuses them from an agent: ask Claude or Codex for anything else, but not for these.

A Claude Code or Codex session reads its instructions and hooks when it starts, and keeps them until it ends. That
is why `dev install` and `dev update`, before they change anything, list the sessions that use devkit and still run,
and ask once in a terminal:

```text
2 running sessions use devkit; restart each after this to load the new hooks (until then it keeps the old ones, which change no setting):
  Claude Code · acme/billing (feat/invoice-export) · devkit 26.11.5 · session 3f9a1c2e · since 2026-11-07 09:12
  Codex · acme/web · devkit 26.11.5 · session 7b2d04aa · since 2026-11-07 10:40
  Go on? [Y/n]
```

Press Return to go on, or type `n` to stop with nothing changed; `--yes` goes on without asking. Then restart each
session on the list. Until you do, a session still on the older devkit leaves every setting devkit manages alone:
the agents' settings and hooks, `CLAUDE.md`, `AGENTS.md`, and the global git config stay as the new release wrote
them.
It tells you once to restart, and its background work (the sync, the CI watch, the repair) runs the new release (see
[Work states › The install pin](work-states.md#the-install-pin)). To change the choices you made in the wizard, run
`dev setup` again.

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
| `~/Development/fireflysoftwaresolutions/devkit` | The devkit checkout (installer; `DEVKIT_HOME` chooses another folder) | Delete the folder |
| Homebrew, git, Python, and the GitHub CLI | Installed by the installer when they are missing | `brew uninstall git python gh`; Homebrew's own uninstall script |
| `~/.zprofile` | One line, `eval "$(/opt/homebrew/bin/brew shellenv)"`, added by the installer only when Homebrew was not on your `PATH` (Apple silicon) | Delete the line |
| `~/.local/bin/dev` | Symlink to `bin/dev` in the pinned copy | Delete it |
| `~/.zshrc` | The block between `# >>> devkit >>>` and `# <<< devkit <<<` | Delete the block |
| `~/.config/starship.toml` | Only after you agree in `dev setup prompt`: the block between `# >>> devkit prompt` and `# <<< devkit prompt <<<`, and its two references in `format` | `dev setup prompt --remove` |
| `~/.gitconfig` | `user.name`, `user.email`, `include.path` (shared defaults), `core.excludesFile`, the gh credential helper | `git config --global --unset …` |
| `~/.docker/config.json` | `cliPluginsExtraDirs`, `credsStore` | Remove the keys |
| `~/Library/LaunchAgents/com.devkit.job.*.plist` | The scheduled jobs | `dev jobs` lists them; for each, `launchctl bootout gui/$UID/com.devkit.job.<name>` and delete the file |
| `~/.config/devkit/` | Your configuration and personal rules | Delete it |
| `~/.local/state/devkit/` | Caches, locks, session records, runs and timing, the latest status per project, logs, the canary log, the dashboard, the `dev rm` manifest (`trash.jsonl`), backups | Delete it |
| `~/.local/share/devkit/` | Fetched third-party skills (`skills-src/`, `skills/`), the Vale styles, the [pinned copies](#the-pinned-copy) (`releases/`, read-only) with the record of the installed one (`install.json`), and the Claude Desktop extension bundle (`desktop/`) | Run `chmod -R u+w ~/.local/share/devkit`, because the pinned copies are read-only, then delete it |
| The devkit extension in Claude Desktop | Installed when you choose **Install** | Remove it in **Settings › Extensions** |
| Claude Code and Codex plugins and MCP servers | Installed and enabled | `claude plugin disable <name>`, `claude mcp remove -s user <name>`, `codex plugin remove <name>` |

Uninstalling never deletes your repositories, the handoff repository or the workspace folders under `~/Development`.

See also [Concepts](concepts.md), [Configuration](configuration.md), and [Troubleshooting](troubleshooting.md).
