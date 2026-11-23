# Security policy

devkit runs inside developers' agent sessions. Its hooks see every tool call, and its installer writes the agents'
settings, hooks, and instructions. A vulnerability in devkit can reach every machine that installs it, so report it
privately, as described here.

## devkit is not a security boundary

devkit decides nothing about what an agent may run. The agent's own permission settings decide:

| Agent | Where it is set | What devkit's setup chooses by default |
| --- | --- | --- |
| Claude Code | `permissions.defaultMode` in `~/.claude/settings.json` (`claude.default_mode` in devkit's configuration) | `bypassPermissions`: no prompts |
| Codex | `approval_policy` and `sandbox_mode` in `~/.codex/config.toml` (`codex.mode` in devkit's configuration) | `never` and `danger-full-access`: no prompts, full access |

With these defaults, an agent runs anything you could run, with your credentials: the GitHub account you signed in
to with `gh`, your SSH keys, and the tokens on your Mac. What it runs is at your risk. devkit's hooks record each
call, route crew calls to their lanes, and add advice to a call, such as a write outside a crew member's scope; they
never refuse a call and never hold one for your approval. On Codex, a crew member's changed command carries an
`allow` only when your Codex approval policy is `never`, or when it is one plain `dev progress`, `dev crew`,
`dev watch`, or `dev session` call with nothing the shell would expand; anything else is left for Codex to ask
you about. devkit's own commands still check their own work (`dev commit` refuses staged secrets and AI attribution,
`dev push` refuses a plain force push and a push without local CI evidence, `dev release` follows the delivery
policy), but a raw `git` or `gh` command runs none of those checks. Those checks catch mistakes; they are not a
defense against an agent set on getting around them. For example, a staged `.gitattributes` or git settings in
the environment can hide a change from the secret scan.

To limit what agents can do, use the controls that do enforce limits:

- **The agent's permission mode.** Choose `acceptEdits` or `default` for Claude Code, or `workspace` for Codex
  (sandboxed to the project, asks when it needs more), in `dev setup` or with `dev config set`.
- **Your Git host.** Protect the default branch: require pull requests, reviews, and passing checks, and block force
  pushes. These hold whatever runs on your Mac.
- **Your credentials.** Give tokens the narrowest scope that works, and keep production credentials off the machine
  where agents run.
- **The folders you trust.** Once you trust a repository's folder in the agent, its own agent settings
  (`.claude/settings.json`, `.codex/`) can set hooks, MCP servers, and environment variables, devkit's
  `DEVKIT_CONFIG` and `DEV_ROOT` included, and so change where devkit reads its configuration. Trust only
  repositories whose code you would run.

### What the call token protects

devkit names who runs each `dev` command by a call token: the pre-tool hook puts `DEVKIT_CALL` in front of the call,
and the token holds only for the process whose id the call's prefix pins first (or the `dev` process of a single
plain call) and the processes below it, and only while the call is open (see [The crew › Who is calling](docs/crew.md#who-is-calling)).

Where the agent checks your permission rules on each command (Claude Code outside `bypassPermissions`, Codex when it
asks), devkit changes nothing in a plain `dev` call, so an allow rule such as `Bash(dev progress:*)` matches the text
the agent wrote and the call runs without a prompt. devkit then names its caller by facts the kernel keeps, never by
the text alone: the call's own shell sits right below the agent, started within seconds of the call, its argument
list holds exactly that command, and the call's first `dev` command pins that shell. A second process that claims
the same call marks it contested, and devkit refuses it for every claimant and records it. devkit makes no
permission decision of its own for such a call; your rules, as the agent reads them, decide.

Both protect against mistakes and make misuse visible:

- **Mix-ups between callers.** A crew member's command is never taken as the tech lead's, or as another member's:
  not when their command texts look alike, not while the tech lead's call and a member's call are open at the same
  time, and not when devkit cannot tell who calls, since it then refuses the change.
- **Commands outside their call.** A background or detached job, a nested agent such as `codex exec` or `claude -p`,
  and a token copied from a log or a transcript name nobody once their call has closed, or outside the call's own
  shell, so the state changes they try are refused.
- **Project code that devkit runs.** `dev ci --run`, `dev test`, and `dev test prove` start the project's workflow
  steps and tests without the token or the lane marks, and so does every git command devkit runs, with the git hooks
  that `dev commit` and `dev push` start. A `dev` command in a step, a test, or a hook that a crew member wrote names
  nobody there, so devkit refuses its state change.
- **A record of each attempt.** Each failed check and each state change without a token writes a caller identity
  record to the audit trail ([The audit trail › Caller identity records](docs/audit.md#caller-identity-records)).
  devkit keeps only a hash of the token, and removes its value from every command text it records.

What it does not protect:

- **devkit's own state.** The call ledger, the session records, and the audit log are files your user owns. A crew
  member that edits them can still act as another caller. The next tech-lead call shows the damage, but devkit does
  not stop it, and such an attack is out of scope for reports (see [Scope](#scope)).
- **Crew code that the tech lead runs.** A crew member's code that the tech lead starts with an ordinary command,
  such as `npm test`, `make`, or a git hook that a plain `git commit` starts, runs inside the tech lead's call. A
  `dev` command in that code acts as the tech lead, with your full permissions. When the call ends, devkit records
  each state change that the call's own text does not run (`caller.token-unlisted-use`), so the change shows in the
  audit trail, but devkit does not stop it.
- **A race won against an unchanged call.** A crew member that replaces its own call's shell (`exec`) with one whose
  arguments run the tech lead's open `dev` call, started within that call's first seconds and before the tech
  lead's own `dev` command pins it, can make that one change as the tech lead. The tech lead's command is then
  refused as contested, and the audit trail records `calls.pin-contested`, so the change does not go unnoticed.
- **What an agent may run.** The token names who runs a `dev` command and limits nothing else. Against an agent set
  on misuse, the controls that hold are the agent's permission mode and your Git host's branch protection.
- **The token while its call runs.** Any process of your user can read it from the process list then: holding only
  for the call's own shell is what makes a copy useless elsewhere. Transcripts and Codex rollouts, which the agents
  keep, can hold the token after its call closed, when it names nobody.

## Supported versions

| Version | Supported |
| --- | --- |
| The latest release (see [Releases](https://github.com/fireflysoftwaresolutions/devkit/releases)) | Yes |
| Older releases | No: update with `dev update` |

Fixes ship as a new patch version (CalVer `YY.MM.PATCH`), with notes in the [CHANGELOG](CHANGELOG.md).

## Report a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a
vulnerability**, or go straight to
[the new advisory form](https://github.com/fireflysoftwaresolutions/devkit/security/advisories/new). Only you and the
maintainers see the report.

Do not report a vulnerability in a public issue, pull request, or discussion.

Include what helps the maintainers reproduce it:

- the devkit version (`dev version`) and your macOS version;
- the agent and the surface, such as Claude Code in the terminal or in Claude Desktop's Code tab, Codex in the
  terminal or the ChatGPT app, or the Claude Desktop extension;
- the steps, the impact you see, and a proof of concept if you have one.

Redact secrets before you send logs or transcripts: devkit's logs can contain command lines and file paths.

### Reports devkit files for you

devkit can file a security report itself, through its [bug registry](docs/bug-registry.md). A report you file with
`dev bug report --security`, and any failure devkit treats as security-sensitive, never goes to a public issue.
Security-sensitive means a crash in the crew's file-scope code, the lesson rules, or the code that builds and checks
reports; or free text or an error message about bypassing, allowing, injecting, tokens, secrets, credentials,
passwords, leaks, exfiltration, traversal, escalation, privileges, the sandbox, grants, or exploits. Such a report
goes this way:

1. devkit holds it on your Mac. `dev bug pending` prints exactly what it would send: devkit facts only, never paths,
   names, code, or output from your projects.
2. When you, or an agent, run `dev bug send <id>`, devkit sends it to this repository's private vulnerability
   reporting, under your GitHub account.
3. If that fails, or private reporting is off, the report stays held and devkit tells you; it never falls back to a
   public issue.

Then add the steps and the impact to the advisory, as in the list of what to include.

## What happens next

1. The maintainers confirm in the advisory that they received the report, and keep you updated there.
2. They reproduce the issue and fix it in the advisory's private fork.
3. They release the fix as a patch version, publish the advisory, and request a CVE when the issue qualifies.
4. The advisory credits you, unless you prefer not to be named.

## Scope

In scope:

- the `dev` CLI and everything under `lib/` and `bin/`;
- the hooks: a way to make a hook leak data, corrupt devkit's records, or run code devkit did not mean to run is a
  vulnerability;
- the crew's caller identity: a way for one agent to act as the tech lead or another crew member through devkit's
  commands without editing devkit's state files, or one that leaves a record that looks like a normal verified call;
- the installer (`install.sh`, `dev setup`, `dev install`) and the settings, hooks, and instructions it writes;
- the MCP server (`dev mcp`) and the Claude Desktop extension;
- the dashboard, a generated HTML file: data it shows without escaping is in scope;
- the CI/CD templates that `dev ci init` writes;
- the bug registry: its workflow (`.github/workflows/bug-registry.yml`) and what devkit sends to it. A way to make
  devkit send project data, or to plant a detour that reaches other people's agents, is a vulnerability.

Out of scope:

- third-party skills, plugins, and tools that devkit installs: report those upstream ([NOTICE](NOTICE) lists their
  sources);
- issues in the agents themselves (Claude Code, Codex, Claude Desktop, the ChatGPT app): report those to their
  vendors;
- attacks that need an already compromised machine or account;
- a crew member, or another process of your user, that edits devkit's state files (the call ledger, the session
  records, the audit log) to act as another caller;
- what an agent runs with the permissions you gave it: devkit does not limit agents (see
  [devkit is not a security boundary](#devkit-is-not-a-security-boundary)).

## How this repository is protected

- Secret scanning and push protection are on.
- CI runs a security job on every pull request: gitleaks, Semgrep, osv-scanner, Trivy, and zizmor.
- GitHub Actions are pinned by commit SHA and container images by digest, with least-privilege tokens.
- Dependabot keeps the pinned actions current.
- devkit bundles no third-party code.

See also [Concepts › Agents act with your permissions](docs/concepts.md#agents-act-with-your-permissions),
[Agents](docs/agents.md), and the [contributing guide](CONTRIBUTING.md).
