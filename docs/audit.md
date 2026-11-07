# The audit trail

This page is the reference for devkit's audit trail: how long devkit keeps its records, what personal data they hold
and how the people they describe can use their rights under the GDPR (the EU General Data Protection Regulation),
what each record contains, how much is recorded, and how to verify, query, and export the records. For the daily job
that publishes them and a first look at `dev audit`, see
[Scheduled jobs and the audit trail](jobs-and-audit.md#the-audit-trail).

## Retention

Each machine appends its records to `~/.local/state/devkit/audit/YYYY-MM.jsonl` (mode 0600, in a 0700 folder). The
daily `audit` job copies the last three month files to `audit/<user>@<host>/` in the handoff repository, then prunes.

| Key | Default | What it keeps |
| --- | --- | --- |
| `audit.retention.local_months` | `13` | Month files on this machine: a full year plus one month, so a yearly review or a year-over-year incident still has its records |
| `audit.retention.team_months` | `13` | This machine's month files in the handoff copy. The team copy is read by more people, so by default it keeps no longer than the machine |
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

- **Who acted:** the GitHub login, the short host name, the agent (`claude`, `codex`, `job`, or `human`), the
  session id, and, when a crew member acts, its member id, lane, and launch id.
- **What happened:** the action, its target and details, the outcome, the project, branch, run and step, the reason
  given for an override (`why`), and evidence such as the command, the commit, or the exit code.

devkit keeps these out of every record it writes:

- **Prompt text and message text.** A crew message is recorded by its id, sender, recipient, and length, and
  `dev crew ask` and `dev crew answer` by their naming arguments (`--to`, the message id) and the length of the text.
  Crew messages often quote the developer's requests, so their text stays in the session's own records.
- **The developer's approval lines.** Evidence for a decision or a grant (`crew.evidence`) keeps what it covers (the
  decision id, or a grant's action and target), the channel, and the id of the developer's message in the
  transcript. Of the line itself (`approve D<n>: …`, or the question and answer from the question tool) it keeps only
  the length: a hash of a short answer could be guessed, and `dev audit verify` finds the developer's message by its
  id. It keeps the answer only when that answer is one of the decision's options; free text becomes
  `[free text: N characters]`. An answer from the question tool also keeps three fields, which show how a click
  became an option and still show it when the transcript is gone after 30 days:
  - `match` (in `evidence`): `exact` (devkit's own label, word for word), `letter` (an offered label that names an
    option by its letter, such as `A: Skip them`), `words` (one that names it by its words), or `free` (text you
    typed yourself). For an irreversible decision it is always `exact`.
  - `asked` (in `evidence`): `exact` when the question was devkit's own, `reworded` when the tech lead reworded it.
  - `label` (in `ref`): the label you clicked, only when the dialog offered it. Labels are the tech lead's text, like
    the options `crew.decision` keeps, and are redacted, home-cleaned, and cut to 120 characters. Text you typed
    yourself (**Other**) is never kept: it stays a length.

  The question itself is kept as a length only, with no hash: devkit's own words plus a short context are easy to
  guess, so a hash in an export would confirm a guess.
- **Decision questions.** A member's question for the developer (`crew.decision`) keeps the decision id, the member
  who asked, the option labels (redacted as usual), the labels the dialog offers for them (`labels`, the
  recommended one marked and the option that performs an irreversible action with what it runs), the session, and
  the length of the question, which often quotes the developer.
- **Free-text answers.** The developer's answer (`crew.decided`, and the `dev crew decide` command that records it)
  keeps the decision id, who answered, and the answer only when it names one of the decision's options, by its
  label or its letter; any other answer becomes `[free text: N characters]`.
- **The user name in a home folder.** `/Users/<name>/…` and `/home/<name>/…` become `~/…`.

Secrets are redacted, as far as the redactor knows their shape. Tokens, keys, passwords, passphrases, credentials,
passwords in URLs, and the secrets that common commands take (`curl -u`, `mysql -p`, `sshpass -p`, `docker login -p`,
`gh secret set --body`, and a value echoed into `--password-stdin` or `--with-token`) are redacted in every text field before a record is written: the target, the details, the
reason, the run and step names, and the branch. The redactor matches known shapes, so a secret in a shape it does
not know can still reach a record. Add a pattern for it with `audit.redact.extra`.

### Records written before 26.11.3

Records written before 26.11.3 can hold what the preceding rules now keep out. That covers records in the older v1
format, and v2 records sealed before the fix, some of which hold crew message text or a home path. devkit cleans every
record with the current rules whenever it reads one, so `dev audit`, `dev audit show`, and `dev audit export` do not
show that text. A v2 record that the cleaning changed is marked `"cleaned": true`. Its `hash` still covers the stored
line, which is what `dev audit verify` checks.

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
  "actor": {"user": "ancongui", "host": "mac", "agent": "claude", "session": "962d6aba-c6a"},
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
| `actor` | yes | Who acted: `user` (the GitHub login), `host` (the short host name), `agent` (`claude`, `codex`, `job`, or `human`), and `session` (the first 12 characters of the session id) |
| `crew` | when a crew member acts | `member` (its id), `role`, `lane` (the subagent's id), `parent` (the lane that launched it, or `main`), and `tool_use_id` (its launch) |
| `project` · `branch` · `run` · `step` | when known | The repository, its branch, the goal of the session's `dev progress` run, and the step running in it |
| `action` | yes | What happened, as a dotted name such as `dev.commit`, `guard.deny`, `crew.decided`, or `audit.verify` |
| `category` · `severity` | yes | Set by the action, see [Categories, severities, and outcomes](#categories-severities-and-outcomes) |
| `target` · `detail` | yes | What the action was about and how, redacted. At most 300 and 600 characters |
| `outcome` | yes | `ok`, `blocked`, `failed`, or `recorded` |
| `why` | for an override | The reason given with `--why`, redacted. At most 300 characters |
| `evidence` | when there is some | `command`, `commit`, `check`, `exit`, `duration`, `cwd`, `tool`, `rule`, `count`, `file`, `flag` (an escape hatch's name), `channel`, `transcript_uuid`, and, for a question tool answer, `match` and `asked` |
| `ref` | when there is some | Links to other records and decisions: `id`, `by`, `was`, `type`, `result`, `answer`, `label`, `labels`, `asked_by`, `irreversible`, `mode`, `overrides`, `routed`, and `session`; for the irreversible-action gate, `tier` (1 or 2), `setting` (`audit` or `enforce`), `grant` (the grant's id), `source` (`E1`, `E2`, `step`, or `preapproval`), and `repo` (the `owner/repo` acted on) |
| `prev` · `hash` | yes | The hash chain, see [Verify the chain](#verify-the-chain) |

A stored record has no other fields: devkit drops whatever a caller passes outside this list. A record read back
can also carry `"cleaned": true` (see [Records written before 26.11.3](#records-written-before-26113)). Records
written in the older v1 format (`ts`, `user`, `host`, `agent`, `session`, `action`, `target`, `detail`, `outcome` at
the top level) are
still read in the v2 shape (with `v` set to `1`, an id that starts with `V1`, and no chain fields), cleaned, and
exported like the rest, but they are not part of the chain.

### Categories, severities, and outcomes

devkit sets the category and severity from the action, by its longest registered prefix. For example, `dev.commit`
falls under `dev` (change, info), `dev.release` is release and notice, `guard.deny` is refusal and warning, and
`audit.publish` is security and critical.

| Category | Records |
| --- | --- |
| `change` | `dev` commands that change something |
| `decision` | Guard questions, crew decisions and modes, plans, and RACI overrides |
| `refusal` | Guard refusals and the stub, test, and crew gates |
| `security` | Blocked settings changes, unverified callers, lane conflicts, and the audit's own checks |
| `lifecycle` | Installs and updates, sessions, crew lanes starting and ending, and missed status blocks |
| `job` | Scheduled jobs, CI watches, cleanups, and sleep |
| `release` | Releases and delivery policies |
| `data` | Handoff writes, file commands (`dev rm`, `mv`, `cp`), and audit exports |
| `config` | Changes to devkit's configuration, jobs, and policies |

Severities run `info`, `notice`, `warning`, `critical`. An outcome of `failed` or `blocked` raises a record to at
least `warning`, and a tampered result raises it to `critical`.

## Levels: how much is recorded

`audit.level` decides which records are written. A team policy can raise it, never lower it.

| Level | What is recorded |
| --- | --- |
| `security` | Only refusals, security and configuration records, any blocked outcome, any override with a `why`, and the audit's own records |
| `changes` (default) | Everything devkit records: every `dev` command that changes something, every guard decision, every write to the handoff repository, every scheduled job, and the crew's lifecycle and decisions. Read-only commands and dry runs are not recorded |
| `all` | The same, plus every read-only `dev` command (`dev status`, `dev ls`, `dev audit`, and the rest) |

Refusals, overrides, and changes to safeguards are recorded at every level, so `security` is the way to record less.

## Verify the chain

Each record's `hash` is the SHA-256 of its canonical JSON (keys sorted, no spaces, without `hash` itself). Its `prev`
is the `hash` of the v2 record before it in the same month file, with any lines between them (v1 records) sealed
into the link. Editing, removing, or inserting a record breaks the chain from that point on. The copy that the daily
`audit` job publishes to the handoff repository is the anchor: each local month file must start with its published
copy and only add to it.

```bash
dev audit verify
```

```text
  ✓ ~/.local/state/devkit/audit/2026-11.jsonl  1786 record(s), 3324 v1 line(s) outside the chain
  ✓ ~/Development/.handoff/audit/ancongui@mac/2026-11.jsonl  0 record(s), 1159 v1 line(s) outside the chain
✓ the chain holds: 1786 record(s) · 1786 not yet published, so not yet anchored
```

It checks every local month file and every copy in the handoff repository, and exits `0` when the chain holds. On a
problem it prints the file, the line, and the record's id, and exits `1`:

| Problem | Meaning |
| --- | --- |
| does not match its hash | The record was changed after it was written |
| no message of the developer backs it | A `crew.evidence` record points to no prompt or question tool answer of the developer in the session's transcript |
| the transcript shows another answer, match, label, or asked | `dev audit verify` reran the matcher on the question and label in the transcript, against the decision as the session's run holds it, else as its `crew.decision` record keeps it (`options`, `labels`), and the evidence record says otherwise. `asked` is checked while the run still holds the question |
| does not link to the one before it | A record was removed, inserted, or changed before this one |
| differs from the published copy | Records were changed or removed after they were published |
| unreadable | The month file could not be read |

Some checks cannot run on every record. When a `crew.decision` record kept its options or labels only up to the
300-character limit, verify cannot rebuild that decision: it prints a note marked `!` and still exits `0`, because a
check it cannot run is no evidence of a change.

Records written since the last publish are counted as _not yet published, so not yet anchored_. Every run is itself
recorded as `audit.verify`, with `critical` severity when it fails. The daily job never publishes a local file over a
published copy it no longer extends; it records `audit.publish` as blocked and critical instead. It never publishes a
month file over 45 MB either, because GitHub refuses files over 100 MB and then every handoff push: that month's copy
stays as it was, the job records `audit.publish` as blocked with `warning` severity, and `dev audit stats` and
`dev doctor` say so until devkit publishes daily segments instead of whole months, which is planned.

## Query and export

`dev audit` reads this machine's records; `--team` reads every machine's from the handoff repository.

```bash
dev audit                                     # the last 7 days, the newest 60 records
dev audit --since 2026-11-01 --until 2026-11-06 --category refusal
dev audit --member Tank --severity warning    # this severity and above
dev audit --grep 'push' --outcome blocked     # a regular expression over action, target, detail, and why
dev audit show 01M6SFPEZ2                     # one record, whole
dev audit stats --days 30                     # records per category and per day
dev audit config                              # the effective settings, the files, and the next prune
```

The filters are `--days`, `--since`, `--until`, `--action` (the start of the action), `--actor` (`user@host` or the
agent), `-p` or `--project`, `--member`, `--category`, `--severity`, `--outcome`, and `--grep`. `--format` takes
`table` (the default), `json`, or `csv`; `--json` is the same as `--format json`.

`dev audit export` takes the same filters and writes the records in one of four formats:

| Format | What you get |
| --- | --- |
| `jsonl` (default) | One record per line, as stored. A record cleaned on read carries `"cleaned": true` and no longer matches its `hash`; `dev audit verify` checks the stored line |
| `csv` | A header row, then one row per record: `id`, `time`, `user`, `host`, `agent`, `session`, `member`, `role`, `lane`, `project`, `branch`, `run`, `step`, `action`, `category`, `severity`, `outcome`, `target`, `detail`, `why`, `evidence`, `ref`, and `hash`. A cell that starts with `=`, `+`, `-`, or `@` gets a leading `'`, so a spreadsheet never runs it as a formula |
| `ocsf` | One OCSF 1.3.0 Base Event per line (class 0, activity Other), with the severity and status mapped and devkit's own fields under `unmapped` |
| `cef` | One CEF line per record: `CEF:0\|Firefly Software Solutions\|devkit\|<version>\|<action>\|<target>\|<severity>`, then the time, user, host, action, category, outcome, and detail, with the id, project, member, step, why, and hash as custom strings |

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
policy can add patterns, never remove them.

See also [Teams and machines](teams.md), [Configuration › Jobs and audit](configuration.md#jobs-and-audit), and
[Scheduled jobs and the audit trail](jobs-and-audit.md).
