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

## Where the records live

Each machine appends its records to one segment per UTC day in `~/.local/state/devkit/audit/` (mode 0600, in a 0700
folder). The daily `audit` job closes each segment once its day has passed, compresses it, and lists it in a manifest:

```text
~/.local/state/devkit/audit/
  manifest.json                  the closed segments in chain order: size, SHA-256, records, time span,
                                 records per day and per action, chain head and link
  2026-11-24.01.jsonl            the open segment: today's (UTC), where records are appended
  2026-11/
    2026-11-23.01.jsonl.gz       a closed segment, gzip-compressed
    2026-11-23.02.jsonl.gz       the same day's next segment, after the first reached 16 MiB
    2026-10.jsonl.gz             a month file of devkit 26.11.27 or earlier, compressed once its month passed
  2026-11.jsonl                  this month's file of devkit 26.11.27 or earlier: read, never written again
  reseal/                        the maps of dev audit reseal (see Re-seal older records)
```

- **One segment per UTC day.** A new segment starts on a new UTC day, or when the open one reaches 16 MiB. A record
  whose clock went back stays in the open segment.
- **One chain across segments.** The first record of a segment links to the end of the segment before it, so
  removing a whole segment breaks the chain at the next one. See [Verify the chain](#verify-the-chain).
- **Closed segments are compressed.** JSON lines compress about eight to ten times. Readers decompress them as they
  read, and skip the closed segments whose time span or actions a query rules out, so `dev audit --days 1` reads
  about one day, not a month. The embargo check reads every segment: the manifest is outside the chain, so it never
  decides whether an embargo is found. `dev audit verify` reports an edited manifest. When a manifest, this
  machine's or one in the handoff copy, lists a segment whose file is gone, the embargo check cannot tell what that
  segment held: it holds every push and backup and names the file, as it does for a file it cannot read.
- **A close that stops is finished by the next one.** The job compresses a segment, lists it in the manifest, then
  removes the plain file. If it stops between those steps, the plain file stays the segment until the next run
  closes it again, and a record written meanwhile never goes into a segment the manifest lists already.
- **Month files of an earlier devkit stay as they are.** devkit 26.11.27 and earlier wrote one file per local month,
  each its own chain. devkit reads and verifies them as they are and never writes to them again: the first segment
  links to the end of the newest month file, and the manifest records that point as `origin`. A session still running
  an earlier devkit can append to this month's file until it restarts, so the job compresses a month file only once
  its month has passed, and holds the file's own lock, the one such a session appends under, from its last read of
  the file until it removes it.

### Known gaps

- **A session still on devkit 26.11.27 or earlier misses the compressed segments until it restarts.** It reads only
  the plain files at the top of the audit folder, so its `dev audit` queries and its embargo check skip this
  machine's closed segments. With `audit.publish` on, its embargo check still finds an embargo in the published
  segments of the handoff copy. Restart every session after the update: `dev install` and `dev update` list them.
- **Such a session can lose a record at the moment the job closes last month's file.** The job holds the file's lock
  while it closes it, but a session that opened the file just before waits for that lock and then appends to the
  file the job has removed. It takes a record dated last month, written in the moment the job runs.

## Retention

The daily `audit` job publishes the last three months to `audit/<user>@<machine id>/` in the handoff repository (see
[The handoff copy](#the-handoff-copy)), then prunes. Prune also covers the folders this Mac wrote under a hostname it
had before (`audit/<user>@<hostname>/`), for the month files whose last record this Mac wrote.

| Key | Default | What it keeps |
| --- | --- | --- |
| `audit.retention.local_months` | `13` | Months of records on this machine, segments and month files alike: a full year plus one month, so a yearly review or a year-over-year incident still has its records |
| `audit.retention.team_months` | `13` | This machine's months in the handoff copy: each month's folder of segments and day files, and a one-file copy where an earlier devkit published one. The team copy is read by more people, so by default it keeps no longer than the machine |
| `audit.retention.floor_months` | `3` | The least any retention may be. Three months covers the time it takes to notice an incident and investigate it, so 3 is also the hard minimum: `dev config validate` refuses 1 or 2 |
| `audit.hold` | `false` | A legal hold: `true` stops all pruning on this machine and in the handoff copy |

A `local_months` or `team_months` under the floor is raised to it. To record less, set `audit.level` to `security`
rather than shorten the floor. The team policy, `policy/audit.json` in the handoff repository, can raise any of these
values for every machine, never lower them. A team that must keep records longer, for example for a client contract
or an external audit, raises `team_months` there and says why in the commit.

Every prune is itself recorded as `audit.prune`, with the months and the number of records it removed. The manifest
keeps the link of the last segment it removed as `start`, so the first segment kept still verifies. Each manifest
drops a segment before its file is removed, so a prune that stops halfway never leaves a manifest listing a file that
is gone. A reseal map
goes with the `audit.reseal` record that backs it: prune removes the map wherever it removes the segment that holds
that record (on this machine, in the handoff copy, or both), and names it in `audit.prune`. The segments the map
bridged are older, so they are gone by then.
`dev audit config` shows the effective values, where the records are, their size, and what the next prune removes.

### What git keeps

Pruning removes month files from the handoff repository's **working copy only**. Git history keeps every published
month until that history is rewritten, so today `team_months` does not delete anything from git. `dev audit config`
says so on its `git history` line. `dev audit reseal` rewrites the text of
[records written before 26.11.3](#records-written-before-26113) on this machine and in the working copy, not in the
history. A procedure to purge pruned months and those older lines from the history is planned: it rewrites a shared
repository, so the developer runs it. It must run before anyone else gets access to the handoff repository, or before
the first published month (2026-11) passes `team_months` (at 13 months, 2027-12), whichever comes first.

Each machine prunes only its own folder. The folder of a retired machine stays until that cleanup also covers other
machines' folders, which is planned and not built yet.

## The handoff copy

The daily `audit` job publishes each machine's records to the handoff repository one UTC day at a time. GitHub
refuses a pushed file over 100 MB, and from then on every push of the handoff repository, so no published file is
ever over 45 MiB, and no day's file is rewritten.

```text
audit/ana@m-s9jn9xnz/
  manifest.json                  the published segments: the entries of this Mac's manifest
  2026-11.jsonl                  a one-file copy that devkit 26.11.19 or earlier published: it stays as it is
  2026-11/
    2026-11-16.jsonl             the records of the month file after that copy's last byte, to the end of the 16th
    2026-11-17.jsonl             one file per UTC day of the month file
    2026-11-18.held.json         a day over 45 MiB: its size, count, and SHA-256, not its records
    2026-11-19.jsonl
    2026-11-24.01.jsonl          a closed segment, published once as it was closed (not compressed: git compresses
                                 what it stores, and an earlier devkit reads it)
  reseal/                        the maps of this Mac's re-seals, which every machine's verify reads
```

- **One file per closed segment.** The job publishes each closed segment of the last three months once, with the
  manifest that lists it, and never rewrites it. A segment holds at most about 16 MiB, so none is ever held. The open
  segment waits for the next run, so a record waits up to about 29 hours to be anchored: the rest of its UTC day,
  plus the time to the next 05:00 run. When the copy begins after the first segments, because they are older than
  three months or were pruned before the first publish, its manifest records as `start` where its chain begins.
- **A published segment that differs is never written over.** If this Mac's closed segment and the published one
  differ, the job leaves the published one as it is and records `audit.publish` as blocked and critical, and
  `dev audit verify` names the segment.
- **Month files of an earlier devkit, one file per closed day.** A day is closed once a record of a later UTC day
  follows it in the local month file, once that month has ended on the machine, or, for the last day of this month's
  file, once this devkit writes segments. The job publishes each closed day once; a record that a session still on
  an earlier devkit appends to that last day writes its day file again.
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

`dev audit --team`, `dev audit export --team`, `dev audit verify`, and the embargo check read every layout and count
each record once. A machine still on devkit 26.11.27 or earlier reads the published segments in its queries and its
embargo check. Its `dev audit verify`, though, checks each segment on its own and reports the first record of each as
not linked: update that machine.

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

- **Who acted:** the GitHub login, this Mac's machine id and its hostname at the time, the agent (`claude`, `codex`,
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
not know can still reach a record. Add a pattern for it with `audit.redact.extra`.

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

- On this machine, in `~/.local/state/devkit/audit/`, until `dev audit reseal --apply` rewrites them (see
  [Re-seal older records](#re-seal-older-records)).
- In the handoff repository's working copy, until that re-seal, which rewrites this machine's copy too.
- In the handoff repository's git history, until the purge described in [What git keeps](#what-git-keeps).

### Re-seal older records

`dev audit reseal` shows which files hold records that today's rules would store differently, and how many, and
changes nothing:

```bash
dev audit reseal
```

```text
  ~/.local/state/devkit/audit/2026-11.jsonl  285 record(s), 41 older line(s) to re-seal
  14 file(s) of this Mac's handoff copy follow them
Nothing changed. To re-seal, run from your own terminal: dev audit reseal --apply
```

`dev audit reseal --apply` rewrites them. It runs only from your own terminal, never from an agent's session, and only
on a trail that verifies. Your own terminal means a terminal on standard input, no agent's environment variables, and
no agent among the command's parent processes; the `agent` name a record shows is not enough, since a session can set
it. An agent's call is refused and recorded as `audit.reseal` blocked.

1. It runs `dev audit verify` first. When the chain does not hold, it refuses, changes nothing, exits `1`, and records
   `audit.reseal` as blocked and critical: a rewrite would hide what broke the chain.
2. It writes the reseal map, `~/.local/state/devkit/audit/reseal/<id>.json`, before it rewrites anything: each
   rewritten file's old and new size, SHA-256, chain head and link, and the ids of the records it changed. The old
   chain heads stay there, outside the files it rewrites.
3. It cleans each record with today's rules and chains the records of each file again from the first change. A
   segment's first record keeps its link to the old end of the segment before it; verify accepts that link only
   through a reseal map that an `audit.reseal` record backs. It never changes the fields devkit's own checks read
   back, even when a pattern of `audit.redact.extra` matches them (see [Export and forwarding](#export-and-forwarding)):
   a pattern in the team policy, which any teammate can commit, so cannot lift an embargo or unhook the chain.
4. It rewrites this machine's copy in the handoff repository to match, with the map, and commits it.
5. It records `audit.reseal` with the map's SHA-256 (`evidence.check`), then closes and publishes the segment that
   holds that record, so every machine can check the map.

Older-format lines (v1) are cleaned in their own shape; their `V1…` id, which is the hash of their line, changes.
It prepares the closed segments first; other devkit commands wait to write only while it rewrites the files they
append to. A month file that a session on an earlier devkit wrote again after its month was closed is re-sealed here
only, since it was never published. Afterwards, reading shows no `"cleaned": true`. Git history still
holds the old lines until the purge in [What git keeps](#what-git-keeps).

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

Each record is one line of JSON in a segment (or, for records of devkit 26.11.27 and earlier, a month file).
`dev audit show <id>` prints one whole:

```bash
dev audit show 01M6SFPEZ2
```

```json
{
  "v": 2,
  "id": "01M6SFPEZ26QJ416WAYP5S80YH",
  "ts": 1794004171.746,
  "time": "2026-11-06T22:29:31.746Z",
  "actor": {"user": "ana", "host": "m-0a1b2c3d", "label": "ana-mbp", "agent": "claude", "session": "3f2a91c0-7b1"},
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
| `actor` | yes | Who acted: `user` (the GitHub login), `host` (this Mac's machine id, the same whatever its hostname; a record from before 26.11.16 holds the short hostname), `label` (the hostname at the time, for display), `agent` (`claude`, `codex`, `job`, or `human`), and `session` (the first 12 characters of the session id). A `dev` command a Codex thread runs is recorded under `codex` and that thread's session, even though Codex passes no devkit variable to its commands; only your own terminal is `human` |
| `crew` | when a crew member acts, or devkit cannot tell who did | `member` (its id), `role`, `lane` (the subagent's id), `parent` (the lane that launched it, or `main`), and `tool_use_id` (its launch). A record made during a run whose caller devkit cannot tie to a lane (a background job a command started, or a command that open calls of the tech lead and a member both hold) holds `{"member": "unknown"}` and no `step`; `dev audit --member unknown` lists them |
| `project` · `branch` · `run` · `step` | when known | The repository, its branch, the goal of the session's `dev progress` run, and the step running in the caller's lane: a crew member's own step, the main session's for the tech lead |
| `action` | yes | What happened, as a dotted name such as `dev.commit`, `crew.gate`, `crew.decided`, or `audit.verify` |
| `category` · `severity` | yes | Set by the action, see [Categories, severities, and outcomes](#categories-severities-and-outcomes) |
| `target` · `detail` | yes | What the action was about and how, redacted. At most 300 and 600 characters |
| `outcome` | yes | `ok`, `blocked`, `failed`, or `recorded` |
| `why` | for an override | The reason given with `--why`, redacted. At most 300 characters |
| `evidence` | when there is some | `command`, `commit`, `check`, `exit`, `duration`, `cwd`, `tool`, `rule`, `count`, `file`, `flag` (an escape hatch's name), `channel`, `transcript_uuid`, and, for a question tool answer, `match` and `asked` |
| `ref` | when there is some | Links to other records and decisions: `id`, `by`, `was`, `type`, `result`, `answer`, `label`, `labels`, `asked_by`, `irreversible`, `mode`, `overrides`, `routed`, and `session`; for messages, claims, and escalations between agent sessions, `kind`, `recipients`, and `resource`; for [caller identity records](#caller-identity-records), `agent`, `ancestors`, `lane_var`, and `thread`; for the irreversible-action gate, `tier` (1 or 2), `setting` (`audit` or `enforce`), `grant` (the grant's id), `source` (`policy-auto`, `E1`, `E2`, `step`, or `preapproval`), `repo` (the `owner/repo` acted on), `origin_repo` (the session's own project, when automatic policy allowed an operation in another project of your workspace), and, for a program the guard cannot classify, `kind` (`unclassified:unread`, `unclassified:built`, `unclassified:unsure`, or `unclassified:opaque`); for the developer's approval lines (`crew.evidence`), `kind` `typed-midturn` when devkit read the line from the session's transcript because you typed it while the session worked, and `since` (the running turn's start) for a line the prompt hook received while the session worked: `dev audit verify` accepts the transcript's record of it when it is stamped between `since` and the record's time; automatic authorization also retains `operation`, `mode_source`, `policy_digest`, `origin_repo`, `account`, and `principal_kind`; for a guard refusal, `refusal_id` (the stable id of the place that refused, or `hooks:<rule>` for a refusal the hook makes itself; see [The bug registry](bug-registry.md)) |
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
| `changes` (default) | Everything devkit records: every `dev` command that changes something, every write to the handoff repository, every scheduled job, and the crew's lifecycle and decisions. Read-only commands and dry runs are not recorded. A `dev sync` or a handoff push that went well (`dev.sync`, `handoff.push`) is counted instead, and one summary per action and UTC day closes that day's segment |
| `all` | The same, plus every read-only `dev` command (`dev status`, `dev ls`, `dev audit`, and the rest), and each `dev sync` and handoff push on its own |

Refusals, overrides, and configuration changes are recorded at every level, so `security` is the way to record less.

A summary record keeps the action, the UTC day as its target, the count in `evidence.count`, the agents that ran
them in its detail, and `ref.type` `summary`; its time is the last one it counts, and its agent is `job`. A sync or
push that failed or was blocked is always recorded on its own. `dev audit --action dev.sync` finds both.

## Verify the chain

Each record's `hash` is the SHA-256 of its canonical JSON (keys sorted, no spaces, without `hash` itself). Its `prev`
is the `hash` of the v2 record before it, with any lines between them (v1 records) sealed into the link. The first
record of a segment links to the end of the segment before it (its manifest `link`), so the chain runs across
segments. A month file of an earlier devkit is its own chain, and the first segment links to its end as it was when
this devkit started writing segments (the manifest's `origin`). Editing, removing, or inserting a record, or removing
a segment, breaks the chain from that point on. The manifest anchors each closed segment by its size, SHA-256, record
count, and link, and verify reads each closed segment again to check the rest of its entry too: the time span and the
records per day and per action that readers skip segments by. What the daily `audit` job publishes to the handoff repository is the anchor across machines: each
closed segment must be the one published, and each local month file must start with the files published of its
month, in order, and only add to them. A held day counts by its size and SHA-256.

```bash
dev audit verify
```

```text
  ✓ ~/.local/state/devkit/audit/2026-11.jsonl  1786 record(s), 3324 v1 line(s) outside the chain
  ✓ ~/.local/state/devkit/audit/2026-11/2026-11-23.01.jsonl.gz  773 record(s)
  ✓ ~/.local/state/devkit/audit/2026-11-24.01.jsonl  212 record(s)
  ✓ ~/Development/.handoff/audit/ana@m-s9jn9xnz/2026-11/2026-11-16.jsonl  912 record(s), 3324 v1 line(s) outside the chain
  ✓ ~/Development/.handoff/audit/ana@m-s9jn9xnz/2026-11/2026-11-23.01.jsonl  773 record(s)
✓ the chain holds: 4456 record(s) · 212 not yet published, so not yet anchored
```

It checks every local unit (segments and month files) and every copy in the handoff repository, and exits `0` when the
chain holds. A month published by day is one chain across its one-file copy, when it has one, and its day files, so
a day file removed from the middle breaks the link at the first record of the next one; each machine's published
segments are one chain, checked against its published manifest. On a problem it prints the file, the line, and the
record's id, and exits `1`:

| Problem | Meaning |
| --- | --- |
| does not match its hash | The record was changed after it was written |
| no message of the developer backs it | A `crew.evidence` record points to no prompt or question tool answer of the developer in the session's transcript |
| the transcript shows another answer, match, label, or asked | `dev audit verify` reran the matcher on the question and label in the transcript, against the decision as the session's run holds it, else as its `crew.decision` record keeps it (`options`, `labels`), and the evidence record says otherwise. `asked` is checked while the run still holds the question |
| does not link to the one before it | A record was removed, inserted, or changed before this one, or a segment or a day file before it is missing |
| differs from its manifest entry | A closed segment's size, SHA-256, record count, time span, records per day or per action, chain head, or link is not what the manifest recorded when the job closed it: the segment or the manifest changed. The problem names the fields that differ |
| is in the manifest, but its file is gone | A closed segment was removed |
| the first segment links to a month file as it was at byte N | The month file of an earlier devkit changed before the point where the segments start |
| no audit.reseal record backs the reseal map | A reseal map was changed, or written by something other than `dev audit reseal` |
| differs from the published copy | Records were changed or removed after they were published |
| differs from what a held day's record says | Records of a day held over 45 MiB were changed or removed after the job recorded that day |
| unreadable | A segment, a month file, the manifest, or a held day's record could not be read |

Some checks cannot run on every record. When a `crew.decision` record kept its options or labels only up to the
300-character limit, verify cannot rebuild that decision: it prints a note marked `!` and still exits `0`, because a
check it cannot run is no evidence of a change. A held day's records are checked only on the machine that wrote them:
elsewhere, verify prints a note for that day and follows the chain on from where its record says the chain stands.

Records not published yet are counted as _not yet published, so not yet anchored_, and so are the records of a held
day. Every run is itself recorded as `audit.verify`, with `critical` severity when it fails. The daily job never
publishes over a segment that differs from this machine's, or a file this machine's month file no longer extends; it
records `audit.publish` as blocked and critical instead, and leaves the published files as they are. How the job splits a month into days, and what it does
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

A query reads only the closed segments that can hold a match: the manifest gives each one's first and last record
time and its actions, so `--days`, `--since`, `--until`, and `--action` skip the rest without opening them. The open
segment and the month files of an earlier devkit that are not compressed yet are always read.

Cleaning a record for reading is most of the rest of a query's time, so devkit checks what it can on the stored record
first and cleans only the records that pass:

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
| `ocsf` | One OCSF 1.3.0 Base Event per line (class 0, activity Other), with the severity and status mapped and devkit's own fields under `unmapped`. `device.hostname` is the hostname at the time and `device.uid` the machine id |
| `cef` | One CEF line per record: `CEF:0\|Firefly Software Solutions\|devkit\|<version>\|<action>\|<target>\|<severity>`, then the time, user, host, action, category, outcome, and detail, with the id, project, member, step, why, and hash as custom strings. `shost` and the syslog header carry the hostname at the time, and `deviceExternalId` the machine id |

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

The daily summaries of `dev.sync` and handoff pushes at level `changes` are forwarded too, when they are written.

`audit.redact.extra` adds your own regular expressions to the redactor, for secrets that only your team uses. A team
policy can add patterns, never remove them. These patterns never apply to the fields devkit's own checks read back
from the trail, when a record is written or re-sealed: a `git.embargo` record's `branch`, `target`, `detail`,
`project`, and `evidence.commit`, an `audit.reseal` record's `ref.id` and `evidence.check`, and every field of an
`audit.prune` record. The built-in rules still redact them when the record is written. An embargo's reason is in the
handoff note in plain text anyway.

See also [Teams and machines](teams.md), [Configuration › Jobs and audit](configuration.md#jobs-and-audit), and
[Scheduled jobs and the audit trail](jobs-and-audit.md).
