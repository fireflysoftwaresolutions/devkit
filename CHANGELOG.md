# Changelog

All notable changes to devkit. Versions follow CalVer `YY.MM.PATCH` (PATCH restarts at 0 each month); tags are
`vYY.MM.PATCH`. Sections follow [Keep a Changelog](https://keepachangelog.com/).

## [26.11.4] - Unreleased

Not released yet: the date is set when the release is tagged.

### Added

- **The tech lead asks your decisions nicely, and together.** `dev crew decisions --ask` prints the pending decisions
  as question dialog calls ready to send: at most 4 questions per call, an irreversible decision alone in its call,
  each opening with `D<n> ·` and one line on who asks and what it blocks, with the recommended option first; `--json`
  prints the dialog's input. When decisions piled up while you were away, your next prompt tells the tech lead once
  to ask them together, in as few calls as that allows.
- **The impeccable engine is installed with the skill.** `dev install --only skills` fetches the engine binary for
  this Mac from the upstream release that the pinned skill names, checks its SHA-256 against the pinned skill's own
  `engine-checksums.txt`, and puts it beside the launcher, so `impeccable context` no longer downloads code inside an
  agent session (0.02 s once warm). A mismatch, a failed download, or a file error (a folder it cannot write)
  installs nothing, is audited, and does not stop the rest of the install: the other skills are still linked. Claude
  Code sessions set `IMPECCABLE_NO_UPDATE_CHECK=1` and `IMPECCABLE_NO_TELEMETRY=1`, so the engine neither checks the
  network for a newer version nor sends telemetry.
  See [Skills and plugins › impeccable and its engine](docs/skills-and-plugins.md#impeccable-and-its-engine).
- **`dev doctor` checks impeccable:** the skill linked for Claude Code, its engine present with the pinned digest, and
  the Codex plugin enabled, each with its fix.
- **`dev impeccable <verb>` runs the impeccable engine devkit installed.** It takes the engine from devkit's skills
  folder (never from the current folder, an argument, or the environment), checks its SHA-256 against the install
  record before every run, and runs it in the current folder with each word as one argument, without a shell. The
  engine gets the skill folder, `IMPECCABLE_NO_UPDATE_CHECK=1`, and `IMPECCABLE_NO_TELEMETRY=1`, but no other
  `IMPECCABLE_*` variable. A missing record, a digest mismatch, or an engine outside devkit's skills folder runs
  nothing and names the fix, `dev install --only skills`. The verbs that write the agents' settings, hooks, or skill
  folders (`hooks`, `install`, `link`, `update`, `pin`, `unpin`, and `doctor --fix`) run only in your terminal. See
  [Skills and plugins › impeccable and its engine](docs/skills-and-plugins.md#impeccable-and-its-engine).

- **`dev ext ls` lists every third-party plugin, skill, and MCP server devkit installs** for Claude Code and Codex:
  its source, what pins it (a commit, an exact version, or `⚠ observed` for a vendor marketplace plugin, with the
  version and commit installed), whether it is installed and enabled, its license, its layer, and why it is there
  (`-v`). `--json` prints every field. Agents may run it: it reads the agents' own files and starts no process.
- **`dev ext outdated` and `dev ext update`.** `outdated` shows what upstream has now against each pin, with the
  compare or release link, through your own `gh` and `npm`; a skill counts only changes in its own folder, and an
  upstream that cannot be reached is reported as could not be checked, never as up to date. `update <name> --why "…"` bumps a team
  pin in the devkit checkout, on a branch only, after printing the compare link, the commits, the files changed, and
  any license change. `update --apply` updates a vendor marketplace plugin on this Mac and accepts it; only you run
  it, in a terminal.
- **Your own extensions: `dev ext add`, `remove`, `enable`, and `disable`.** Add a plugin, a skill pinned by commit,
  or an MCP server pinned by exact version or `https` URL, or turn one of devkit's off, with `--why`. Your choices
  live in `ext` in your devkit configuration, and `dev install`, `dev config repair`, and the managed-settings lock
  keep them, so a plugin you turned off is no longer turned back on within six hours. `--project` turns a Claude Code
  plugin on or off for one repository; `--team` changes devkit's defaults in the checkout, on a branch. devkit itself
  and its security plugins cannot be removed or turned off. Only you run these, in a terminal.
- **`dev update --check` tells you whether a newer devkit release is out**, and what changes in it: the installed and
  newest releases, the CHANGELOG headings in between, and whether the checkout and the pinned copy are behind. It is
  read-only, so agents may run it. A daily `update-check` job records the answer at 09:15; the first session of the
  day and `dev status` then name a pending release, without reaching the network themselves.

### Changed

- **Your answer counts even when the question is reworded, except for an irreversible action.** An answer to the
  question dialog is your evidence for a reversible decision when the question opens with `D<n> ·` (or its header is
  `D<n>`) and the label picks one of its options by letter or words; the rest may carry context. Text you type
  yourself is your answer but picks no option, and a dialog call that asks one decision twice counts for neither
  question. A decision that leads to an irreversible action counts only in devkit's exact call: one question, its
  own header, labels only. The call asks that one question and nothing else (`dev crew decisions --ask` gives each
  such decision a call of its own), the question and the labels are exactly as it prints them, the header is `D<n>`
  or empty, no option carries a description, a preview, or another field, and you click one of those labels
  without adding a note (a note can qualify the click, so answer again without one). The
  option that performs the action says what it runs (`A: yes (release v26.11.4)`). Anything else opens nothing and
  is audited as `crew.evidence-refused`, and `dev audit verify` reports earlier evidence whose call breaks the rule.
  See [The crew › How the tech lead asks you](docs/crew.md#how-the-tech-lead-asks-you).
- **The audit shows how your click became an option.** `crew.evidence` keeps `match` (`exact`, `letter`, `words`, or
  `free`), `asked` (`exact` or `reworded`), and the label you clicked when the dialog offered it (redacted, at most
  120 characters), but not your own text or the question. `dev audit verify` reruns the matcher on the
  question and label in the transcript and reports a record that says otherwise. See
  [The audit trail › What a record holds](docs/audit.md#what-a-record-holds-and-what-it-never-holds).
- **`dev crew ask --decision` takes 2 to 4 lettered options and a recommendation,** with a question under 300
  characters, and prints the corrected command otherwise. An irreversible action asked without options is
  `A: yes | B: no`, and its question names its target as a word of its own: `v26.11.40` does not name `v26.11.4`,
  and `D1` does not name PR `1`.
- **More words read as not doing it:** an option that starts with pass, stay, or leave never performs an
  irreversible action, so `--performs` refuses it.
- **Frontend work starts with impeccable.** The Frontend engineer, the UX designer, and the Mobile engineer load
  `frontend-design` and `impeccable` and run `dev impeccable context` once at the start of their work, on Claude Code
  and on Codex; `i18n` work now requires `impeccable` too.

### Fixed

- **Crew members can run impeccable, with `dev impeccable`.** The guard reads the skill's launcher as a script that
  runs a program named only at run time, and refuses it in every crew lane. It still does: the shell can reach another
  file than the one the guard reads by the same typed path (a symbolic link, `..` after `cd`, a quoted `~`, a glob, or
  a folder moved earlier in the same command), so no typed launcher path is trusted. Crew lanes run
  `dev impeccable <verb>` instead, from an allowlist: every lane runs `context`, `detect`, and `doctor` without
  `--fix`, and write lanes and the main session also run `live`, `generate`, `document`, `extract`, and `init`.
  Read-only lanes keep to the reading flags and give `detect` only local path shapes (no URL, host, domain, address,
  `user@`, query, or fragment). Every other verb, including the engine's hidden ones and any a newer engine adds, is
  the developer's: the guard refuses it to every agent, the main session's included, and `dev impeccable` runs it
  only in your terminal.
- **Desktop notifications show again.** Every devkit notification on macOS (a run that blocks or finishes, a long
  turn, a CI run devkit watched, a background job) failed without a word: devkit wrote the `·` in the title as a
  JSON escape, which AppleScript rejects. The text and title now reach `/usr/bin/osascript` as arguments, so they
  need no escaping and cannot run as script, and an `osascript` planted earlier on your `PATH` does not run.

### Security

- **A plugin that changes outside devkit is reported.** `dev install` records the version and commit of each vendor
  marketplace plugin it installs. When one changes on its own, the session start, `dev status`, `dev doctor`, and
  `dev ext ls` name the change, and the audit trail gets one `ext.drift` record for it. A skill or MCP server off its
  pin, a required security plugin that is off, and a plugin a repository enables outside devkit's catalog are
  reported the same way.
- **Agents cannot change your extensions through the vendor CLIs either.** A `claude` or `codex` call whose words
  include `plugin`, `plugins`, `mcp`, or `marketplace` anywhere, an option's value included, is refused unless the
  whole command is one of a few read-only forms (`claude plugin list|details <name>|validate <path>`,
  `claude plugin marketplace list`, `claude mcp list|get <name>`, `codex plugin list`,
  `codex plugin marketplace list`, `codex mcp list|get <name>`, or a group alone), with `-h`, `--help`, or `--json`
  at most. This allowlist fails closed: any other spelling, `--` or another option included, is refused even when
  it only reads. The guard finds the program in the forms it reads (a relative path from the command's folder, a
  link on the `PATH`, a package runner such as `npx`, `npm exec`, `bun x`, `yarn exec`, or `pnpm claude`, the CLI's
  script under `node`), refuses one it cannot know before it runs (a variable, a `PATH` the command sets, a copy the
  command makes), reads a `sh -c` line and a `bash -s` input with the words after them, and applies the rule to a
  program's code too. With `claude` or `codex`, a word the shell expands (`$P`, `"$@"`, `$'…'`, `$(…)`,
  `{plugin,install}`, `plugi?`) or a word `xargs` adds counts as a group word, so `claude -p "$PROMPT"` is refused
  as well. `dev ext update --apply` and `dev ext enable|disable --project` also check that you run them in a
  terminal, and `--team` after a `--` no longer passes the guard as a team edit.
- **The guard reads every level of a command for its git, gh, and file rules.** A `sh -c` or `eval` line, the
  command `find -exec` or `xargs` runs, and the command a runner runs (`uv run`, `npx`, `npm exec`, `bun x`,
  `yarn exec`, `timeout`) get the same rules as the command itself, so
  `find . -exec sh -c 'git push --force origin main' \;` and `npm exec -- git push --force origin main` are refused
  as `git push --force origin main` is.
- **Only your own `dev install` vouches for an extension.** `dev install` and `dev update` started from a program's
  code (`subprocess.run(['dev', 'install'])`, `os.system`, AppleScript's `do shell script`) are refused as the
  typed command is. A `dev install` that runs outside your terminal anyway neither accepts a change made outside
  devkit nor records what ships with the Codex app for the first time: it names them, and they stay as drift. The
  first record names each MCP server with the command and arguments it runs.
- **Every acceptance shows until you acknowledge it.** A process an agent starts in `screen`, `tmux`, or a Terminal
  window can look like your terminal, so each change `dev install` or `dev ext update --apply` accepts and each
  Codex app entry `dev install` records for the first time writes an `ext.accept` audit record (the entry, the
  terminal, the parent processes), sends a desktop notification, and stays listed at the session start and in
  `dev status` until you run the new `dev ext ack` in your terminal. The notification is a security notice:
  `DEVKIT_NO_NOTIFY=1` and `progress.notify: false`, which an agent can set, do not turn it off. Each listed entry is
  one short line without control, bidirectional, or zero-width characters, or other characters that render as
  nothing (variation selectors, Hangul fillers). In Claude Code, the session start shows
  that list to you as a message, and shows the last `dev ext ack` once with its time and terminal. An acceptance the
  list cannot take (busy, a file that does not parse, or one that cannot be written, such as on a full disk) is not
  made: the change stays as drift, and the refusal is audited.
  The guard now reads what `screen`, `tmux`, `expect -c`, and AppleScript's `do script` run, refuses `open` of a
  Terminal or iTerm window or a `.command` file and `xargs dev`, and skips a package manager's own options before
  its verb (`npm --silent exec`, `pnpm -C . exec`, `uv --quiet run`, `uv run --with x`). It refuses `screen -X stuff`
  keys that hold a control key other than tab, carriage return, or line feed (`^@`, `^A`, `^H`, `^O`, DEL, or ESC,
  as `^X`, `\NNN`, or the character itself), because the shell edits the line with them, and a command that holds a
  NUL character. Before, a NUL made the guard fail and let the command through.
- **Drift covers what devkit does not manage.** A plugin, MCP server, or skill folder installed outside the catalog,
  for Claude Code and Codex, is reported as `not-in-catalog`. What ships with the Codex app is listed without drift
  only when its own files vouch for it (a bundled plugin whose cached files are all byte-equal to the app bundle's)
  or `dev install` recorded it (an MCP server that runs from the bundle, a runtime plugin, a bundled plugin the app
  rewrote), and `dev install` names each one the first time; a later change shows as `changed`. A server in
  `~/.codex/config.toml` written in a form devkit does not read (a dotted or quoted key, an inline table, a key at
  the root, a quoted name) is never the app's, and a line no server owns makes none of them the app's. A required plugin that is not installed, or that a project turns off in `.claude/settings.json` or
  `.claude/settings.local.json`, is reported as `required-off` with the file, and `dev doctor` points to
  `dev config lock`. The lock now keeps a digest of each vendor plugin's files, so a rewritten version or commit
  number no longer hides changed files, and a devkit skill whose folder is not devkit's copy shows as `changed`.
- **Pins bind what runs.** An MCP server runs only through `npx` or `uvx`, the first package they run carries the
  exact version, and variables that load other code or another registry (`NODE_OPTIONS`, `PYTHONPATH`, `DYLD_*`,
  `LD_*`, `NPM_CONFIG_*`, …) are refused. `dev ext update` moves a skill pin only to a commit on the repository's
  default branch, never to a fork's commit served under the same name, and changes nothing when the review cannot
  be read. `dev ext update --apply` accepts only the version it reviewed, and `dev install` names each change it
  accepts. An overlay copy of a devkit default keeps it required, and `dev ext add` refuses one, and a skill whose
  name a devkit default already uses, for either agent, or that devkit puts in the shared skills folder itself (a
  skill from its own tree, or `playwright-cli`): one name is one folder for both agents. Names are compared in any
  case, as the macOS file system compares them, so `Playwright-CLI` is refused too. A skill name starts with a
  letter, digit, or underscore, so `.`, `..`, and hidden names are refused, and `dev install` skips a `skills.json`
  name that would land outside the shared skills folder, deleting nothing.

### Known limits

- Vendor marketplace plugins (`claude-plugins-official`, `openai-curated-remote`) are observed, not pinned: devkit
  records what got installed and reports changes, but cannot hold them at a commit yet.
- MCP servers started with `npx` are pinned at their top-level version only; their own dependencies are not.
- The guard reads commands as typed. A program name built at run time (`$(printf claude)`, an alias), a script piped
  into a shell, a command `tmux`, `screen`, `expect`, or a git alias starts later, and options that load a plugin
  for one session only get through; drift at the session start, in `dev status`, and in `dev doctor` is the
  backstop. [What the guard cannot see](docs/guard.md#what-the-guard-cannot-see) lists them.
- `dev install`, `dev ext update --apply`, and `dev ext ack` take the process tree and the terminal as proof that
  you typed them, and a process an agent starts through a launcher can look the same: `screen` or `tmux`, `expect`,
  AppleScript's `do script`, `open` of a Terminal window or a `.command` file, or `launchctl`. The guard refuses the
  forms it can read, the octal escapes such as `\015` and the caret controls such as `^m` that `screen -X stuff`
  types included, but not a script file or a command built at run time. `tmux send-keys` with editing keys
  (`C-a`) or hex keys (`-H`) still gets through until 26.11.5. The notification, the session-start
  message, and the audit trail are the backstop until a kernel-level check lands in 26.11.5. The notification shows
  only on macOS, and only while notifications are allowed for Script Editor, the app macOS lists `osascript`
  notifications under; an agent can change that setting, so the session-start message and the audit trail are the
  lasting record. See
  [Who changes extensions](docs/skills-and-plugins.md#who-changes-extensions-and-how-drift-shows).

### Removed

- **Context7 leaves both agents.** `dev install` uninstalls the Claude Code plugin and removes the Codex MCP server,
  drops its `enabledPlugins` key, and audits each removal as `ext.retire`. Library docs now come from the project's
  installed packages and framework MCP servers, then web search.

## [26.11.3] - 2026-11-06

This release installs devkit with one command, runs its
hooks from a pinned copy, gives you a live dashboard, puts the crew under one enforced RACI (who is Responsible,
Accountable, Consulted, and Informed for each kind of work), holds irreversible actions until you grant them, protects
devkit's own state from agents, carries a session's work through compaction, backs up branches that exist only on
this Mac when that starts no CI run, explains what fills your disk and frees it item by item, and makes the audit trail
verifiable and exportable.

### What changes for you

devkit refuses more in this release. Each refusal says why and names the command to use instead.

- **You run `dev install`, `dev update`, and `install.sh` yourself, in a terminal.** Agents are refused them.
  `dev install` refuses a checkout with uncommitted tracked changes or code that does not compile, and pins only a
  published release or a commit whose `bin/dev ci --run` passed; `--unverified --why "…"` overrides that, and the
  override is audited.
- **Agents cannot change devkit's state, configuration, or install.** Writes to `~/.local/state/devkit`,
  `~/.config/devkit/config.json`, and `~/.local/share/devkit`, `dev config set safeguards.*` or `audit.*`, and
  `dev config edit` are refused to every agent. You change safeguards in a terminal. A `DEVKIT_*` variable added to
  the `env` block of a Claude Code settings file (user, project, or local) is refused and removed.
- **devkit cannot be pointed elsewhere by an agent.** The guard refuses `HOME`, `DEVKIT_CONFIG`, `DEV_ROOT`, or
  `DEVKIT_SESSION` set for `dev`, and a swapped `dev` link or another `dev` earlier on the `PATH`. devkit reads a
  non-standard `DEVKIT_CONFIG` or `DEV_ROOT` only from a command you run in a terminal with no agent among its parent processes.
- **Four escape hatches moved.** `dev commit`, `dev push`, `dev pr create`, and `dev release` no longer read
  `DEVKIT_SKIP_SECRET_SCAN` or `DEVKIT_LOCAL_CI_PASSED` from their environment. Pass `--ci-passed` (recorded as
  attested), or let a real `dev ci --run` lift the push budget. `dev git` and `dev gh` no longer read
  `DEVKIT_FLAKY` or `DEVKIT_CROSS_PROJECT` from theirs; rerun a flaky CI run with
  `dev run rerun <id> --flaky "<reason>"`. In the main session, a hatch still works as a prefix of the command
  itself, such as `DEVKIT_SKIP_SECRET_SCAN=1 git commit`, where the guard sees it and records it as `guard.bypass`;
  the next prompt names it, and `DEVKIT_GUARD=off` is recorded once per session and shown when a session starts.
- **Irreversible actions wait for your grant.** In the main session, these need a line you type on its own,
  `grant <action> <target>`: merging a pull request (`grant merge 142`), releasing a version, also by pushing its tag
  (`grant release v26.11.3`), a force push or a push to the default branch, a comment, review, or edit on a pull
  request or an issue, publishing a package, a deploy, a write through an API, and an MCP tool that sends, creates,
  or deletes something outside this Mac. The refusal names the exact line. A grant is used once, holds only in the
  repository of the session you typed it in, and expires after 30 minutes. A push of another branch and a new pull
  request are recorded, not held. For its first week on a Mac the gate only records what it would refuse, then it enforces;
  `safeguards.irreversible_main` sets it.
- **Some actions are yours alone, in every setting.** Lifting an embargo (`dev branch embargo --lift`),
  `dev backup --public`, and a change to a project's backup policy wait for a line you type: `grant embargo <branch>`
  or `grant backup <project>` (`grant developer-only <target>` works too). The gate's first audit week does not let
  them through, and crew members never run them.
- **An agent's push or pull request of an embargoed branch needs your `grant embargo <branch>`.** A push or pull
  request from a folder the guard can only learn when it runs (`cd "$X" && git push`, `GIT_DIR=$G git push`) is
  refused while any project on this Mac has an embargo; the agent writes the folder out instead.
- **A decision is yours only in your own words.** It counts when you answer the question dialog, or type a line of
  your own: `approve D3`, `approve D3: B`, or `grant push feat/invoice-export`. Without that, an answer the tech lead
  records is marked unverified, and an irreversible decision stays pending. `dev crew mode auto --approve` takes one
  target per action (`push feat/*`) and never pre-approves publish, deploy, a developer-only action, or a command the
  guard cannot name.
- **Crew members are named by role.** devkit's output, briefs, and refusals write a member as _Backend engineer
  (Tank)_ and the main session as the tech lead. A final message or a member's report whose prose names a member
  without the role is sent back once; the role alone is fine, in any language (`canary.roles`).
- **`dev` options are read exactly.** Every `dev` command refuses an abbreviated option (`--sched` for
  `--scheduled`), and a word after `--` is an argument, as the guard reads it.
- **`dev pr ready` needs local CI evidence for HEAD,** because marking a draft ready starts its one CI run. When every
  check of a pull request was skipped, `dev pr checks` reports no checks yet (exit code 8) and `dev pr merge` waits for
  the real run.
- **Only the tech lead plans and decides.** `dev progress start`, `assign`, `begin`, `reopen`, `skip`, and `done`, and
  `dev crew decide` and `mode`, are refused from a crew member and from a script. A second `dev progress start` on a
  live run needs `--replace --why "…"`.
- **The RACI is enforced.** `dev progress assign` refuses a member who is not Responsible for the step's kind of work,
  and `dev progress done` refuses to end a run before each Accountable member approved; `--why "…"` overrides both
  and is recorded. `dev progress start` and `add` refuse unknown, ambiguous, or duplicate step names and dependency
  cycles, and no step starts before its dependencies are done.
- **Crew members are held tighter.** They are refused every escape hatch (`DEVKIT_*` variables, `env -i`,
  `--ci-passed`, `--no-verify`, `dev config set`, new secret-scan exceptions). A review or read-only member's shell
  runs only a read-only allowlist; add your project's own checks with `safeguards.readonly_commands`. Members never
  merge, release, tag, publish, deploy, write through an API, or force-push. A member pushes a branch, backs one up,
  or opens a pull request only in a step allowed it (`dev progress assign --allow push,pr`), and only with your grant
  for that action or a decision of yours that names it; the step keeps the target and the repository that evidence
  covers, and opens nothing else. When a review or read-only member's shell call writes files, the tech lead gets a
  message, which names the working member whose scope holds a file, and the member's step ends unverified until the
  tech lead checks the diff. In the session scratchpad, each member writes only in its own `scratchpad/<member>/`
  folder.
- **A member's step ends done only on evidence.** That is a Check ✓ in its report, or its own
  `dev progress step --check`. Otherwise the step shows as unverified, for the tech lead to close with evidence or
  reopen (`dev progress reopen <step> --why "…"`).
- **Approvals are explicit.** Each finished step owes a verdict from the Accountable member of its kind of work,
  given after the work, for example by the Check ✓ of a review step that follows it. Nobody approves their own work:
  when the approver did it, the Code reviewer (Seraph) approves code and configuration, and the tech lead the rest. An
  approval needs the ✓ itself, so "tests not run" approves nothing, and a step with no approver cannot be approved
  until its kind of work is declared (`dev progress assign … --activity <id>`).
  `dev progress step --approves "<steps>"` and `dev crew ask --decision --approves "<steps>"` name the work a Check or
  your answer approves.
- **Running a devkit hook by hand is refused** in every session, because a hook event typed through a shell could
  forge your prompt or answer.
- **Audit records are kept 13 months**, on this Mac and in the handoff copy; this Mac used to keep two years.
  `dev config validate` refuses an `audit.retention.floor_months` under 3.
- **`dev ls`, `dev du`, and `dev status` show what is on this Mac.** A footer counts the other projects of the
  portfolio, and `dev ls --all` lists them with the `dev clone` command for each.
- **`dev gc` is the old name of `dev free`.** Without arguments it prints the plan to free space instead of a
  cleanup dry run, and `dev gc 3` frees item 3 of that plan. `dev gc --apply` still runs the unattended cleanup.
- **`dev dashboard` starts a local server** and opens a one-time sign-in link instead of writing a file.
  `dev dashboard --export <file>` writes the static page.

### Added

#### Install and update

- **A one-line install:** `curl -fsSL https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh | bash`
  installs Homebrew, git, Python, and the GitHub CLI when they are missing, clones the newest devkit release into
  `~/Development/fireflysoftwaresolutions/devkit`, signs you in to GitHub, and starts the setup wizard. Its questions
  come from the terminal even through the pipe; without a terminal (CI), it runs without questions and says how to
  finish. It says first that devkit is for macOS (Apple Silicon and Intel) and stops on any other system. New:
  `--no-setup`, `--help`, and the `DEVKIT_HOME`, `DEVKIT_REPO`, and `DEVKIT_REF` variables. Each step says in one
  plain sentence what it does and why; a failed step is named, with the command to run again; and the run ends with
  what you have now and what to do first.
- **A first install for people who are not developers:** a step-by-step guide in Getting started, with what you
  see at each step. Pressing Return at every question gives a working setup: the GitHub sign-in uses HTTPS and the
  browser, so it asks no questions about protocols or tokens, and the wizard's first screen says what devkit and the
  handoff repository are.
- **A pinned, read-only copy runs the hooks and the guard.** `dev install` copies the checkout's commit to
  `~/.local/share/devkit/releases/<version>-<sha>`, read-only and with a SHA-256 manifest. The hooks, the status line,
  `~/.local/bin/dev`, the scheduled jobs, and the Desktop launcher run that copy, so an edit to the checkout, broken
  or malicious, no longer changes the guard of every session at once. `dev doctor` fails on a changed copy or a hook
  that runs elsewhere, `dev install --version <version>` rolls back, and the newest three copies and any in use are
  kept. `dev update` pins the new release.
- **First-task tutorials for every surface:** Claude Code (Terminal and the IDEs), Claude Desktop's Code tab, Codex
  CLI, and Codex in the ChatGPT app, plus a page on Claude Desktop chat and Cowork. Each shows what you see on the way:
  the status block, progress updates, approvals, the crew graph, and the dashboard. Where to use devkit compares the
  surfaces. Codex is not verified live yet.

#### The live dashboard

- **`dev dashboard` is a live, read-only app on this Mac.** It starts a local server (or reuses one of the same
  version), opens a one-time sign-in link, and pushes each change to the open page, which updates in place: the
  route, the filters, the scroll position, and an open drawer stay. `--no-open` prints the link, `--status` and
  `--stop` manage the server, and `--export <file>` and `--path` write the static page. The server stops after 30
  minutes with no open page (`dashboard.idle_minutes`). It answers only GET and HEAD on 127.0.0.1, with a token or a
  same-site cookie, under a content security policy that allows only its own scripts.
- **Background work:** decisions waiting for you first, every run with its steps and owners, CI results with the
  first error and the next action, and scheduled jobs with their failed, overdue, and not-scheduled states.
- **One drawer for every detail,** the connected task (who has it, what blocks it, what is next), the crew with
  role-first names, and a live indicator with reconnecting and offline banners. No data is never "on track," every
  count names its window, and a contradiction shows where it occurs, with its fix. The navigation never scrolls
  sideways, from 320 to 1,600 pixels wide.

#### The crew

- **A RACI for every kind of work, led by the tech lead.** Each of 34 kinds of work has one Responsible member, one
  Accountable member, and the members Consulted and Informed (`dev crew raci`). The tech lead (Morpheus) routes every
  development request by it. Nobody approves their own kind of work: the Security engineer approves CI/CD, the Code
  reviewer approves security code, the Product owner approves UX, the Security engineer approves compliance, and the
  tech lead approves incidents.
- **Roles first, everywhere.** The crew views, the progress update and its Crew line, messages, decisions,
  delegation, dispatch, scope and slot refusals, the continuity texts, and every brief's report header write a member
  as _Backend engineer (Tank)_. The guard's refusals call the main session the tech lead. Stored names and the ids in
  commands (`--to tank`) do not change. The end-of-turn role check holds the prose to it (see What changes for you).
- **A procedure for every member:** phases, the skills and devkit commands to use, a gate, and a proof that devkit
  checks. Each brief carries the knowledge of its kind of work (skills and devkit guides), the stack skills of its
  files, and the known pitfalls for the task.
- **Kinds of work in any language.** A step's kind of work comes from what you declare (`--activity` on
  `dev progress assign`, `[id]` in a plan spec), then from the member's role, then from its files. Words are only the
  last guess and never refuse work. Lessons work the same way: they carry anchors (files, commands, kinds of work,
  error signatures, stack), set with `dev lesson add --file`, `--command`, and `--error`, or `dev lesson anchor`.
- **The crew gates.** When a member stops, devkit reads its transcript and sends it back once for a missing proof, a
  skill it did not load, unfinished work, a test that cannot fail, or a missing lesson after repeated refusals.
  `crew.gates.enforce: false` only records them.
- **Lanes and helpers.** Each member's lane binds to its own launch, so members launched together never take each
  other's name or step, and a step binds to a working lane only when that lane's launch names it. A member may launch
  narrow helpers, which get its tools and files, take a slot, and stop at `progress.max_depth` (2).
- **Crew slots.** `progress.max_parallel` (4) sets how many members work at once, and your recorded usage lowers it:
  `progress.quota_steps` keeps a share of the slots from 50, 70, and 85 % usage, the 5-hour and the weekly windows
  both count and the lower limit wins, and one member works at a time from `progress.quota_percent` (90). A
  review-only or read-only member costs half a slot and the Lookup scout (Sparks) nothing (`progress.slot_weights`).
  `progress.reserve` (1) keeps a slot for what you ask while the crew works. `dev crew next` ranks the ready steps by
  critical path, then your latest request, then age, and records that order; a launch out of it needs
  `dev crew next --first <step> --why "…"`. The slots in use and why the limit is lower show in `dev crew`,
  `dev crew next`, and the status block's Crew line, and each change of the limit is audited (`crew.limit`). A member
  stopped with TaskStop frees its slot at once. A team's `policy/progress.json` can set a ceiling, and you change the
  keys with `dev config set`, in a terminal.
- **Who is calling.** A call ledger records which agent made each shell call, so a `dev` command knows whether the
  tech lead or a member ran it, and two sessions in one repository each update their own run.
- New commands: `dev crew graph` (the live session as one graph; `--mermaid`, `--json`), `dev crew rules` (every
  crew rule and the check that enforces it), `dev crew raci --set`, `dev crew set`, and `dev crew history` (the team's
  changes, kept in the handoff repository with who and why), `dev crew decisions --ask`, `dev task reopen --why`, and
  `dev agents rules [--section]`.
- The status block gains optional **Crew** and **You** bullets, and `dev status` shows each on its own rows.
- Claude Desktop chat and Cowork get the full global rules through the extension's new `devkit_rules` tool.

#### Continuity

- **A session keeps its work through compaction, resume, and `/clear`,** on Claude Code and Codex. Before a
  compaction, devkit writes a checkpoint: the run and its current step, the crew working now, the decisions and
  messages waiting, your last 5 requests, and git. In Claude Code's main session it also tells the summarizer what to
  keep. After the compaction or a resume, the session gets the continuity brief first (at most 4 KB), and in Claude
  Code the next hook names what the summary left out ("Not in the summary"). A member whose own context was compacted
  gets a reminder of its role, step, scope, and report format. After `/clear`, the new session hears of the old
  session's open run and gets `dev session carry`. See [Continuity](docs/continuity.md).
- **Your requests stay private to the session.** They are kept redacted, cut to 1,000 characters, only for turns of
  development work, for 7 days at most, and only the session's own provider sees their text. The summary itself is
  never stored. `dev session forget` deletes the records.
- **`devkit_resume`,** a read-only tool of the devkit extension, gives Claude Desktop chat and Cowork the same brief for
  a project's latest session, with your request text only when the app is that session's provider.
- The global instructions tell the agent that devkit's record wins over a summary, and what to keep when it writes
  one.

#### Branches, backups, and pull requests

- **The branch map:** one row per local branch with its upstream, how far it is ahead of and behind the upstream and
  the default branch, the commits that exist only on this Mac and the branch that carries them, its last backup, an
  embargo, its worktree, its pull request, its linked task, and whether its work is safe off this Mac, with the
  reason. `dev branch ls` prints it (`--json`), `dev status` has a Branches section, `dev overview` counts the
  branches not backed up and embargoed, the handoff note's generated block lists the branches, the session-start
  context names the branches only on this Mac, and the dashboard has a Branches tab on each project page.
- **`dev backup [<branch>…] [--all]`** copies the commits that exist only on this Mac to the remote, under the same
  branch name, when that push starts no CI run. It reads the CI configuration of the commit it sends (GitHub
  Actions, GitLab, Bitbucket, and Azure) to tell. It never pushes the default branch, a long-lived branch (`main`,
  `master`, `develop`, `trunk`, `gh-pages`), or a branch a workflow deploys from, never forces, and stops at a
  diverged remote branch with the commands to resolve it. Its WIP, embargo, security-fix, and secret checks read
  exactly the commits the push target lacks, also when another remote has them, and it reads visibility and pull
  requests on the repository the branch's remote names. `--accept-ci` pushes anyway and counts against the push
  budget. `dev sync` backs up the branches
  that need it every hour and at the end of a session, for commits older than `backup.after_minutes` (30);
  `backup.auto: false` turns that off.
- **A backup policy per project:** `dev backup policy on|off|ask --why "…"` records it in the project's handoff note,
  for every machine and the team. Without one, a private repository is on and a public one is ask: `dev backup`
  asks you once, in your own terminal, and records the answer. `dev sync` and agents are never asked and never
  publish.
- **Embargoes:** `dev branch embargo <branch> --why "…" [--until …]` holds the pushes of a branch, and of the commits
  it held after a merge, until you lift it (`--lift`): from its project, its git folders and worktrees, temporary
  clones, and folders that are no project (see Known limits). An embargo is kept in the handoff note's metadata, so every
  machine sees it, and a note devkit cannot read stops every backup. On a public repository, a branch whose commits
  read as a security fix is not backed up: devkit suggests an embargo, and `dev backup --public` pushes it anyway.
- **Draft pull requests early, CI once.** With `delivery.draft_pr` on, `dev pr create` opens a draft unless
  `--ready`, and a draft needs no local CI evidence where the workflows skip drafts. `dev pr ready` checks the
  evidence for HEAD, then marks the draft ready, which starts its one CI run. `dev ci audit` flags workflows that run
  every job on drafts or skip drafts without `ready_for_review`, and `--fix` and `dev ci init` write the draft gate.
- **The push budget counts only pushes that start CI.** devkit reads whether a push starts a run, and counts it on
  any doubt.
- `dev task start` links the task to the branch it runs on, in the private handoff, so task ids stay out of branch
  names.

#### Commit messages

- **One template for every commit:** `config/git/commit-template.txt`, with the header, why, what changed, and how it
  was verified, then the `Refs:` and `BREAKING CHANGE:` trailers. `dev commit --why "…" --verified "…"` writes it,
  `--body` (repeatable) adds what changed, `--refs` and `--breaking` add the trailers, and `Refs:` comes from the
  ticket of the task started on the branch. A message that names a devkit task id is refused.
- **The template in your editor:** `dev setup` asks whether new commit messages open with it, the git step of
  `dev install` sets `commit.template` in each repository that has none of its own, and `dev setup commit-template`
  asks again (`--remove` takes it out). See [Git and GitHub › Commits](docs/git-and-github.md#commits).

#### The terminal

- **Project aliases:** `dev alias <alias> [project]`, `dev alias rm <alias>`, and `dev alias` to list them, shared
  with the team through the handoff repository. Names match by exact id or alias, then repository name, then start,
  then any part; a tie lists every match with its command instead of guessing. `dev status`, `go`, `open`,
  `wt new --repo`, `claude`, and `codex` accept them, so `dev status devkit` works from any folder.
- **A new `dev status`:** it shows Needs you first, then Right now, the last update in full, details, and the footer. It wraps
  at 100 columns, fits 80, and reads the same with `NO_COLOR`; `--json` returns the same data.
- **`dev config` is a report:** each difference says whether it is new in this devkit version, changed outside
  devkit, or removed outside devkit, and names the fix. `dev config --check` prints one line and exits 1 on a
  difference.
- **A devkit helper in your shell prompt,** if you want it: `dev setup prompt` asks first, in your own terminal only
  (an agent's call changes nothing), and `dev install` asks once. Inside a devkit project it shows the project's
  alias, how many crew members are working, your branch's checks, and a question that waits for you; outside, nothing.
  Nothing runs when the prompt is drawn: the hooks (a tool call at most every 5 seconds; a prompt, the end of a turn,
  and a member that starts or stops at once), `dev status`, `dev alias`, `dev prompt refresh`, and CI watch results
  write its facts to a cache, which a zsh function reads (0.13 ms per prompt, measured). The function reads the cache
  only when it is a regular file of yours that nobody else can write, and takes only digits from it as numbers.
  Members count while their heartbeat is under 15 minutes old, the checks hide when you leave their branch, and after
  24 hours without a refresh only the alias stays. Starship gets a marked block in `starship.toml`, saved first; plain
  zsh gets devkit's own block, so `~/.zshrc` is not edited; oh-my-posh and Powerlevel10k get snippets.
  `dev setup prompt --remove` takes it out, and `dev prompt` shows what it shows here. See
  [The prompt helper](docs/getting-started.md#the-prompt-helper).
- **`dev config set <key> <value>`** changes one key in a terminal: it validates the value, writes the file
  atomically, prints the old and the new value, and records `dev.config.set` in the audit trail. Agents are refused.
- devkit keeps the whole status block of a final message (up to 8,000 characters), not its first 700.
- Shell completion covers every command and the verbs of `dev task`, `progress`, `crew`, and `agents`.
- `dev doctor` checks that the skills the crew names are installed for each agent. The setup wizard offers optional
  skill groups for the tools you pick, such as `iac` for Terraform.
- **`dev session`** shows and manages what devkit keeps of a session through compaction, `/clear`, and resume:
  `brief` prints it (`--latest` for the project's newest session with an open run), `carry` moves the run into this
  session after `/clear`, `compactions <transcript>` counts a Claude Code transcript's compactions, and `forget`
  deletes the records. `brief`, `carry`, and `forget` are the tech lead's. See Continuity for the hooks that fill
  these records.

#### Disk

- **`dev du` explains what fills your disk.** For each project on this Mac: the main checkout, managed worktrees,
  Claude Code and Codex worktrees, and copies outside the layout, each with its largest ignored folders. Then
  Colima's disk and the space macOS has not got back, agent data and caches with whether each is safe to clear, and a
  plan to free space, ranked by size, with each item's safety, risk, and command. It deletes nothing, and answers in
  under 2 seconds from its cache (`--refresh`, `--plan`, `--json`, or one project by name). See
  [Disk use](docs/disk.md).
- **`dev free <n>`** frees items of that plan by number, or every safe one with `--safe`. It lists what it deletes,
  asks once, checks again at that moment for changed files, unpushed commits, and programs working in the folder,
  and deletes nothing when a newer measurement changed the plan you saw. It keeps a build folder that holds files git
  tracks, never prunes Docker volumes, and records every deletion in the audit trail.
- **`dev docker trim`** gives Colima's free space back to macOS without deleting anything.

#### The audit trail

- **Schema v2:** every record has a sortable id, who acted (and the crew member, lane, and launch), the project,
  branch, run, and step, a category and severity, the reason for an override, and its evidence. Older records still
  read. See [The audit trail](docs/audit.md).
- **A hash chain:** each record is linked to the one before it, and `dev audit verify` reports the first edited,
  removed, or inserted record, using the published copy as the anchor.
- **Retention you set:** `audit.retention.local_months`, `team_months`, and `floor_months`, `audit.hold` for a legal
  hold, and `audit.level` (`security`, `changes`, or `all`). A team policy in the handoff repository can only raise
  them.
- **Queries and exports:** `dev audit show`, `stats`, and `config`, filters by date, project, member, category,
  severity, outcome, and text, `dev audit export` as JSONL, CSV, OCSF, or CEF, and `audit.forward` to a file or
  syslog.
- **Personal data:** the audit trail page says what is recorded and never recorded, who receives it, how long it is
  kept, and how the people recorded use their GDPR rights.

### Changed

- A first `dev setup` on a Mac with no projects asks which languages to set up, with Python and Node preselected,
  instead of installing Java, PHP, Node, and Python without asking.
- Claude Code: `attribution.sessionUrl` is `false`, so cloud and Remote Control sessions add no `Claude-Session`
  trailer either; the deprecated `includeCoAuthoredBy` key is removed; and `BASH_DEFAULT_TIMEOUT_MS` is 5 minutes,
  so a test suite or a build in the foreground is not cut off at 2.
- Codex: `analytics.enabled` is `false`. `dev doctor` also reads whether your Codex account adds commit and PR
  attribution (an account setting, not a `config.toml` key) from your newest session.
- `dev doctor` reads Claude Desktop's own switch for bypass mode in the Code tab (Settings → Code → Allow bypass
  permissions mode) and says whether it is allowed, off, or not offered for the signed-in account.
- `dev update` follows releases: a checkout at a release tag moves to the newest `vYY.MM.PATCH` tag (compared as
  numbers, so 26.11.10 is newer than 26.11.2) and then runs `dev install`; at the newest release already, it says so
  and changes nothing. A checkout on a branch still gets `git pull --ff-only`. It refuses, with nothing changed, while
  tracked files have local changes or when the checkout is at a commit that is neither a release nor a branch.
- The wizard's line prompts say so when no terminal can answer and every question takes its default. Without a
  GitHub sign-in, the wizard says why it needs one and prints the exact command.
- The PostToolUse hook shows the status block right after a call that usually ends a turn (a subagent launch, a
  monitor, a background command, or a closing `dev` command), with the project and branch filled in. A note
  written after the last tool call no longer lands before the block, so the Stop hook does not have to send
  the turn back.
- **Lessons are recalled by what a call does.** A session gets the catalog of kinds of work, skills, and lesson
  anchors at its start. A tool call is reminded of the lessons anchored on its command or file, and its output of
  those whose error signature it shows, each at most once an hour; every refusal names the lessons of the call it
  refuses. `dev sleep` merges lessons that share anchors, and with background AI on (`ai.*`, opt-in, with a daily
  budget), a small model also merges lessons written in different languages.
- Skill rules trigger on files, commands, and declared kinds of work, and a turn counts as development work from
  what it did, never from your words.
- The dashboard counts a run as working while the run or any of its crew lanes reported in the last 30 minutes,
  "of about" is the plan's current total (active time plus the time left), lists show a step's title without codes
  such as F4, and a member's step that stopped without evidence shows as unverified, for the tech lead to check, never
  under Needs you.
- **Fewer turns sent back.** Before the end of a turn, and after every commit, devkit lists the end-of-turn items
  still open (the handoff note, required skills, tests that cannot fail, untracked stubs), each once per turn, so the
  agent settles them before the Stop hook would. The handoff gate sends a turn back only at
  `handoff.block_after_commits` (5) commits newer than the note, when the oldest is `handoff.block_after_minutes` (60)
  old, or after a milestone: the end of a run, a work item closed, a push, a pull request, or a release. An
  intermediate step is not a milestone, a milestone counts only the commits made before it, and while crew members
  work the main session gets the reminder only. Each send-back is audited as `stop.block` and each item settled after
  a reminder as `stop.prevented`; `dev canary` opens with the turns sent back per day and rule. Codex gets the same
  reminder after a shell call, not verified live yet.
- **devkit's own test suite is hermetic.** Run it as a package, `python3 -m unittest discover -s tests -t .`:
  `tests/__init__.py` gives the run a temporary home and refuses, and records, any access to your real devkit state,
  configuration, install, handoff repository, or agent homes. CI, the contributing guide, and the docs use that
  command, and the consistency checks keep them on it.
- Estimates are rounded to 5 minutes, and to the minute under 10 minutes, on the dashboard and in `dev progress`:
  _of about 2h00m_, not _1h59m_. Measured times stay exact.
- Each hook reconciles its session's own run: a member that stopped without saying so, a lost launch, or a run whose
  session ended is marked as such, so the run, its slots, and the dashboard stay true.
- `dev doctor`'s fix for a full disk names `dev free`.

### Fixed

- The audit trail's copy in the handoff repository never grows past 45 MB in one file: a month that would pass it is
  held, with a warning in the trail and in `dev doctor`, instead of growing toward GitHub's 100 MB limit and stopping
  every handoff push. Daily, compressed audit files replace the monthly ones in 26.11.5.
- `dev install` no longer stops at the skills step on a fresh clone or a release archive: git keeps no empty folder, so
  devkit's own `agents/skills` folder is missing there, and the step now links the third-party skills without it.
- `install.sh` works on Intel Macs: it finds Homebrew in `/usr/local` as well as `/opt/homebrew`, and runs the wizard
  with Homebrew's Python from `brew --prefix` instead of a fixed `/opt/homebrew/bin/python3`. On Apple Silicon, it adds
  Homebrew to `~/.zprofile` when Homebrew was not on the `PATH`, so new terminals find `brew`, `gh`, and the toolchain.
  The shell profile and `dev setup` also find Java and Docker's CLI plugins under Homebrew's real prefix.
- Hooks and `dev` commands no longer lose each other's changes: one lock per session covers its session and run
  records, so of 8 crew questions asked at once, all 8 are kept (it was 1), and an ended session no longer comes back.
- Crew members launched together no longer swap names, lanes, or steps.
- A step assigned to a member who is already working waits for its dependencies instead of starting.
- The Stop hook waits while crew members are still working on a plan, instead of sending the tech lead back each
  turn.
- Crew message and decision ids stay unique past 200, so an answer no longer reaches the wrong member.
- A crew member working in a worktree of the same repository can edit the files of its scope.
- Read-only crew commands (`graph`, `show`, `raci`), commit messages, and heredocs that only mention a lane command
  are no longer refused to members.
- The prompt hook no longer logs an error on every prompt and loses the turn's class.
- A reply with no tool calls (a status question, "continue") counts as the previous turn's kind, so a missing status
  block there is scored.
- `dev doctor` informs, instead of failing, about a crew skill from an optional group you have not turned on.
- The `devkit_audit` tool shows who acted again, and `devkit_task_update` says that it changes tasks and that only
  the audit trail, not the guard, covers it.
- `dev status` shows a saved status block's Crew and You lines on their own rows and wraps long values.
- The irreversible-action gate's audit record failed on every write, so a crew member got the gate's error text
  instead of its refusal. Records are written now, and a record that fails cannot change the gate's decision.
- Review and read-only members can run `dev test prove`, which works in a temporary copy of the checkout.
- The Accountable or a Consulted member of a kind of work could take it without `--why` and then approve it. Only
  the Responsible member takes it now; the others need `--why`, which is recorded.
- `dev gc <n>` is recorded in the audit trail as a deletion, while listing aliases and printing the plan to free
  space are not recorded, because they only read.
- **Review readiness.** A review step waited forever behind a member's unverified step, which only that review could
  approve. A review step now starts on unverified work, and its Check ✓ ends done the steps it follows that its
  member approves; every other step still waits for done or skipped. One rule tells a review step everywhere: its
  kind of work is review, or its member works review-only.
- **Step binding.** A step assigned to a member whose lane was still running could close done when that lane ended,
  with none of its work. A lane's ending now closes only the steps its evidence is about: the step its launch names,
  a step its `dev progress step --step "<name>"` names, or one its report names. Its other steps go back to pending.
- A file that a working member changed during a review member's shell call no longer counts against the reviewer:
  it is flagged as possible, and the message names the member whose scope holds it.
- `grant unknown cmd-<hash>`, which a refusal of a command the guard cannot name asks for, recorded nothing, so the
  command could never run. The grant opens that one command, once.
- A decision's options reached no audit record, so every answer read as free text. Records now keep the option an
  answer names.
- `dev test`, `dev test prove`, and `dev ci --run --resume` run a test folder that is a Python package from the
  repository root, so its `__init__.py` setup runs.
- `dev stubs` needs the stub word and its time word in one clause, so a comment that says nothing is stubbed is not a
  finding.
- The status block that `dev progress step` prints rounds estimates like the dashboard.
- A member's `dev crew ask` or `dev progress step` whose message comes from a quoted heredoc reaches its lane
  instead of being refused.

### Security

- devkit trusts only its own Codex hooks. Before, it told Codex to trust every new hook in `~/.codex/hooks.json` at
  each session start and every 6 hours, so a hook an agent wrote there would later run outside the guard. Now a hook
  counts as devkit's only when it matches exactly what devkit renders (event, matcher, command, timeout); any other
  hook stays untrusted, and `dev doctor` lists it among the hooks devkit did not write.
- The guard sees what a command really runs: through wrapper flags that take a value (`nice -n 5`, `env -u X`,
  `sudo -u me`, `exec -a x`), interpreter flags (`python3 -u bin/dev`), arithmetic shifts that looked like heredocs,
  git and gh aliases, shell scripts run by their path, and `git push --repo=<url>`. `env -S` is refused, because the
  guard cannot see the command it runs.
- Writes with a relative path from devkit's state or configuration folder, or from a folder that holds them, are
  refused.
- A `WebFetch` URL that carries a secret is refused.
- **Your approvals cannot be forged.** devkit reads your answers and grants only from the session's own transcript,
  where Claude Code writes it; an event that names another file records nothing and is audited as
  `crew.evidence-refused`, and a prompt or answer replayed through a hook counts once. Agents cannot write, move,
  or delete Claude Code transcripts or Codex sessions. A program an agent runs that loads devkit's code to reach these
  functions or devkit's state is refused, also when it loads them through the agent's own files (three levels deep)
  or through code decoded at run time. On Codex, devkit reads no transcript yet, so a grant or an answer you type
  there opens nothing: run irreversible actions on Codex yourself, in a terminal.
- **A "no" never opens an irreversible action.** A decision that leads to a release, merge, push, or deploy opens it
  only when your answer chooses the option that performs it (`--performs`, else the one option that reads yes or
  approve, else the recommended one); before, any verified answer opened it, `approve D1: no` included. Any other
  answer opens nothing and is audited as `crew.grant-refused`, with the decision and the option, never your text.
  `dev crew ask` refuses a `--performs` that names an option reading as not doing it (`B: no`), and the question
  you are asked names the choice that runs the action and its target: `D1 · Release now? (choosing “A: release”
  runs release v26.11.4)`.
- The irreversible-action gate sees more: the repository that `gh -R`, `--repo`, or `GH_REPO` names (so a grant for
  one repository never covers another), commands run through `script`, `watch`, GNU `parallel`, `sem`, `arch`,
  `flock`, `unbuffer`, `chronic`, or `yarn workspace`, targets that `xargs` appends, `curl` methods inside short-flag
  clusters (`-sXPOST`), and `npm -w <pkg> publish`.
- A forged hook event can no longer end a working member's lane and free its slot.
- The audit redactor closes the 14 gaps a probe of 32 fake secrets found (among them names like
  `AWS_SECRET_ACCESS_KEY`, quoted values, `--password` flags, PGP private keys, and Stripe, GitLab, Hugging Face,
  Google OAuth, Docker, Slack, and Azure tokens), and also knows Bearer tokens, JWTs, npm and PyPI tokens, Google API
  keys, and URL credentials. It also redacts `curl -u`, `sshpass -p`, `mysql -p`, `docker login -p`,
  `gh secret set --body`, `Authorization: token`, `passphrase=`, `credentials:`, and a value piped by `echo` or
  `printf` into `--password-stdin` or `--with-token`. The redactor knows these shapes and no others: a secret in a
  shape it does not know can still reach a record, so keep secrets out of commands.
- Records hold less about people. Crew message text, decision questions, and the user name in home folders are left
  out; a decision's answer is kept only when it names one of the options (otherwise its length), and your approval
  lines only by their length. Records written before are cleaned the same way when read, and `dev audit verify` still
  checks them as stored.
- The dashboard server checks the `Host` header, sends `base-uri` and `form-action` rules in its content security
  policy, keeps its token out of process arguments, and reads files only inside the handoff repository. Search
  results and the command palette open a link only after the page's URL check, so a `javascript:` or `data:` link in
  the data cannot run, in the static export either.
- **The guard was hardened over three red-team rounds.** Each way around it that a round found is closed, and a test
  in `tests/test_red_team.py` or `tests/test_red_team_2.py` keeps it closed. The classes closed:
  - A command whose program or subcommand is known only when it runs is the `unknown` action: a crew member never
    runs it, and the main session runs it once with `grant unknown cmd-<hash>`, which the refusal names.
  - A command that git or gh hands to a pager is judged as the command it is, and agents may not write a pager or
    any other git setting that runs a command (in any scope, from the environment, or in a repository's own `.git`
    folder, whose config and hooks agents can no longer write).
  - A nested agent session that would run without devkit's hooks or sandbox is refused, also when a package runner
    or a program's own code starts it, and agents may not pass a Codex profile.
  - Running one of devkit's hooks by hand, and `dev config lock` and `unlock`, are refused from every tool call.
  - An MCP tool that merges, pushes, or approves counts as an external action, like one that creates or sends.
  - `dev` flags are read the way devkit's parser reads them, so an abbreviation or a word after `--` hides nothing.
  - A heredoc that macOS's bash ends earlier than zsh fails closed, and text inside an interpreter's string literals
    is no longer read as program names.
  - A backup is a push to the guard, granted per branch, and the push budget counts only pushes that start CI.
- **devkit runs isolated.** `bin/dev` runs `python3 -I -S`, and so does every devkit process it starts (hooks in
  the background, the MCP server's calls, scheduled jobs, the wizard, the CI watcher, the dashboard server, the
  Desktop launcher), so a `PYTHONPATH`, a planted module, a user site folder, or a `.pth` file cannot load code into
  it. The guard refuses Python's code-loading variables on a devkit call and on a nested agent session.
- **The gates fail closed.** When the irreversible-action gate itself fails while it enforces, a command that names an
  irreversible action is refused (`guard.irreversible` with a gate error), and so is an MCP call. A crew member's
  shell command that devkit cannot check against its lane is refused instead of run unchecked. Other handlers still
  fail open, with a `guard.failopen` record.
- A grant a step uses for a push or a pull request covers only its own target pattern in its own repository.
- The dashboard server reads a project's branch map at most every 5 seconds and two at once, answering more with
  429, so a client cannot start unbounded git reads.
- An agent's push from a folder that is no project (a temporary clone, a folder made in the same command, one a
  sourced file picks) is refused while any project has an embargo, and a push from inside a git folder, or through
  `env -C` or `sudo -D`, is judged as that folder's project, so its embargo holds.

### Known limits

- **The guard is a seatbelt, not a sandbox.** It catches an agent's mistakes and the common forms of prompt
  injection, but a determined program can still find a form it does not recognize. Keep your own review; see
  [Guard rules › What the guard is and is not](docs/guard.md#what-the-guard-is-and-is-not).
- **Developer grants on Codex are not confirmed yet.** devkit reads no Codex transcript, so a grant or an answer you
  type in Codex opens nothing: run irreversible actions there yourself, in a terminal.
- **Agents use your credentials.** devkit works with the GitHub account you are signed in to with `gh` and your own
  tokens and keys; there is no separate identity for agents, by design. The guard and your grants hold the
  irreversible actions.
- **An embargo holds pushes from its own project only.** It holds pushes from the embargoed project, its git folders
  and worktrees, temporary clones, and unknown folders; a copy of its commits in another project's folder is not
  held yet (26.11.4).

### Removed

- The word lists: skill rules no longer match your prompt (15 patterns removed), the prompt hook no longer looks for
  corrections in it, and `dev lesson scan` no longer lists corrections.
- The static-file-only `dev dashboard`: the static page is `dev dashboard --export <file>`.

## [26.11.2] - 2026-11-06

### Added

- A security policy (`SECURITY.md`) with private vulnerability reporting on GitHub, and a contributing guide
  (`CONTRIBUTING.md`), now that the repository is public.

### Changed

- The release process in Developing devkit uses `dev pr merge`, `dev release`, and a check of the published archive.

### Fixed

- `dev agents test` reads the hooks log from the byte offset it recorded, so the guard check no longer reports
  "no guard decision logged" when earlier lines hold multi-byte characters.
- `dev ci --run --resume` reruns the steps that read every file (secret, filesystem, and docs scans) after a
  docs-only change: a document can leak a key too. Builds and code linters still keep the docs shortcut.
- The Stop hook asks for `dev plan revise` only for the plans of the project the session works in. With two sessions
  open, it no longer sends one back over a plan the other changed in another project.
- `dev doctor` compares the installed instructions (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`) and the Codex crew
  roles with what this devkit writes, so an outdated install shows as out of date instead of passing because the
  files exist.
- The installed-plugin check compares the newest copy by version number, so 26.11.10 counts as newer than 26.11.2.

## [26.11.1] - 2026-11-06

devkit is open source under the Apache License 2.0.

### Added

#### Testing

- **`dev test`** runs the tests that failed last time first, then only the tests your changes can affect (the blast
  radius), with pytest, unittest, Jest, Vitest, Maven, Gradle, Go, PHPUnit, or Pest. `dev test run` takes paths,
  `--failed`, `--affected`, or `--all`; `dev test plan` shows what would run and why; `-p <project>` works from
  anywhere. A selection that runs no test exits with `2` instead of passing.
- **Tests that can fail.** `dev test audit` finds tests that cannot fail for a real bug (no or trivial assertions,
  empty bodies, swallowed failures, skips without a reason, sleeps, mock-only and smoke-only tests) in Python,
  JavaScript and TypeScript, Java and Kotlin, Go, and PHP. `dev test prove` checks that each new test fails without
  your change and passes five times with it. The Stop hook sends back a development turn whose new or changed tests
  cannot fail.
- **`dev ci --run --resume`** repeats only the failed steps, the steps that never ran, and what the fix affects. Its
  evidence combines the full run with the affected tests, and the pull request says so.
- The `testing` skill: a test strategy per kind of code, TDD, flaky tests, mutation testing and diff coverage, and
  diagnosing an existing suite. Required whenever a turn writes or runs tests.

#### CI/CD

- **`dev ci audit`** diagnoses the pipelines of GitHub Actions, GitLab CI, Bitbucket Pipelines, and Azure Pipelines:
  security, reliability, speed, quality gates, and delivery, each finding with a severity and its fix, merged with
  actionlint, zizmor, and `glab ci lint`. `--fix` applies the safe GitHub fixes (permissions, auto-cancel, job
  timeouts, actions pinned by SHA) and prints the snippets for the other hosts; `--strict` fails on medium findings.
- **`dev ci init`** writes a pinned pipeline for the detected stack and host: lint, tests with a JUnit report,
  security scans, and on release tags an SBOM, signing or attestation, and a check of what was published. On GitHub
  it writes `ci.yml`, a cache-free `release.yml`, and `dependabot.yml`; GitLab signs keyless, Bitbucket and Azure with
  a cosign key.
- **`dev ci watch`** starts after every push and pull request, follows the runs to the end, reads the first error of
  a failure, sorts it into a class, and gives one next action. The main session gets it as a crew message and hands
  fixes that are not trivial to Link.
- The delivery-policy field `job_timeout_minutes` (default 30) sets the job timeout that `dev ci audit` suggests,
  `--fix` writes, and `dev ci init` generates; a generated test job runs the policy's test gate.
- The `cicd` skill: fast, reliable, DevSecOps pipelines on any host. Required whenever a turn changes a pipeline.
- devkit's own CI runs a security job (gitleaks, semgrep, osv-scanner, trivy, zizmor), and `actionlint` joins the
  security tools.

#### The crew

- **25 specialist agents** named after The Matrix and led by the main session as Morpheus, from one roster
  (`config/crew.json`) for Claude Code, Claude Desktop, Codex (as roles), and the ChatGPT app. Each member has its own
  lane and time, the files it is assigned (the guard keeps its edits inside them), and a status block it must end its
  report with, which the SubagentStop hook checks (the crew canary). At most four members work at once.
- **`dev crew`**: the roster and who works now (`show`), briefs with the launch line (`brief`), routing (`route`),
  delegation (`delegate`, `decline`), the dispatcher (`next`), messages (`ask`, `answer`, `inbox`), decisions (`mode`,
  `decide`, `decisions`), and `sync` for the generated agents.
- **Guided or automatic.** In guided mode, decisions that are yours wait for you; in auto mode, the crew takes the
  recommended option and records it for review. Irreversible actions wait in both unless you pre-approved them.
- The `crew` skill: who to send for each kind of work, parallel or in sequence, briefs, and reports.

#### Progress

- **Time you can trust.** Active time (turns and subagents, never the wait for the developer), per-step estimates,
  dependencies (`after`), a critical-path ETA shown as a range, and a pace that one odd step cannot swing.
- **A plan that stays true.** Closing a step needs its evidence (`--check`) and a description that matches it;
  `dev progress add`, `reopen`, `skip`, `history`, and `assign --scope`; every change is recorded and audited.

#### Unfinished work

- **Build it for real.** `dev stubs` finds unfinished work in a change or a repository: `TODO` and `FIXME` markers,
  "not implemented" code, placeholders, and empty Python bodies. The Stop hook sends back a development turn that leaves
  untracked ones, and the guard refuses a commit that would include them. Planned work names its task (`TODO(T-…)`) or
  issue.
- The global rules: every test must be able to fail for a real bug; after a failure, re-run only the failed tests and
  what the fix affects; the full suite once, before the push; no stubs or placeholders in delivered work.

#### Surfaces

- Codex gets the plugin's slash commands as skills of the same name.
- New slash commands: `/devkit:progress`, `/devkit:crew-status`, `/devkit:test`, and `/devkit:stubs`.
- The Claude Desktop extension has 18 tools, among them `devkit_crew`, `devkit_stubs`, `devkit_test_plan`, and
  `devkit_ci_audit`.
- The dashboard's run card shows active time, the ETA range, the steps, and the crew's lanes.

### Changed

- **The handoff note stays current.** After every development turn with new commits, the Stop hook asks for a note
  and task update when your commits are newer than the note's last real change (task and session lines written by
  devkit do not count). It also works for sessions that started outside the repository, such as Claude Desktop's.
- The scout is now Sparks, a member of the crew; `scout` remains an alias in Claude Code and Codex.
- The UI skills are required for design work only: "open the dashboard" no longer asks for them.

### Fixed

- `dev sec scan` kept the leading dot of hidden paths in zizmor results: `./.github/workflows/ci.yml` no longer
  becomes `github/workflows/ci.yml`.
- The crew canary counts only subagents someone launched. Claude Code's own helpers, such as the compaction
  summary, are no longer counted as crew misses or sent back, and a report corrected after the reminder is
  recorded as corrected.
- `dev progress note` sends an update while a step is still running, so no update has to close a step; the
  silence reminder and the global rules now ask for a note, not a step, until the step is finished.
- `dev test prove <file>` proves only the file's new or changed tests, so tests that already existed are no
  longer reported as not testing your change.

## [26.11.0] - 2026-11-05

The first release.

### Added

#### Workspace and setup

- **Workspace layout** for any git host, with GitHub repositories in `~/Development/<owner>/<repo>`, other hosts in
  `~/Development/<host>/<group>/<repo>`, projects without a remote in `_local/`, managed worktrees in `.worktrees/`,
  and scratch files in `.tmp/`. `dev clone`, `dev new`, and `dev adopt` keep repositories in place.
- **Setup wizard** (`dev setup`, started by `install.sh`): it detects the Mac and both agents; asks for the workspace,
  the handoff repository (yours or a shared one), the agents and their permission modes, your stacks, your git hosts
  and clouds (preselected from what the Mac already shows), the Docker VM, automation, and the settings lock; shows
  the plan; installs only the packages you need; applies the configuration; offers the sign-ins; and runs
  `dev doctor`. In a terminal it is a full-screen wizard driven with the arrow keys, with **Recommended setup** (what
  it detected, straight to the review) or **Customize**, a review with the packages per group and **Change a section**,
  and an apply step with live progress and the fix for each failure. A second run starts from the saved choices and
  marks what would change. Pipes, `TERM=dumb`, and `--yes` keep the line prompts; `--yes` accepts every default.
- **The `dev` home and banner:** the hand-drawn devkit wordmark with the amber spark as the dot of its `i`, and the
  tagline "Every repo in its place. Every agent on the same page." (also in the README hero and the plugin
  descriptions). The home shows the current project in three lines, the workspace in one, and the items that need
  attention, grouped, each with its command. Headings are a bold title with a rule.
- **Git hosts and clouds:** the configuration keys `hosts` (GitHub, GitLab, Bitbucket, Azure DevOps) and `clouds`
  (AWS, Azure, Google Cloud, DigitalOcean, Kubernetes, and OpenTofu or Terraform) choose which CLIs devkit installs, from
  Brewfile groups of the same names, and `dev doctor` checks that each is installed and signed in, with local checks
  only.
- **`dev install`**, safe to run again, in steps: `config`, `tools`, `cli`, `git`, `handoff`, `instructions`,
  `skills`, `claude`, `codex`, `docker`, `docs`, and `launchd`. The `tools` step installs only the packages that are
  missing for your stacks, git hosts, and clouds (`packages.auto`). `dev update` pulls devkit and applies it again.
- **`dev doctor`** checks the toolchain, GitHub sign-in and git identity, the workspace and handoff repository, the
  instructions and skills, Claude Code and Codex (settings, hooks, hook trust, plugins, MCP servers, profile models),
  sign-in and quota, the devkit plugin and whether each agent's copy is current, required skills, project settings
  that switch devkit off, sessions that ran with devkit, Docker, automation, the status-block canary and free disk.
  Its Claude Desktop and ChatGPT app checks prove that the apps' bundled agents use the same configuration and
  plugins as the terminal.
- **`dev agents`** reports installation, sign-in, plan, and quota for both agents, and `dev agents test` runs a real
  session and checks the hooks, the guard, the status block, the global instructions, and the devkit skills from
  recorded evidence.
- **Scheduled jobs** (`dev jobs`): `sync` (hourly), `sleep` (03:30), `gc` (04:00), `repair` (every 6 hours), `audit`
  (05:00), `doctor` (Mondays 09:00), and `lessons` (Fridays 16:00) as launchd agents `com.devkit.job.<name>`, generated
  from the `jobs` configuration key. `dev jobs show|run|logs|journal|set --every/--at/--weekly|enable|disable|install`.
  Each run has a timeout, a log, a journal entry, and an audit record, and a failure sends a notification. Custom jobs
  run any `dev` command. `dev doctor` checks that every enabled job is loaded and its last run passed. The jobs replace
  the single `com.devkit.sync` agent, which `dev install` removes.
- **Audit trail** (`dev audit`): every changing `dev` command, guard refusal, blocked settings change, missed status
  block, handoff write and push, settings repair, cleanup, and job run, with secrets redacted, in
  `~/.local/state/devkit/audit/YYYY-MM.jsonl`. The `audit` job publishes it daily to `audit/<user>@<host>/` in the
  handoff repository (`audit.publish`); `dev audit --team` reads every machine's. Filters: `--days`, `--action`,
  `--actor`, `--json`.
- **Docker on Colima** (vz, Rosetta, virtiofs) with Compose, Buildx, and Testcontainers settings; `dev docker`.
- **Git** identity and credentials from the GitHub CLI, shared defaults, and a global ignore file for agent and
  security-tool output.

#### Handoff, initiatives, and teams

- **Handoff repository** (default `<github-user>/handoff`, private, or shared by a team): `PROJECTS.md`, `TASKS.md`,
  `ACTIVITY.md`, `registry.json`, one note per project with frontmatter (status, priority, milestone, due date, tags,
  progress), generated auto and task blocks, plans, and an operations log.
- **Near-real-time sync:** every commit, push, merge, tag, rebase, PR, and release (by an agent or a `dev` command),
  every task, plan, lesson, and policy change, and every session start queue a background sync; events within about
  15 seconds share one run of about 3 seconds. The Stop hook writes the session line, renews locks, and publishes
  presence every 2 minutes, and the `sync` job runs hourly. A rejected push is rebased and retried once; a failure is
  audited and shown under Needs attention until a push succeeds.
- **The hierarchy** portfolio → initiatives → projects → tasks, plans, and sessions, used by every command, the session
  context, the dashboard, and the reports.
- **Initiatives** (`dev initiative new|add|rm|ls|show`): cross-project goals with workstreams, cross-project tasks,
  decisions, risks, and progress that counts the initiative's items and linked member tasks. `dev handoff-import`
  adopts an older handoff repository from a staging folder; five were adopted this way during the rollout.
- **Work items** in `items/<scope>/<ID>.md`: one versioned file per task, feature, bug, or spike, with a changelog line
  and a commit for every change. `dev task add` (with `--type`, `--now`, `--later`, `--priority`, `--due`, `--why`,
  `--done`, `--claim`), `start` or `claim`, `release`, `done`, `block --why`, `unblock`, `cancel`, `move`, `edit`,
  `link --to <initiative>`, `show`, `history`, and `delegate --to`. `-p` and `-i` work before or after the verb.
- **Locks across machines:** `dev task start` takes an 8-hour lock and pushes it at once; other sessions are told
  not to work on the task; locks renew while a session works and expire when nobody renews them. Changes of scope need
  `--why`.
- **Delegation:** `dev task delegate` assigns a task to a teammate and writes the message to send, copied to the
  clipboard.
- **Versioned plans** (`dev plan new --kind plan|spec|spike|bugfix|adr`, `revise --why`, `approve`, `supersede`,
  `ls`): every change records a version, who, and why. The Stop hook asks for a revision when a plan changed without
  one.
- **Session logs** in `sessions/<scope>/YYYY-MM.md` (append-only), **presence** in `presence/<user>@<host>.json`, and
  merge rules in `.gitattributes`, so machines and developers do not conflict. `dev team` shows who is working on
  what. `dev handoff --log` adds a line.
- **`dev overview`**, **`dev standup`** and **`dev note set`** for the portfolio, the last days, and project metadata.

#### Progress, time, and communication

- **`dev progress start|step|block|done`**: live progress of agent work, with named steps, an ETA from the real pace,
  estimates calibrated from this project's history, updates published to the team, and desktop notifications when a
  run blocks or finishes or a long turn ends.
- **`dev time`**: turn lengths, estimate accuracy, subagent durations, and the slowest commands.
- **`dev report`**: a Markdown status report for the team or management, with health rules you can explain.
- **Hooks that keep you informed.** UserPromptSubmit answers "how is it going?" from real records, CI/CD pipelines
  included, and ignores background notifications. PostToolUse times slow commands and reminds a quiet agent after
  about 8 minutes. SubagentStart and SubagentStop track subagents.
- **The `communicating` skill** with templates for kickoff, progress, status, decisions, explanations, final and
  failure reports, pull request descriptions, squash-merge messages, review comments, delegation, and the structured
  formats of every record. Agents write in plain American English.

#### Status block

- Every final message starts with the **`[Firefly> DevKit]` status block**: a bold header with project, branch and
  task, then Done, Check (✓, ⚠, ✗ or –, with the evidence), Progress while a run is active, and Next.
- The rule applies to **every turn, without exception**: short answers, questions, status updates, replies during
  background work, and any language. The first turn stays a clean test of the instructions; a session that missed the
  block once gets a reminder in every later prompt, and replies corrected after a reminder are counted apart
  ("N corrected after a reminder" in `dev doctor`, `dev overview`, and `PROJECTS.md`).
- **Nothing comes before the block:** no verdict, correction, greeting, or comment on the last command before it;
  "lead with the point" applies below it, and a reply that waits for background work starts with it too. Every
  `communicating` template that can end a turn (kickoff, progress, status answer, decision, failure) starts with the
  block. `dev progress step` prints the block itself (Done, Check, Progress, Next, then the checklist), and its new
  `--check "<evidence>"` fills Check. The Stop hook says whether the block is missing or which line came before it.
- **Results per surface:** every session and status-block result records where it ran (terminal, Claude Desktop,
  the ChatGPT app, an IDE), and `dev doctor`, `dev overview`, and `PROJECTS.md` break the rate down by surface.
- devkit checks the marker after every turn, **scores the format**, stores a compact line as the project's latest
  status, and shows it in `dev status` (as a block with brand pills), `dev overview`, the dashboard, the session log
  and the next session's context. `ui.powerline` adds Nerd Font arrows between the pills. Older one-line forms and the
  legacy `DevKit ·` marker still count as present. `dev canary` reports the history.

#### Agents, models, and tokens

- **Global instructions** for both agents from one source, `agents/AGENTS.md`: the status block, method, plain
  writing, progress and time, token use, files and disk, worktrees, branches, Conventional Commits, CalVer, local-first
  delivery, backend practice, documentation, required skills, the team protocol, and handoff.
- **The devkit plugin** for Claude Code and Codex (`firefly-devkit` marketplace), with the devkit icon, display
  name, and the Firefly amber as Codex's brand color, and the skills `using-devkit`, `handoff`, `communicating`,
  `working-efficiently`, `workspace`, `delivery`, `release-policy`, `learning-loop`, `backend`, `devsecops`, `docs`, and
  `diagrams`; the slash commands `/devkit:status`, `overview`, `task`, `handoff`, `standup`, `ship`, `ci`, `dashboard`
  and `doctor`; and the read-only **`scout`** agent. `dev plugin validate` checks it with both agents' parsers.
- **Hooks** for both agents (SessionStart, UserPromptSubmit, PreToolUse (the guard), PostToolUse, SubagentStart,
  SubagentStop, Stop, and SessionEnd; Claude Code also ConfigChange and InstructionsLoaded.
- **Models and tokens:** the `working-efficiently` skill (routing, briefing templates, escalation, context hygiene),
  the `scout` role for Codex in `~/.codex/agents/scout.toml`, the Codex profiles `fast`, `deep`, and `review` in
  `~/.codex/<name>.config.toml`, at most 4 Codex subagent threads, and Claude's fast mode chosen per session. devkit
  never sets the main model. Codex's `review_model` makes `/review` use the strongest model, and
  `project_doc_max_bytes` = 131072 lets it read a project's AGENTS.md whole instead of stopping at 32 KiB. New keys:
  `claude.subagent_model`, `claude.subagent_effort`, `claude.fast_mode_per_session`, `codex.max_subagent_threads`,
  `codex.profiles`, `codex.subagent_model` and `codex.subagent_effort`.
- **Required skills:** `config/skill-rules.json` maps kinds of work to skills (UI, Angular, React, Spring, Java tests,
  APIs, databases, workflows, security, docs, diagrams, delivery, communication, lessons). The prompt hook names the skills a
  request needs, Claude subagent briefs get them appended, and the Stop hook sends a turn back once when it changed
  matching files without them. Configure with `skills.enforce` and `skills.rules`.
- **devkit always on:** a session that missed SessionStart gets the context at its first prompt; the guard refuses
  settings that turn off hooks or the plugin; `dev doctor` reports project overrides, sessions that ran with devkit,
  and stale plugin copies, which `dev install` reinstalls.
- **Configuration policy:** `dev config diff` previews drift, `repair` restores it (at session start and every 6 hours),
  `lock` installs a Claude Code managed-settings file, and ConfigChange refuses edits to managed keys.

#### Skills, plugins, and security

- **Curated skills** pinned to upstream commits for architecture (ADRs, system design, C4, domain modeling),
  Angular, React, Spring (including security and observability), testing, documentation, incident response,
  GitHub Actions hardening, Dependabot, threat modeling, Trail of Bits security skills, and OpenTelemetry. The Trail of
  Bits skills (CC-BY-SA-4.0) are credited in `NOTICE`.
- **Optional skill groups** in `skills.groups`: `security-deep`, `postgres`, and `observability` (on by default), and
  `codeql`, `iac`, `load-testing`, `contract-testing`, and `skill-review`.
- **Plugins.** Superpowers, context7, frontend-design, commit-commands, security-guidance (tuned),
  claude-md-management, claude-security, and the TypeScript, Python, Java, and PHP language servers for Claude Code;
  Superpowers, impeccable, and codex-security for Codex. Chrome DevTools and Angular CLI MCP servers, pinned.
- **Both design skills for both agents:** devkit links `impeccable` for Claude Code and `frontend-design` for Codex,
  pinned, where the agent's plugin does not already bring them, and the UI rule requires both.
- **`dev sec scan`:** secrets (gitleaks), dependencies (osv-scanner), code (Semgrep), filesystem and IaC (Trivy) and
  GitHub Actions workflows (zizmor), with scanner telemetry, and update checks off.

#### Delivery, docs, and diagrams

- **Delivery:** `dev ci` shows what CI runs so you run it locally and push once; `dev calver` prints the current and
  next CalVer version.
- **Delivery policy:** `dev delivery` detects how a project versions, tests, releases, and ships (tags, version files,
  release tools, changelog, CI, the test gate, branch model, what it publishes and deploys), recommends a policy that
  keeps what works (SemVer for libraries, CalVer for apps and services), and lists the gaps. `dev delivery apply
  --why` records it in `policies/<scope>.json` in the handoff repository, versioned with its history, and `--tasks`
  opens a work item per gap. Sessions see the policy, the guard refuses tags that break it, and release work requires
  the new `release-policy` skill. Initiatives can record a release train. devkit's own policy is recorded.
- **CI/CD status on any host:** `dev ci status [--watch]` shows the runs of the branch on GitHub Actions, GitLab CI,
  Bitbucket Pipelines, and Azure Pipelines, with jobs, the current step, elapsed time, and an ETA from recent
  successful runs. `dev status` shows the latest run, and status answers include the pipelines. GitHub is verified
  live; the other three are verified against recorded API responses.
- **devkit's own CI:** a pinned macOS workflow that runs the suite on Python 3.9 and 3.13, the devkit pull request
  template, and grouped weekly Dependabot updates for the actions.
- **Templates** for plans, specs, spikes, bug fixes and ADRs (`templates/plans/`), typed tasks (`templates/tasks/`),
  pull requests (`templates/github/pull_request.md`, and `pull_request.es.md` in Spanish) and the handoff repository README (`templates/handoff/`).
- **Docs tooling:** `dev docs check` (markdownlint, lychee links and anchors, Vale with Google and write-good, SVG
  checks), the `docs` skill, and the `documentation-writer` and `doc-coauthoring` skills.
- **Diagrams:** `dev diagram render|check|new` renders validated SVG diagrams (flow, architecture, sequence, state,
  C4, ER, timeline) in the Firefly palette, dark and light, and the `diagrams` skill.

#### Git and GitHub

- **`dev branch new|rename|ls|clean`:** branches from an up-to-date `origin/<default>`, with names normalized to the
  standard (`feat/PROJ-142-invoice-export`), harness names renamed before the first push, and merged or gone branches
  cleaned after a dry run.
- **`dev commit`** makes Conventional Commits (known type, imperative mood, no final period, at most 72
  characters), never on the default branch, AI attribution removed, the secret scan applied; `--wip` and `--unwip`
  replace the shared stash.
- **`dev rebase`** on `origin/<default>` with a conflict report.
- **`dev ci --run`** runs the workflows' `run:` steps locally and records evidence for the exact files tested;
  `--mark-passed --note` records a suite you ran yourself; `--evidence` shows it.
- **`dev push`:** one push after the checks: branch name, fresh fetch, no WIP commits, not behind on a first push,
  local CI evidence, the push budget, and `--force-with-lease` that never drops someone else's commits.
- **`dev pr create|view|ls|checks|merge|ready|comment|review|diff`** opens PRs from the repository's template, or
  devkit's in English or Spanish (`templates/github/pull_request.es.md`, chosen by `language`), pre-filled with the
  commits, the local CI result, and the links; empty required sections are refused unless `--draft`. `dev pr checks --watch` waits
  and shows the failing jobs' trimmed logs. `dev pr merge` merges only when the checks and reviews allow it, with the
  repository's method and a clean squash message, then tidies the branches.
- **`dev run ls|view --failed|rerun --flaky`**, **`dev release`** (tag, push, and GitHub release by the delivery policy,
  with the CHANGELOG section as notes), and **`dev issue ls|view|create`**.
- **`dev git …` and `dev gh …`** run any git or gh command with the same checks.
- New configuration keys `commits.types` and `gitx.github_hosts`.

#### Lessons

- **`dev lesson add|ls|show|recur|prevent|retire|restore|scan|review`:** blameless lessons (what happened, root cause,
  fix, prevention, detection, evidence) per project, initiative, or globally, in `lessons/` in the handoff repository,
  versioned with their history. Sessions start with the known pitfalls that apply; the Stop hook asks once for a
  lesson after a fix or revert commit; `dev lesson scan` finds reverts, fixes, repeated guard refusals, missed status
  blocks, bouncing tasks, and developer corrections.
- **Use and decay:** every lesson's use is counted per machine and summed for the team; strength halves every 45 days
  without use. **`dev sleep`** (nightly) merges near-duplicates, archives retired, long-prevented, and faded lessons
  into `lessons/archive/` (`dev lesson restore` brings them back), keeps at most 25 active lessons per scope, proposes
  global lessons, and trims devkit's own old records, caches, and logs.
- **The `learning-loop` skill**, required for work about lessons, prefers prevention by test, lint, guard, skill, or
  CI over prose.

#### Files

- **Token-light file commands:** `dev files` (a compact tree), `dev find` (names), `dev grep` (contents, structural
  search with ast-grep), and `dev diff` (changed files first, short hunks, generated files folded) keep their output
  compact and capped. Homebrew adds fd, ast-grep, and difftastic.
- **Safe file operations:** `dev rm` moves paths to the Trash with a manifest and `dev rm --undo`, stages tracked
  deletions like `git rm`, and refuses open files, unsaved or untracked work, repositories, worktrees, and workspace
  and system folders unless the exact flag is given. `dev mv` and `dev cp` never overwrite silently, use `git mv` for
  tracked files, and refuse layout breaks, backup names, and heavy copies.

#### Dashboard and disk

- **Dashboard:** a self-contained, branded HTML command center with a clear information hierarchy: a summary Home
  (what needs you, team now, initiatives, since yesterday) and Initiatives, Projects, Team, Tasks, Activity, Explorer,
  and Health views, initiative and project pages, a task drawer with lock state, commands and
  history, live runs and time, search, a command palette and shareable URLs; regenerated on every sync. Visual first:
  rings, status bars, member waffles, sparklines, a calendar heatmap, CI strips, session bars, and lock countdowns,
  with details in accessible quick-look dialogs that have their own links.
- **Disk:** safe cleanup of merged worktrees, idle build output, stale temp files and the Docker build cache, checked
  again before every removal; copy-on-write `node_modules` for new worktrees; `dev gc` and `dev du`.
- **Identity:** the devkit wordmark outlined from Manrope with the amber spark as the dot of its letter, banners in
  the brand's light-trail system, the plugin icon, and the wordmark drawn by hand for the terminal.

- **Development work only.** devkit's process (the status block, required skills, reminders) applies to
  development turns; a recipe or consulting work with the Firefly consulting kit gets a normal answer. Each turn is
  judged by its request and by what it did (`lib/devkit/scope.py`); `DEVKIT_SCOPE=all` checks every turn.
- **Claude's quota.** A devkit status line (installed only when you have none) shows project, branch, model, context
  and the 5-hour and 7-day usage, and records the windows, so `dev doctor`, the self-test and the dashboard report
  Claude's quota like Codex's.
- **Status block first, always.** The communicating templates and `dev progress step` (new `--check`) now produce the
  block itself; the Stop hook says whether the block is missing or what came before it.
- `dev canary` shows results per surface and corrected misses; `dev help` lists `dev run`, `dev issue`, `dev git` and
  `dev gh`; `dev pr create --dry-run` prints the body alone on stdout; the wizard's sync interval sets the sync job.
- The guard protects the handoff repository's `audit/` folder.

- **Claude Desktop extension.** devkit ships an MCPB extension (`dev install --only desktop`, `dev desktop
  pack|status`) with its name and icon: 14 tools (overview, project status, tasks, standup, report, CI, lessons,
  delivery policy, team, doctor, audit, jobs, task add and update) for Desktop chat and Cowork, served by `dev mcp`
  from your checkout. `dev doctor` checks it next to the Code tab and the ChatGPT app's Codex.

### Security

- **The guard**, a pre-tool hook for both agents, enforces the workspace layout, managed worktrees, and canonical
  clones; blocks backup copies, writes outside the workspace and destructive commands (`rm -r` of protected paths,
  `git clean -x`, `git reset --hard` with changes); blocks plain force pushes, a third push of the same branch within
  an hour, blind CI re-runs, non-conforming branch names and AI attribution; keeps the handoff repository's work
  items, logs, presence, policies, and generated files out of direct edits; keeps each project session to its own
  records; steers `rm`, `unlink`, `find -delete`, `xargs rm`, `git clean -f`, and overwriting `cp` and `mv` to the
  recoverable `dev` commands; refuses tags that break the delivery policy; refuses a bare `git stash` (use
  `dev commit --wip`), raw `gh pr merge` and `gh release create` (use `dev pr merge` and `dev release`, or prefix
  `DEVKIT_RAW_GH=1`), and always `gh pr merge --admin`; refuses a commit, push, or tag chained after a pipe without
  `pipefail`; recognizes Copilot, Gemini, and Cursor co-author lines as attribution; and keeps `lessons/` out of
  direct edits.
  `dev guard check` simulates any decision.
- **No secrets in commits:** the guard runs gitleaks on what a `git commit` is about to record, including a `git add`
  earlier in the same command, and refuses the commit when it finds a secret.
