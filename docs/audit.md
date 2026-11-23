# The audit trail

devkit records what happens: every `dev` command that changes something, the crew's lifecycle and decisions, the
writes to the handoff repository, and the scheduled jobs. An automatic decision is recorded as automatic, never as
your answer. Records written by devkit 26.11.18 and earlier can also hold the command guard's decisions
(`guard.*`), grant records, and decision evidence (`crew.evidence`); devkit still reads, verifies, and exports them,
and writes no new ones.

This page is the reference for devkit's audit trail: how long devkit keeps its records, what personal data they hold
and how the people they describe can use their rights under the GDPR (the EU General Data Protection Regulation),
what each record contains, how much is recorded, and how to verify, query, and export the records. For the daily job
that publishes them and a first look at `dev audit`, see
[Scheduled jobs and the audit trail](jobs-and-audit.md#the-audit-trail).

## Retention

Each machine appends its records to `~/.local/state/devkit/audit/YYYY-MM.jsonl` (mode 0600, in a 0700 folder). The
daily `audit` job publishes the last three months to `audit/<user>@<machine id>/` in the handoff repository, one file
per day (see [The handoff copy](#the-handoff-copy)), then prunes. Prune also covers the folders this Mac wrote under a
hostname it had before (`audit/<user>@<hostname>/`), for the month files whose last record this Mac wrote.

| Key | Default | What it keeps |
| --- | --- | --- |
| `audit.retention.local_months` | `13` | Month files on this machine: a full year plus one month, so a yearly review or a year-over-year incident still has its records |
| `audit.retention.team_months` | `13` | This machine's months in the handoff copy: each month's folder of day files, and a one-file copy where an earlier devkit published one. The team copy is read by more people, so by default it keeps no longer than the machine |
| `audit.retention.floor_months` | `3` | The least any retention may be. Three months covers the time it takes to notice an incident and investigate it, so 3 is also the hard minimum: `dev config validate` refuses 1 or 2 |
| `audit.hold` | `false` | A legal hold: `true` stops all pruning on this machine and in the handoff copy |

A `local_months` or `team_months` under the floor is raised to it. To record less, set `audit.level` to `security`
rather than shorten the floor. The team policy, `policy/audit.json` in the handoff repository, can raise any of these
values for every machine, never lower them. A team that must keep records longer, for example for a client contract
or an external audit, raises `team_months` there and says why in the commit.

Every prune is itself recorded as `audit.prune`, with the months and the number of records it removed.
`dev audit config` shows the effective values, where the records are, their size, and what the next prune removes.

### What git keeps

Pruning removes month files from the handoff repository's **working copy only**. Git history keeps every published
month until that history is rewritten, so today `team_months` does not delete anything from git. `dev audit config`
says so on its `git history` line. A procedure to purge pruned months, and the text of
[records written before 26.11.3](#records-written-before-26113), from the history is planned. It must run before
anyone else gets access to the handoff repository, or before the first published month (2026-11) passes
`team_months` (at 13 months, 2027-12), whichever comes first.

Each machine prunes only its own folder. The folder of a retired machine stays until that cleanup also covers other
machines' folders, which is planned and not built yet.

## The handoff copy

The daily `audit` job publishes each machine's records to the handoff repository one UTC day at a time. GitHub
refuses a pushed file over 100 MB, and from then on every push of the handoff repository, so no published file is
ever over 45 MiB, and no file is rewritten every day.

```text
audit/ana@m-s9jn9xnz/
  2026-11.jsonl                  a one-file copy that devkit 26.11.19 or earlier published: it stays as it is
  2026-11/
    2026-11-16.jsonl             the records after that copy's last byte, to the end of the 16th (UTC)
    2026-11-17.jsonl             one file per UTC day
    2026-11-18.held.json         a day over 45 MiB: its size, count, and SHA-256, not its records
    2026-11-19.jsonl
```

- **One file per closed day.** A day is closed once a record of a later UTC day follows it in the local month file,
  or once that month has ended on the machine. The job publishes each closed day once and never rewrites it. The
  open day waits for the next run, so a record waits up to about 29 hours to be anchored: the rest of its UTC day,
  plus the time to the next 05:00 run.
- **Days are UTC days,** taken from each record's time and kept within the local month file the record was written
  to. A record whose clock went back stays in the day it follows, so the day files joined in order are the local
  month file, byte for byte, and one chain.
- **A one-file copy stays.** devkit 26.11.19 and earlier published `YYYY-MM.jsonl` whole, every day. Such a copy is
  never rewritten nor removed; the day files go on from its last byte.
- **A day over 45 MiB is held.** Its records stay in this machine's local trail, where `dev audit verify` checks them.
  `YYYY-MM-DD.held.json` keeps the day's size, record count, and SHA-256, and where the chain stands at its end, so
  the days after it still publish and verify on every machine. The job records `audit.publish` as blocked with
  `warning` severity, and `dev audit stats` and `dev doctor` name the day, with the command that shows what filled
  it: `dev audit stats --since <day> --until <day>`. A day that size is far beyond a heavy day (about 10 MiB), so
  look for an action that repeats.

`dev audit --team`, `dev audit export --team`, `dev audit verify`, and the embargo check read both layouts and count
each record once. A machine still on devkit 26.11.19 or earlier reads the day files too, but its `dev audit verify`
checks each day on its own and reports the first record of each day as not linked: update that machine.

After an update, a month over 45 MiB shows in `dev audit stats` and `dev doctor` as _not published yet_ until the next
`audit` job publishes it by day. To publish it at once, run:

```bash
dev jobs run audit
```

## Personal data and the GDPR

The audit records show what a person did, on which machine, and when, so they are personal data. Developers in the
EU are covered by the GDPR; in Spain also by the LOPDGDD (Spain's data protection law). This section is the
information the people recorded need (GDPR article 13).

| Topic | What applies |
| --- | --- |
| Controller | The organization whose developers use devkit; for Firefly Software Solutions' teams, Firefly Software Solutions |
| Purpose | Security and accountability for what devkit and its agents change, decide, or refuse |
| Legal basis | Legitimate interest (article 6(1)(f)) |
| Never used for | Measuring a person's performance or working time. For employees in Spain, the Workers' Statute (article 20.3) and LOPDGDD article 87 also require telling them about the records |
| Recipients | This machine; everyone with access to the handoff repository; GitHub, which hosts that repository; and the model provider when an agent reads `dev audit` |
| Retention | As in [Retention](#retention), including what git keeps |

### What a record holds, and what it never holds

devkit records only what the purpose needs (data minimization, article 5(1)(c)):

- **Who acted:** the GitHub login, this Mac's machine id (never its hostname), the agent (`claude`, `codex`,
  `job`, or `human`), the session id, and, when a crew member acts, its member id, lane, and launch id.
- **What happened:** the action, its target and details, the outcome, the project, branch, run and step, the reason
  given for an override (`why`), and evidence such as the command, the commit, or the exit code.

devkit keeps these out of every record it writes:

- **Prompt text and message text.** A crew message is recorded by its id, sender, recipient, and length, and
  `dev crew ask` and `dev crew answer` by their naming arguments (`--to`, the message id) and the length of the text.
  Crew messages often quote the developer's requests, so their text stays in the session's own records. A message
  between agent sessions on this Mac is recorded by its kind, its recipients' sessions, and, for a claim, the
  resource; its text, a claim's reason, and an escalation's question never are.
- **Free text in a command line.** Since 26.11.5, a record that keeps a command line keeps only the words devkit
  knows to be safe, and replaces every other word with its length, such as `[text not recorded: 22 characters]`, or
  `N+` when the command was cut inside it. Words next to each other in one argument share one marker.
  `dev crew ask` and `dev crew answer` keep their marker `[message text not recorded: N characters]`, and
  `dev crew decide` keeps `[free text: N characters]`. These words stay:
  - The program, when it is `dev`, `git`, or `gh`, and its subcommands. The program is the word at command
    position: devkit skips reserved words such as `if`, `do`, `{`, and `!`, variable assignments, and wrappers such
    as `env`, `sudo`, `timeout`, `nice`, `xargs`, and `uv run` with their options and values. It also finds the
    program as a path (`~/bin/dev`) or `python3 -m devkit`, and inside the text of `bash -c`, `sh -c`, `zsh -c`,
    and `eval`. Anywhere else, such as in `kubectl --context dev` or `echo dev crew show`, `dev` is a word like
    any other.
  - The name of another program at command position when devkit's list of known programs has it, such as `kubectl`
    or `sleep`. The list is fixed and the same on every machine, with no lookup in your `PATH`: devkit's own
    programs, the programs devkit's read-only classifier knows, package runners, and wrappers, and
    common developer tools, such as `python3`, `npm`, `make`, `docker`, and `curl` (`PROGRAMS` in
    `lib/devkit/audit.py`). Any other program becomes its length, since a word there can be a client's name, a file,
    or a password. A name written with quotes or an escape, such as `"cat"`, also becomes its length.
  - The last part of a path, when the path runs a program on that list. A program or a wrapper run by its path,
    `dev`, `git`, and `gh` included, keeps only that last part: `/opt/homebrew/bin/python3` is recorded as
    `[text not recorded: 17 characters]/python3`.
  - Option names, such as `--why` and `-m`.
  - The value of an option that names something, such as `--to`, `--type`, `--scope`, `--project`, or `--step`
    in `dev`, and `-C`, `-R`, `--repo`, `--base`, and `--head` in `git` and `gh`. In `dev visual`, the record and
    map ids and the capture settings stay, such as `--audit`, `--node`, `--viewport`, `--theme`, and the path of
    `--storage-state`.
  - The arguments of `dev` that name something, such as a branch, a path, a project, or a crew member. An argument
    that selects a task, a lesson, a step, or something of the [bug registry](bug-registry.md) (a bug, a detour, an
    outbox item) stays only as an id or a number, since it can be part of a title; a fingerprint or a detour id
    becomes its length.
    `dev crew raci` also classifies a task given in words, so its argument stays only as a kind of work or a crew
    member of devkit's roster, such as `backend` or `tank`, and its `--set` value only as `R`, `A`, `C`, and `I`
    with crew members.
  - The target of a `dev session` command, only in one of its shapes: a session or a group of them (`@4d1cffc7`,
    `project`, `project:acme/api`, `initiative:payments`, `repo`, `all`), a message or escalation id (`S-…`,
    `E-…`), a claim's resource (`release:acme/api`, `push:acme/api@main`, `custom:db-migration`), or a transcript
    (`….jsonl`). A message typed where the target goes becomes its length, and the text of `tell`, `ask`, `answer`,
    and `escalate` always does.
  - The value of `status` and `priority` in `dev note set`, which is one of a fixed set, such as `status=blocked`.
    The value of any other key becomes its length.
  - Ids (`T-…`, `L-…`, `P-…`, `D<n>`, `M<n>`, `R<n>`) and numbers of up to six digits. A longer number, such as a
    phone number, becomes its length.
  - Shell operators, reserved words, and redirections. A redirection's file stays only when it names no file of
    yours: `/dev/null`, `/dev/stdout`, `/dev/stderr`, `/dev/tty`, a number, or `-`.
  - The name of a variable that the command sets (`MSG=`), and the value of a `DEVKIT_*` variable when it is one
    short word, such as `DEVKIT_SCOPE=all`.

  Everything else becomes its length: free text, such as a `dev crew ask` message, the text of `--why`, `--check`,
  `-m`, or `--cause`, a lesson rule's `--match`, a detour's `--hint`, `--dev`, and `--text`, a heredoc's body, a here-string, and a comment; and a word that only might be free text, such as a
  filename, a branch passed to `git push`, or a value devkit does not know. In `dev visual`, a page's URL or route
  prefix, a selector, a recipe step (`--do`), CSS, and the page's own words (`--state`, `--label`, `--avoid`) become
  their length too, since they can carry a token or what the page shows. A line whose program is none of these
  keeps its operators, the program's name when the list has it, and each other word's length: `rm -rf src` is
  recorded as `rm [text not recorded: 3 characters] [text not recorded: 3 characters]`, and `globex-sync --all` as
  `[text not recorded: 11 characters] [text not recorded: 5 characters]`.

  These fields hold a command line:

  | Record | Field | What it holds |
  | --- | --- | --- |
  | `dev.<command>` | `target` | The `dev` command's arguments |
  | `guard.*` (26.11.18 and earlier), such as `guard.deny`, `guard.ask`, and `guard.bypass` | `target` | The call the guard refused or asked about, or that an escape hatch let through. A refused file edit keeps the file's path, and a refused tool call keeps the tool's name |
  | `guard.*` (26.11.18 and earlier) | `detail` | The refusal the agent read. What it quotes in backticks is masked as a command line, so its operators stay, and devkit's `…` stays. A quote whose first word is not a program on devkit's list is free text and becomes its length, so a quote of one word stays only as such a program, such as `cat`. The guard quotes each value as one Markdown code span, with a longer fence when the value holds a backtick, so the value cannot end the quote early. The rest of the refusal stays |
  | `guard.irreversible` (26.11.18 and earlier) | `detail` | The words of the irreversible command, or the shell text the guard could not parse |
  | `calls.unclosed` | `detail` | The call that got no result, and how long devkit waited for it |
  | `job.*` | `target` | The job's `dev` arguments |
  | Any record | `evidence.command` | A command line |

  For example, `dev task add "Fix the Acme export" --why "Ana asked" --type bug` is recorded as
  `add [text not recorded: 19 characters] --why [text not recorded: 9 characters] --type bug`. Only the stored
  record changes: the command runs as typed. A value that names
  something stays, so a name passed as one, such as a crew member, a step's name, or a path, can still reach a
  record.
- **Delegated work and handoff commits.** `crew.delegate` keeps who delegated to whom and the step, and what the
  member should do only as its length. A commit to the handoff repository (`handoff.*`) keeps the item's id and the
  operation, such as `T-…: done`, and the rest of its message, which holds the title and the reason, only as its
  length.
- **Extension execution and app MCP acceptance.** `ext.apply` records the action, entry id, exit status, and
  SHA-256 digests of the command and result. It does not store raw arguments, stdout, or stderr. App MCP acceptance
  records and notices identify the configuration by digest, without copying URLs, arguments, or passwords.
- **Approval lines in older records.** Records from devkit 26.11.18 and earlier can hold evidence for a decision or
  a grant (`crew.evidence`): what it covered (the decision id, or a grant's action and target), the channel, and the
  id of the developer's message in the transcript. Of the line itself it kept only the length, and the answer only
  when it was one of the decision's options; an answer from the question tool also kept `match` and `asked` (in
  `evidence`) and `label` (in `ref`). devkit writes no new evidence records.
- **Decision questions.** A member's question for the developer (`crew.decision`) keeps the decision id, the member
  who asked, the option labels (redacted as usual), the labels the dialog offers for them (`labels`, the
  recommended one marked), the session, and the length of the question, which often quotes the developer.
- **Free-text answers.** The developer's answer (`crew.decided`, and the `dev crew decide` command that records it)
  keeps the decision id, who answered, and the answer only when it names one of the decision's options, by its
  label or its letter; any other answer becomes `[free text: N characters]`.
- **The user name in a home folder.** `/Users/<name>/…` and `/home/<name>/…` become `~/…`.

Secrets are redacted, as far as the redactor knows their shape. Tokens, keys, passwords, passphrases, credentials,
passwords in URLs, and the secrets that common commands take (`curl -u`, `mysql -p`, `sshpass -p`, `docker login -p`,
`gh secret set --body`, and a value echoed into `--password-stdin` or `--with-token`) are redacted in every text field before a record is written: the target, the details, the
reason, the run and step names, and the branch. The redactor matches known shapes, so a secret in a shape it does
not know can still reach a record. Add a pattern for it with `audit.redact.extra`. While the team policy's patterns
cannot load, those fields keep their length only (see [Export and forwarding](#export-and-forwarding)).

### Records written before 26.11.3

Records written before 26.11.3 can hold what the preceding rules now keep out. That covers records in the older v1
format, and v2 records sealed before the fix, some of which hold crew message text or a home path. devkit cleans every
record with the current rules whenever it reads one, so `dev audit`, `dev audit show`, and `dev audit export` do not
show that text. A v2 record that the cleaning changed is marked `"cleaned": true`. Its `hash` still covers the stored
line, which is what `dev audit verify` checks. Records written before 26.11.5 can also hold the free text of a
command line, and reading masks it the same way. Their `dev` arguments were joined by spaces, so a word cannot tell
where an argument ends: once a word in them is free text, reading keeps the command and its subcommands, and after
them only the option names that `dev`'s parser knows, the ids, and numbers of up to six digits. A command that a
guard refusal quotes holds its words joined the same way and is masked by the same rule.

Message retry keys (`--request-id`) and page cursors (`--cursor`) are free text: audit records keep only their
length. Message file inputs record the cleaned path, never the file contents. Acknowledgments retain the supplied
digest and delivery generation so the recorded operation can be traced without recording its message body.

The stored lines still hold the text, in three places:

- On this machine, in `~/.local/state/devkit/audit/`, until a planned re-seal, not built yet, rewrites them with
  the current rules and records the migration.
- In the handoff repository's working copy, until the first publish after that re-seal.
- In the handoff repository's git history, until the purge described in [What git keeps](#what-git-keeps).

### The rights of the people recorded

| Right | How |
| --- | --- |
| Access (article 15) | `dev audit export --team --actor <user>@<host> --since <YYYY-MM-DD> --format jsonl -o <file>` writes that person's records from every machine to a file only its owner can read (mode 0600) |
| Rectification (article 16) | Records are never edited: the hash chain would break and `dev audit verify` would report it. A correction is a new record that names the id of the old one |
| Erasure (article 17) | While the retention period runs, the records are still needed for the security purpose, and a request can be declined for that reason. After it, they must go, from git history too, through the purge procedure in [What git keeps](#what-git-keeps) |

### Before teammates share an audit repository

A new handoff repository is private to the developer who owns it. Once other people get access, each of them can
read every machine's full activity log, which is systematic monitoring of employees. Under the European Data
Protection Board's criteria, that likely requires a data protection impact assessment (DPIA). Before you give anyone
access:

1. Screen the change for a DPIA, with your organization's data protection officer if it has one.
2. Write a legitimate-interest assessment: why the team needs the records, and how the people recorded are protected.
3. Tell the people recorded what is collected and why (the table at the start of this section).
4. Purge the history first ([What git keeps](#what-git-keeps)): until then, git still holds the message text of
   records written before 26.11.3.

The planned design for shared repositories, not built yet, publishes, per month file, a seal (the chain's last hash
and the record count) plus the refusal, security, and configuration records, instead of every record. The seal still lets
`dev audit verify` prove that nothing was changed.

## The record: schema v2

Each record is one line of JSON in a month file. `dev audit show <id>` prints one whole:

```bash
dev audit show 01M6SFPEZ2
```

```json
{
  "v": 2,
  "id": "01M6SFPEZ26QJ416WAYP5S80YH",
  "ts": 1794004171.746,
  "time": "2026-11-06T22:29:31.746Z",
  "actor": {"user": "ana", "host": "m-0a1b2c3d", "agent": "claude", "session": "3f2a91c0-7b1"},
  "action": "audit.verify",
  "category": "security",
  "severity": "info",
  "target": "2 file(s)",
  "detail": "the chain holds",
  "outcome": "ok",
  "evidence": {"count": 1786},
  "project": "fireflysoftwaresolutions/devkit",
  "run": "Release 26.11.3",
  "step": "dashboard security review",
  "prev": "7f904a76bdc9a984342cf669adefc7256878b425d92883e8569d5e2acd098926",
  "hash": "898d797ae27697e94de6fa746db8b8d7484b31e6b9718b24ee29f7ffd2e444b0"
}
```

| Field | Always there | What it holds |
| --- | --- | --- |
| `v` | yes | The schema version, `2` |
| `id` | yes | A ULID: 26 characters of Crockford base 32 that sort by time. `dev audit show` takes any unique start of it |
| `ts` · `time` | yes | When, as Unix seconds with milliseconds, and as ISO 8601 in UTC |
| `actor` | yes | Who acted: `user` (the GitHub login), `host` (this Mac's machine id, the same whatever its hostname; a record from before 26.11.16 holds the short hostname), `label` (only in records from before this release: the hostname at the time; records are published to the shared handoff repository, so devkit no longer writes it), `agent` (`claude`, `codex`, `job`, or `human`), and `session` (the first 12 characters of the session id). A `dev` command a Codex thread runs is recorded under `codex` and that thread's session, even though Codex passes no devkit variable to its commands; only your own terminal is `human` |
| `crew` | when a crew member acts, or devkit cannot tell who did | `member` (its id), `role`, `lane` (the subagent's id), `parent` (the lane that launched it, or `main`), and `tool_use_id` (its launch). A record made during a run whose caller devkit cannot tie to a lane (a background job a command started, or a command that open calls of the tech lead and a member both hold) holds `{"member": "unknown"}` and no `step`; `dev audit --member unknown` lists them |
| `project` · `branch` · `run` · `step` | when known | The repository, its branch, the goal of the session's `dev progress` run, and the step running in the caller's lane: a crew member's own step, the main session's for the tech lead |
| `action` | yes | What happened, as a dotted name such as `dev.commit`, `crew.gate`, `crew.decided`, or `audit.verify` |
| `category` · `severity` | yes | Set by the action, see [Categories, severities, and outcomes](#categories-severities-and-outcomes) |
| `target` · `detail` | yes | What the action was about and how, redacted. At most 300 and 600 characters |
| `outcome` | yes | `ok`, `blocked`, `failed`, or `recorded` |
| `why` | for an override | The reason given with `--why`, redacted. At most 300 characters |
| `evidence` | when there is some | `command`, `commit`, `check`, `exit`, `duration`, `cwd`, `tool`, `rule`, `count`, `file`, `flag` (an escape hatch's name), `channel`, `transcript_uuid`, and, for a question tool answer, `match` and `asked` |
| `ref` | when there is some | `withheld` when the record keeps lengths instead of text because the team policy's redaction patterns did not load (it names the file); links to other records and decisions: `id`, `by`, `was`, `type`, `result`, `answer`, `label`, `labels`, `asked_by`, `irreversible`, `mode`, `overrides`, `routed`, and `session`; for messages, claims, and escalations between agent sessions, `kind`, `recipients`, and `resource`; for [caller identity records](#caller-identity-records), `agent`, `ancestors`, `lane_var`, and `thread`; for the irreversible-action gate, `tier` (1 or 2), `setting` (`audit` or `enforce`), `grant` (the grant's id), `source` (`policy-auto`, `E1`, `E2`, `step`, or `preapproval`), `repo` (the `owner/repo` acted on), `origin_repo` (the session's own project, when automatic policy allowed an operation in another project of your workspace), and, for a program the guard cannot classify, `kind` (`unclassified:unread`, `unclassified:built`, `unclassified:unsure`, or `unclassified:opaque`); for the developer's approval lines (`crew.evidence`), `kind` `typed-midturn` when devkit read the line from the session's transcript because you typed it while the session worked, and `since` (the running turn's start) for a line the prompt hook received while the session worked: `dev audit verify` accepts the transcript's record of it when it is stamped between `since` and the record's time; automatic authorization also retains `operation`, `mode_source`, `policy_digest`, `origin_repo`, `account`, and `principal_kind`; for a guard refusal, `refusal_id` (the stable id of the place that refused, or `hooks:<rule>` for a refusal the hook makes itself; see [The bug registry](bug-registry.md)) |
| `prev` · `hash` | yes | The hash chain, see [Verify the chain](#verify-the-chain) |

The `flag` and `match` evidence, the `irreversible` reference, and the fields of the irreversible-action gate appear
only in records from devkit 26.11.18 and earlier. A stored record has no other fields: devkit drops whatever a caller
passes outside this list. A record read back
can also carry `"cleaned": true` (see [Records written before 26.11.3](#records-written-before-26113)). Records
written in the older v1 format (`ts`, `user`, `host`, `agent`, `session`, `action`, `target`, `detail`, `outcome` at
the top level) are
still read in the v2 shape (with `v` set to `1`, an id that starts with `V1`, and no chain fields), cleaned, and
exported like the rest, but they are not part of the chain.

### Categories, severities, and outcomes

devkit sets the category and severity from the action, by its longest registered prefix. For example, `dev.commit`
falls under `dev` (change, info), `dev.release` is release and notice, `crew.gate` is refusal and warning, and
`audit.publish` is security and critical.

| Category | Records |
| --- | --- |
| `change` | `dev` commands that change something |
| `decision` | Crew decisions and modes, plans, and RACI overrides (and guard questions in records from 26.11.18 and earlier) |
| `refusal` | `dev` command refusals and the stub, test, and crew gates (and guard refusals in records from 26.11.18 and earlier) |
| `security` | Blocked settings changes, unverified callers, lane conflicts, and the audit's own checks |
| `lifecycle` | Installs and updates, sessions, crew lanes starting and ending, and missed status blocks |
| `job` | Scheduled jobs, CI watches, cleanups, and sleep |
| `release` | Releases and delivery policies |
| `data` | Handoff writes, file commands (`dev rm`, `mv`, `cp`), and audit exports |
| `config` | Changes to devkit's configuration, jobs, and policies |

Severities run `info`, `notice`, `warning`, `critical`. An outcome of `failed` or `blocked` raises a record to at
least `warning`, and a tampered result raises it to `critical`.

Agent sessions on this Mac that talk to each other (see [Teams and machines › Sessions on one
Mac](teams.md#sessions-on-one-mac)) write `peer.*` records. Each keeps the kind of message (`ref.kind`), the sessions
it went to (`ref.recipients`), and the claimed resource (`ref.resource`), never the text:

| Action | Category and severity | Records |
| --- | --- | --- |
| `peer.send` · `peer.answer` | `lifecycle`, info | A note, question, or answer sent, with its recipients |
| `peer.refused` | `refusal`, notice | A message, claim, or settle line devkit refused: a crew member or the developer's own terminal sent, a claim is held, or a prompt tried to settle twice |
| `peer.claim` · `peer.release` | `decision`, info | A claim taken, renewed, queued, or given up |
| `peer.break` | `security`, warning | Another session's claim broken with `dev session release` |
| `peer.escalate` · `peer.settle` | `decision`, notice | An escalation and the developer's settling of it, with the choice's letter or `free text` |
| `peer.sweep` | `job`, info | The hourly cleanup of old messages, ended sessions' mailboxes, and lapsed claims |

### Caller identity records

devkit names who runs each `dev` command by its call token (see
[The crew › Who is calling](crew.md#who-is-calling)). Each failed check of a token, each state change that has no
token, and each fact about Codex threads that changes how devkit names a caller writes one record. A record holds:

- the call's id, in `target` and `crew.tool_use_id`;
- the session devkit found, in `ref.session`, and its agent, in `ref.agent`;
- the `dev` command line, masked like any other, in `evidence.command`;
- the processes between the command and its agent, in `ref.ancestors`;
- the lane variable and the `CODEX_THREAD_ID` the command carried, in `ref.lane_var` and `ref.thread`;
- the outcome.

No record holds the token's value: devkit replaces it with a redaction mark in every text it keeps.

| Action | Category and severity | Records |
| --- | --- | --- |
| `caller.no-token` | `security`, warning | A state change without a call token, in a session whose calls carry one: refused |
| `caller.stale-token` | `security`, warning | A token whose call has closed or ran past its time limit, such as a background command's: refused for a state change |
| `caller.foreign-session` | `security`, warning | A token of another session's call, such as one a nested `codex exec` or `claude -p` inherited: refused for a state change |
| `caller.foreign-agent` | `security`, warning | A token used under another agent process than the one that made the call: refused for a state change |
| `caller.token-foreign-use` | `security`, warning | A token used outside the process its call pinned and the processes below it, or after that process id went to another process, naming both: refused for a state change |
| `caller.token-unpinned` | `security`, warning | A token whose call's agent process devkit does not know: refused for a state change |
| `caller.token-unexplained-use` | `security`, warning | A `dev` command resolved the token of a tech lead's call that runs no `dev` command, found when that call ends |
| `caller.token-unlisted-use` | `security`, warning | State changes through a call's token by `dev` commands the call's own text does not run, such as one a script or hook the call started ran, with their count and the caller, found when that call ends: allowed and recorded |
| `caller.conflict` | `security`, warning | A token and a lane variable, or a Codex thread, that name different callers: refused for a state change |
| `caller.mismatch` | `security`, warning | A Codex command that ran on another thread than the caller its `dev` command resolved to, found afterwards in the rollout |
| `caller.spoof-suspect` | `security`, warning | A member's command that drops or sets `DEVKIT_CALL` or `CODEX_THREAD_ID` (`unset`, `env -u`, `env -i`, an assignment); the member also gets advice |
| `caller.thread` | `security`, warning | A Codex command without a token that `CODEX_THREAD_ID` named: a read, or a state change that its caller's own open call holds exactly |
| `calls.forged` | `lifecycle`, notice | A token whose call the hook never opened: refused for a state change |
| `calls.pin-contested` | `lifecycle`, notice | Another process wrote a call's pin before its shell did, so its token names nobody: refused for a state change, and recorded again when the call ends |
| `calls.pin-malformed` | `lifecycle`, notice | A call's pin holds something other than a process id, or cannot be read: refused for a state change |
| `calls.pin-early` | `lifecycle`, notice | A call's pinned process started outside the call's own time: refused for a state change |
| `calls.pin-late` | `lifecycle`, notice | A call's pin was written more than 30 seconds after the call opened, found when that call ends |
| `calls.pin-replaced` | `lifecycle`, notice | A call's pin is not the file its first verified use saw, found when that call ends |
| `calls.token-skipped` | `lifecycle`, notice | A shell call that went out without its token, with the reason; its state changes are then refused |
| `codex.thread-shared` | `security`, notice | A member's call carried the root thread's id, so the root thread names the tech lead no more in that session |

`dev audit --action caller` and `dev audit --action calls` list them. The run's own history keeps the other half: in
`dev progress history`, each step change a token-verified call makes names how devkit knew the caller (`how`) and
the call's id (`tid`).

## Levels: how much is recorded

`audit.level` decides which records are written. A team policy can raise it, never lower it.

| Level | What is recorded |
| --- | --- |
| `security` | Only refusals, security and configuration records, any blocked outcome, any override with a `why`, and the audit's own records |
| `changes` (default) | Everything devkit records: every `dev` command that changes something, every write to the handoff repository, every scheduled job, and the crew's lifecycle and decisions. Read-only commands and dry runs are not recorded |
| `all` | The same, plus every read-only `dev` command (`dev status`, `dev ls`, `dev audit`, and the rest) |

Refusals, overrides, and configuration changes are recorded at every level, so `security` is the way to record less.

## Verify the chain

Each record's `hash` is the SHA-256 of its canonical JSON (keys sorted, no spaces, without `hash` itself). Its `prev`
is the `hash` of the v2 record before it in the same month file, with any lines between them (v1 records) sealed
into the link. Editing, removing, or inserting a record breaks the chain from that point on. What the daily `audit`
job publishes to the handoff repository is the anchor: each local month file must start with the files published of
its month, in order, and only add to them. A held day counts by its size and SHA-256.

```bash
dev audit verify
```

```text
  ✓ ~/.local/state/devkit/audit/2026-11.jsonl  1786 record(s), 3324 v1 line(s) outside the chain
  ✓ ~/Development/.handoff/audit/ana@m-s9jn9xnz/2026-11/2026-11-16.jsonl  912 record(s), 3324 v1 line(s) outside the chain
  ✓ ~/Development/.handoff/audit/ana@m-s9jn9xnz/2026-11/2026-11-17.jsonl  614 record(s)
✓ the chain holds: 3312 record(s) · 260 not yet published, so not yet anchored
```

It checks every local month file and every copy in the handoff repository, and exits `0` when the chain holds. A
month published by day is one chain across its one-file copy, when it has one, and its day files, so a day file
removed from the middle breaks the link at the first record of the next one. On a problem it prints the file, the
line, and the record's id, and exits `1`:

| Problem | Meaning |
| --- | --- |
| does not match its hash | The record was changed after it was written |
| no message of the developer backs it | A `crew.evidence` record points to no prompt or question tool answer of the developer in the session's transcript |
| the transcript shows another answer, match, label, or asked | `dev audit verify` reran the matcher on the question and label in the transcript, against the decision as the session's run holds it, else as its `crew.decision` record keeps it (`options`, `labels`), and the evidence record says otherwise. `asked` is checked while the run still holds the question |
| does not link to the one before it | A record was removed, inserted, or changed before this one, or a day file before it is missing |
| differs from the published copy | Records were changed or removed after they were published |
| differs from what a held day's record says | Records of a day held over 45 MiB were changed or removed after the job recorded that day |
| unreadable | The month file, or a held day's record, could not be read |

Some checks cannot run on every record. When a `crew.decision` record kept its options or labels only up to the
300-character limit, verify cannot rebuild that decision: it prints a note marked `!` and still exits `0`, because a
check it cannot run is no evidence of a change. A held day's records are checked only on the machine that wrote them:
elsewhere, verify prints a note for that day and follows the chain on from where its record says the chain stands.

Records not published yet are counted as _not yet published, so not yet anchored_, and so are the records of a held
day. Every run is itself recorded as `audit.verify`, with `critical` severity when it fails. The daily job never
publishes over a file this machine's month file no longer extends; it records `audit.publish` as blocked and
critical instead, and leaves the published files as they are. How the job splits a month into days, and what it does
with a day over 45 MiB, is in [The handoff copy](#the-handoff-copy).

## Query and export

`dev audit` reads this machine's records; `--team` reads every machine's from the handoff repository.

```bash
dev audit                                     # the last 7 days, the newest 60 records
dev audit -n 200 --action guard               # the newest 200 guard records of the last 7 days
dev audit --since 2026-11-01 --until 2026-11-06 --category refusal
dev audit --member Tank --severity warning    # this severity and above
dev audit --grep 'push' --outcome blocked     # a regular expression over action, target, detail, and why
dev audit show 01M6SFPEZ2                     # one record, whole
dev audit stats --days 30                     # records per category and per day
dev audit config                              # the effective settings, the files, and the next prune
```

The filters are `--days`, `--since`, `--until`, `--action` (the start of the action), `--actor` (`user@<machine id>`,
the user alone, or the agent), `-p` or `--project`, `--member`, `--category`, `--severity`, `--outcome`, and `--grep`. `--format` takes
`table` (the default), `json`, or `csv`; `--json` is the same as `--format json`. The table shows the newest `-n`
matches (60 by default), and its header counts every match; `json`, `csv`, and `dev audit export` return every match.

### Query speed

Cleaning a record for reading is most of a query's time, so devkit checks what it can on the stored record first and
cleans only the records that pass:

| Filter | Checked on |
| --- | --- |
| `--days`, `--since`, `--until`, `--action` | The stored record |
| `--category`, `--severity`, `--outcome`, `--actor` | The stored record when it is v2, since cleaning never changes these fields; the cleaned record when it is v1 |
| `-p`, `--member`, `--grep` | The cleaned record, since cleaning can change them: a home folder becomes `~` |

The table view with only time and action filters cleans just the rows it shows. On a month of 91,000 records, the
default `dev audit` shows a week of 51,000 matches in under a second.

If a query is slow, a filter in the last row of the table, `json`, `csv`, or an export is cleaning every record of
the window. Narrow it with a filter from the first two rows, such as `--days 1` or `--action guard`, and run it again.

### Export and forwarding

`dev audit export` takes the same filters and writes the records in one of four formats:

| Format | What you get |
| --- | --- |
| `jsonl` (default) | One record per line, as stored. A record cleaned on read carries `"cleaned": true` and no longer matches its `hash`; `dev audit verify` checks the stored line |
| `csv` | A header row, then one row per record: `id`, `time`, `user`, `host`, `agent`, `session`, `member`, `role`, `lane`, `project`, `branch`, `run`, `step`, `action`, `category`, `severity`, `outcome`, `target`, `detail`, `why`, `evidence`, `ref`, and `hash`. A cell that starts with `=`, `+`, `-`, or `@` gets a leading `'`, so a spreadsheet never runs it as a formula |
| `ocsf` | One OCSF 1.3.0 Base Event per line (class 0, activity Other), with the severity and status mapped and devkit's own fields under `unmapped`. `device.hostname` is the hostname this Mac carried at the record's time, which only this Mac's own export knows (another Mac's records show their machine id), and `device.uid` the machine id |
| `cef` | One CEF line per record: `CEF:0\|Firefly Software Solutions\|devkit\|<version>\|<action>\|<target>\|<severity>`, then the time, user, host, action, category, outcome, and detail, with the id, project, member, step, why, and hash as custom strings. `shost` and the syslog header carry the hostname as `device.hostname` does, and `deviceExternalId` the machine id |

```bash
dev audit export --format cef --days 1
dev audit export --team --since 2026-11-01 --format csv -o ~/Desktop/audit-november.csv
```

Without `-o`, the export goes to the terminal. With `-o`, devkit writes the file with mode 0600, because the records
hold names and hosts. Both are recorded as `audit.export`.

To send every new record to another system as it is written, set `audit.forward`:

| Value | Where each record goes |
| --- | --- |
| `""` (default) | Nowhere else |
| An absolute path to a file | Appended to that file as one JSON line (mode 0600) |
| `syslog` | The Mac's local syslog, as a CEF line (facility 13, log audit) |
| `syslog://host:port` | A syslog server over UDP, as a CEF line |

`audit.redact.extra` adds your own regular expressions to the redactor, for secrets that only your team uses. A team
policy can add patterns, never remove them. Every place devkit screens text for secrets uses the same patterns, the
built-in rules plus `audit.redact.extra` of this Mac and of the team: the redactor, bug reports before they leave this
Mac, the lesson screen before recall, the hook payloads that `DEVKIT_HOOK_CAPTURE=1` keeps, and the check on messages
that agents type.

When those patterns cannot load, devkit withholds text instead of redacting it with fewer patterns: the built-in rules
cannot tell that a token only your patterns know is a secret. This happens when `policy/audit.json` exists but cannot
be read, is not valid JSON, or is not a JSON object, or when its `redact` section was rejected (an `extra` that is not
a list, a pattern that is not a regular expression, or an unknown key), or when it has a key devkit does not know, at
the top or inside `retention` or `redact`, such as a misspelled `redcat`, a capitalized `Redact`, the dotted
`audit.redact.extra` written as one key, an `extra` under `retention`, or the whole policy wrapped in `audit`
(`policy/audit.json` holds the audit section itself, so the patterns go under `redact.extra`). The `audit` section of
this Mac's `~/.config/devkit/config.json` fails the same way: a section that is not a JSON object, an `extra` written
as one string instead of a list, a pattern that does not compile, or an unknown key. Until you repair the file:

- Every text devkit would redact becomes `[text not recorded: N characters]`: hook and CI notices, checkpoints, the
  crew messages it shows, and the text fields of new audit records. Queries and exports show older records the same
  way.
- A new audit record keeps the fields that hold no free text: its time, actor, action, category, severity, outcome,
  crew, run id, step number, exit code, and other numbers. `ref.withheld` names the file and what is wrong with it.
  It names an unknown key only when the key is a word of at most 24 letters, underscores and dots that no built-in
  rule and no pattern that loaded finds a secret in, such as `redcat`; any other key, such as a token pasted as a
  key, is named by its length and where it is (`an unknown key of 40 characters in audit.retention`). The refusals,
  `dev sync` and `dev audit config` say it the same way, and `dev audit config` quotes a wrong value only when it is
  a number, `true`, `false`, `null` or a short word. `dev config validate` names a key of `config.json` that devkit
  does not read, or a map key it refuses, by the same rule.
- A text field of a record keeps its value only when the whole value is one of devkit's own ids or words: a decision,
  message or reply id (`D3`, `M12`, `R4`); a work item, lesson or plan id at the length devkit writes it (`T-`,
  `B-`, `F-`, `S-`, `L-` or `P-` and 12 characters, or 11 for an id an older devkit wrote); a session message
  or escalation id (`S-` or `E-` and 8 characters); a session (`claude:<uuid>`, `codex:<uuid>`); a crew member's
  exact id, name or alias from the roster, or `devkit:<id>`; a project id from this Mac's projects map; or a state
  or outcome word (`pending`, `decided`, `withdrawn`, `blocked`, `failed`, …). Such a value is still withheld when a
  built-in rule or a pattern that did load (this Mac's, and the team's when its file is valid JSON) finds a secret
  in it. `dev audit show --project` and `--member` still find the record, and a decision record keeps its decision
  id and session. Every other value is its length, even one that starts with an id or ends with a member's name: a
  reason (`why`), a detail, a decision's options, labels and answers (a bare `A` too).
- A refused command does not show its reason, and its `fix:` line names the file to repair.
- Text that an agent types for devkit to store is refused, because devkit cannot tell whether it holds a secret:
  crew and session messages, `dev crew decide` answers (a bare `A` too), and the short fields of a session message.
  The refusal names the file to repair. devkit's own notices (a delegated task, a declined or withdrawn launch, hook
  and CI results) are still delivered and stored, with their text withheld.
- A bug report is held for you instead of sent, and a captured hook payload keeps each text as its length.
- The lesson screen holds every lesson out of recall. It still runs the built-in rules, the patterns that did load,
  and its other screens on every field: a lesson they find something in is held for that reason, which is written
  into the lesson. Any other lesson's reason says that it cannot be checked for secrets, and nothing is written into
  it, so it is recalled again once the patterns load. `dev lesson release` is refused until then, with the file to
  repair: the screen could not ask the team's patterns, so a lesson it holds for a URL may also hold a token only
  those patterns know.
- `dev audit config` lists the file's errors and says that text is withheld.

Audit records go on being written, with lengths instead of text. Fix the file the `fix:` line or `dev audit config`
names, and redaction with every pattern starts again at the next command.

`dev sync` never publishes a team policy file (`policy/<name>.json`) that no Mac could load, and never stops for one.
When this Mac's copy cannot be loaded, the sync moves its text out of the handoff repository to a private file under
devkit's state (`policy-held/`, named with the time, a `.why` file beside it with the reason), and the repository keeps
the version it last pulled, so the pull and the push go on. devkit records the hold as a critical, blocked
`handoff.push`, tells the live sessions, and says it at every `dev sync` and every session start until you delete the
held copy. To keep the change, repair it (`dev audit config` shows what is wrong), put it back into the handoff
repository, and delete the held copy; to drop the change, delete the held copy.

- **A good version you staged stays staged.** When you staged a version that loads (`git add`) before the file
  broke, the file goes back to that version and the sync commits it. A staged version that no Mac could load either
  is held as well. When you staged the file's deletion (`git rm --cached`), the deletion stays staged and the sync
  commits it.
- **A file devkit cannot move stays on this Mac.** When the held copy cannot be written (the state folder is not
  writable, or the disk is full), or git cannot put the repository's version back (another git process holds
  `index.lock`), the file stays in the handoff repository as it was and is left out of every commit and push. The
  hold is recorded once per file and reason, and said at each `dev sync`. A pull may stop until you repair the file
  or move it out.
- **A commit that leaves a broken policy is never pushed.** `dev sync` checks each team policy file that this Mac's
  commits not pushed yet change, whatever made them (a raw `git commit`, `dev initiative import`, an older devkit
  whose push failed). While one of them leaves a file no Mac could load, the push is refused: devkit records it once
  per file and reason as a critical, blocked `handoff.push`, says the file, the reason and the repair at every
  `dev sync`, and `dev status` shows the failed push.
- **Remove the broken version from the history, not only from the file.** The broken text may hold a secret you
  pasted, and a repair committed on top still pushes the broken commit with it. Undo the commits not pushed yet with
  `git reset --soft @{u}`, which keeps their changes staged, or amend the commit with `git commit --amend` when it is
  the last one. The next `dev sync` commits the changes again; while the file is still broken, it holds the file
  aside the same way, and the push carries only the versions every Mac can load.

Because a key devkit does not know withholds text, add a top-level key that a newer devkit introduces to
`policy/audit.json` only after every Mac on the team runs that version (`dev update`): a Mac on an older version
reads the key as unknown and withholds text until it updates.

See also [Teams and machines](teams.md), [Configuration › Jobs and audit](configuration.md#jobs-and-audit), and
[Scheduled jobs and the audit trail](jobs-and-audit.md).
