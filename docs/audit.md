# The audit trail

This page explains how long devkit keeps its audit records, what personal data they hold, and how the people they
describe can use their rights under the GDPR (the EU General Data Protection Regulation). For what each record
contains and how to query it, see [Scheduled jobs and the audit trail](jobs-and-audit.md#the-audit-trail).

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
says so on its `git history` line. A procedure to purge pruned months from the history is planned; it must run before
the first published month (2026-11) passes `team_months`, which at 13 months is 2027-12.

Each machine prunes only its own folder. The folder of a retired machine stays until that cleanup also covers other
machines' folders (planned for 26.11.4).

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

A record never holds:

- **Prompt text or message text.** A crew message is recorded by its id, sender, recipient, and length, and
  `dev crew ask` and `dev crew answer` by their naming arguments (`--to`, the message id) and the length of the text.
  Crew messages often quote the developer's requests, so their text stays in the session's own records.
- **Secrets.** Tokens, keys, passwords, and credentials in URLs are redacted before a record is written, in every text
  field: the target, the details, the reason, the run and step names, and the branch.
- **The user name in a home folder.** `/Users/<name>/…` and `/home/<name>/…` become `~/…`.

Records written in the older v1 format are cleaned the same way whenever they are read, so a query or an export never
shows what a new record would not hold.

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

The planned design for shared repositories (26.11.4) publishes, per month file, a seal (the chain's last hash and the
record count) plus the refusal, security, and configuration records, instead of every record. The seal still lets
`dev audit verify` prove that nothing was changed.

See also [Teams and machines](teams.md), [Configuration › Jobs and audit](configuration.md#jobs-and-audit), and
[Scheduled jobs and the audit trail](jobs-and-audit.md).
