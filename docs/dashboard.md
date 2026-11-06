# Dashboard

`dev dashboard` opens the command centre. **Home** summarises everything on one screen. From there you drill into
an **initiative** or a **project** and down to a single **task**, and **Team** shows who is working on what across
machines. Every item comes with the one command that moves it forward. The page is a view over the handoff
repository, so it is as current as the last `dev sync`.

```bash
dev dashboard            # regenerate and open in your browser
dev dashboard --no-open  # regenerate only
dev dashboard --path     # print the file path
```

## Views

| View | Keys | The job it does |
|---|---|---|
| **Home** | `g o` | *A summary of everything, no long lists.* It has four parts. **KPI strip:** initiatives, projects (and how many are not cloned here), open tasks, doing, blocked, people active, live sessions and PRs failing CI; each tile links to the filtered list. **Needs you:** the top five of the action list with their commands, and a link to all of them. **Team now:** one row per person with their live session, run progress, ETA, subagents and locks, plus any collisions. Below these sit compact **initiative cards** (status, progress, milestone, due date, member projects, open, doing and blocked tasks, who is on it) and **Since yesterday** (sessions, commits, merged PRs, completed tasks). |
| **Initiatives** | `g i` | Every initiative as a card (goal, progress, member projects by status, people) or a table, filtered by status and sorted by priority, due date, progress or activity. A milestones-and-due-dates roadmap follows, then a link to the projects outside any initiative. Click an initiative to open its page. |
| **Projects** | `g p` | A dense table (cards optional) with the health dot, status and priority, progress, the task in progress and who holds it, the latest agent status, PRs with a CI dot each, 7-day activity, last activity and who is on it. It is sorted by *needs attention*, can be grouped by initiative, owner, status or priority, and filtered by the facet row. Click a project to open its page. |
| **Team** | `g m` | One card per person (`user@host`) from the presence files in the handoff repo. Each shows their live sessions (agent, project, branch, how long it has run, progress of the current run and the latest status block), their locks with expiry, the tasks they hold and their recent session-log lines. **Collisions** are listed first: two sessions on the same project and branch, and expired locks that are still held, each with its command. |
| **Tasks** | `g t` | Every work item across projects and initiatives, as a board (Now · Next · Later · Closed) or grouped by scope. Filter by status, scope and *claimed by*, including *claimed by me*. Every card shows the lock chip and opens the task drawer. |
| **Activity** | `g a` | A timeline first: live sessions, the session logs of every project and initiative (who, agent, branch, status), completed and blocked tasks, merged PRs, operations and canary misses, over 7 or 30 days. A commits-per-day chart (with a table view) and the most active projects follow. |
| **Explorer** | `g e` | The handoff repository as a tree, with a safe Markdown viewer and find-in-file (`↵` / `⇧↵` step through matches). Up to 2 MB is embedded, newest first. |
| **Health** | `g h` | Providers (sign-in, plan, quota and reset time) and **Time** (see below). Guard decisions grouped by rule, the instruction canary per agent, configuration drift, every worktree and disk usage per project. |

### Initiative page — `#/initiatives/<slug>`

The page opens with a breadcrumb and a header: status, priority, milestone, due date, owner, honest progress (and
where it comes from), and the commands `dev initiative show <slug>` and `dev task -i <slug> add "…" --now`. A row of
numbers comes next, then the tabs:

| Tab | Content |
|---|---|
| Overview | Goal, scope and current state; what is in progress (doing and blocked, with lock chips); who is on it; risks and decisions; links |
| Projects | The member projects in collapsible groups, searchable (name, status, workstream, task in progress, people). Group by family, status, owner, workstream or none. Each row has its status, progress, what is in progress or the latest status, who is on it and `dev clone <id>` when not cloned here. A 77-repository platform is a few groups, not a wall of chips |
| Tasks | The initiative's own items **plus member-project items linked to it** (`links: initiative:<slug>`, the same rule as `dev task -i`). Filter by status (open, doing, blocked, to do, closed) and source (initiative, from projects), grouped Now · Next · Later · Closed |
| Plans | Versioned plans and specs from `plans/initiatives/<slug>/`: status (draft, approved, superseded), version, last change and who made it, the full history, and *edited · needs a revision* with the `dev plan revise … --why` command when the body changed without one |
| Sessions | The session log of the initiative and its member projects, by day, with who, agent and branch |
| Docs & source | The source archives and the documents under `initiatives/<slug>/docs/` |

### Project page — `#/projects/<owner>/<repo>`

The page opens with a breadcrumb (Projects › owner › name) and a header: health reasons, initiatives, progress and
the launch commands (`dev claude`, `dev codex`, `dcd`, `dev handoff`). A project that is **not cloned on this Mac**
shows a clear call to action with `dev clone <id>`; its note, tasks, plans and sessions still come from the handoff
repo. Tabs: **Overview** (live sessions with their runs and status blocks, the latest status, what is in progress and
up next, purpose and state, who is on it, repository facts, time on this project), **Tasks**, **Pull requests** (CI,
review, merge state, size, a command each), **Worktrees** (`dev wt rm`), **Plans**, **Sessions** and **Handoff note**
(the full note, rendered).

A member of an initiative that has no handoff note yet still gets a page: the not-cloned call to action and the
initiatives that list it.

### The task drawer — `?task=<id>`

Every task, wherever it appears (Home, an initiative or project page, Tasks, Team, search, the palette), opens the same
drawer:

- **Lock state.** Who holds it, with which agent, since when, and until when. An expired lock is shown in amber with
  the release command.
- **Commands.** `dev task -p <id>|-i <slug> start|done|release|move|block|cancel|history <task-id>`, with
  `--why "…"` wherever the change must be explained. `-p` / `-i` come before the verb, as `dev task` expects.
- **Details.** Id, project or initiative, assignee, created and updated (who and when), links and the item file in the
  handoff repo.
- **History.** Every version, newest first: version, operation, detail, who (user@host and agent), when, and why.

Checkbox tasks from notes that have no work items yet are shown read-only, with their `dev task` number. The next
`dev sync` imports them.

### Live runs and time

Agents report progress on long runs (`dev progress start|step|block|done`). Wherever a live session appears (Home
*Team now*, Team, the project page) it shows:

- a progress bar with step/steps, elapsed time against the estimate (the calibrated one when known) and the ETA;
- the latest update and the next step. **Blocked** runs are red, with the blocking reason;
- *N subagents running*, and how long the current turn has been running. That is flagged when it is longer than 90%
  of turns here.

**Time** (Health, and per project on its page) shows how predictable the work is:

- the median and p90 turn length over 30 days;
- **actual / estimate**, the median ratio over finished runs that had an estimate; new estimates are scaled by it;
- the slowest commands of the last 7 days, with credentials masked;
- subagent durations, and recent runs with their actual time against the estimate.

### The status block

Agents end every reply with a status block: `**[Firefly> DevKit]** <project> · <branch> · <task>` followed by
**Done**, **Check** (✓ ⚠ ✗ – plus evidence), **Progress** and **Next**. devkit saves it as one line,
`<project> · <branch> ❯ <task> ❯ <done> ❯ <✓ check> [❯ ▸ <progress>] ❯ next → <next>`.

The dashboard parses saved lines in Python (`status_parts`) and draws them as a small card: the amber `Firefly❯ DevKit`
chip, a project · branch chip and a task chip, then labelled Done, Check (coloured by its glyph), Progress and Next
rows. Tables and lists use a compact one-line form. Older lines keep rendering. The segmented form
(`api ⎇ main ❯ T-… ❯ … ❯ next → …`) parses the same way. The legacy one-line form (`DevKit · x@main · …`) is shown as
written, with the marker removed.

## The action list

It is ordered by what blocks you most. Each item has its command, and the most important one is the single amber
button on the page.

| Order | Item | Command |
|---|---|---|
| 1 | One of **your** PRs is failing CI | `gh pr checks <n> -R <owner/repo> --watch` |
| 2 | Changes requested on your PR | `gh pr view <n> -R <repo> --comments` |
| 3 | Your PR has merge conflicts | `cd <its worktree> && git fetch origin && git rebase origin/<default>` (or `dcd … && gh pr checkout …`) |
| 4 | Your PR waits for review · someone else's PR needs a review | `gh pr view <n> -R <repo> --web` · `gh pr diff <n> -R <repo>` |
| 5 | An expired lock is still held · two sessions on the same branch | `dev task -p <id> release <task-id> --why "lock expired"` · `dev team` |
| 6 | A project or initiative is blocked · a task you hold is blocked | `dev handoff <id>` · `dev initiative show <slug>` · `dev task -p <id> show <task-id>` |
| 7 | Overdue tasks and initiatives | `dev task -p <id> done <task-id>` · `dev task -i <slug> done <task-id>` · `dev initiative show <slug>` |
| 8 | Uncommitted or unpushed work left idle | `cd <checkout> && git status -sb` |
| 9 | Finished worktrees (PR merged or closed, clean) | `dev wt rm <branch>`, or `dev gc --apply` for several |
| 10 | A repo outside the layout · a handoff note behind the code | `dev adopt <path>` · `dev handoff <id>` |
| 11 | Configuration drift · a provider not signed in | `dev config diff` · `claude` / `codex login` |

## Progress, health, people

- **Progress is honest.** It is the share of work items done, `done / (done + open)`, unless the note sets
  `progress:` manually, which the bar marks with ✎. Hover any bar to see which. With no tasks and no manual value it
  shows *no tasks*, never a guess.
- **Health.** A project is *at risk* (red) when one of its PRs fails CI or has changes requested, it is blocked, a
  task is overdue, or it is past its due date. It *needs a look* (amber) for merge conflicts, idle uncommitted or
  unpushed work, or a checkout outside the layout.
- **People** are `user@host`. This Mac's own sessions come from the local registry, which is fresher than its
  presence file. Presence older than 24 hours is ignored.
- **Lock chips** (🔒 who · agent · time left) appear wherever tasks appear. Yours are outlined; expired ones are
  amber.

## Search, filters and the command palette

- **Search** (`/`) covers initiatives, projects, people, tasks (by id or title), PRs, notes, plans, session logs and
  every Markdown file in the handoff repo. Results have type facets and highlighted matches. In Initiatives,
  Projects, Tasks and Activity it filters the view in place.
- **Filters**: the project facet row (health, CI, status, priority, owner, stack, tag, live session, open PRs, not
  cloned, outside initiatives) sits above Projects, Activity and Search. Pages use segmented controls for their own
  filters.
- **Command palette** (`⌘K` / `Ctrl K`) jumps to any view, initiative, project, person, task, PR or file with fuzzy
  matching, and copies every command.

## Keyboard

| Keys | Action |
|---|---|
| `/` | Search |
| `⌘K` · `Ctrl K` | Command palette |
| `g` then `o` `i` `p` `m` `t` | Home · Initiatives · Projects · Team · Tasks |
| `g` then `a` `e` `h` | Activity · Explorer · Health |
| `u` | Up: from a page back to its list |
| `←` `→` (`Home` `End`) | Switch tabs when the tab bar has focus · read the chart day by day when it has focus |
| `j` / `k` | Next / previous item in the current list or drawer |
| `↵` | Open the focused item |
| `Esc` | Close the task drawer, palette or sheet; clear the search |
| `?` | Shortcut sheet |

## Routes and the URL hash

Every page, tab, open task and filter lives in the URL, so any view can be bookmarked, shared with someone who has
the same handoff repo, or reached with the back button. The format is `#/<view>[/<id>][?<key>=<value>&…]`, with
defaults omitted. Ids keep their slashes and dots (`#/projects/gitlab.com/firefly/platform/api`). Links from earlier
versions (`#/portfolio`, `?p=<project>`, `?i=<initiative>`) still resolve.

| Key | Values | Example |
|---|---|---|
| view (path) | `overview` `initiatives` `projects` `team` `tasks` `activity` `explorer` `health` `search` `actions` | `#/team` |
| id (path) | an initiative slug · a project id | `#/initiatives/lending` · `#/projects/acme/api` |
| `tab` | initiative: `projects` `tasks` `plans` `sessions` `docs` · project: `tasks` `prs` `worktrees` `plans` `sessions` `note` | `#/projects/acme/api?tab=prs` |
| `task` | a task id: opens the task drawer | `#/projects/acme/api?tab=tasks&task=T-fwk6cesh07u` |
| `mq` · `mg` | member search · grouping (`family` `status` `owner` `ws` `none`) | `#/initiatives/jafly?tab=projects&mg=status` |
| `tst` · `src` | task status (`open` `doing` `blocked` `todo` `done` `all`) · initiative task source (`all` `own` `projects`) | `#/initiatives/lending?tab=tasks&tst=blocked` |
| `q` | search text | `#/search?q=export` |
| `health` `ci` `status` `priority` `owner` `stack` `tag` | comma-separated values | `#/projects?ci=failing` |
| `live` `prs` `nc` `loose` | `1` (live session · open PRs · not cloned · outside initiatives) | `#/projects?loose=1` |
| `sort` `group` `layout` `dense` | Projects: `attention` `activity` `priority` `tasks` `name` `size` · `none` `initiative` `owner` `status` `priority` · `table` `cards` · `1` | `#/projects?group=initiative` |
| `ist` `psort` `ilayout` | Initiatives: status · `priority` `due` `progress` `activity` · `cards` `table` | `#/initiatives?ilayout=table` |
| `tp` `who` `tmode` | Tasks: scope · claimed by (`me` or `user@host`) · `board` `list` | `#/tasks?who=me&tst=doing` |
| `kind` | Needs-you kind filter | `#/actions?kind=CI` |
| `f` · `fq` | Explorer file · find-in-file text | `#/explorer?f=initiatives/lending/README.md` |
| `range` | `7` or `30` (Activity) | `#/activity?range=30` |
| `types` | search result types | `#/search?q=api&types=initiative,task` |

## Where it comes from

| | |
|---|---|
| File | `~/.local/state/devkit/dashboard/index.html`: one self-contained HTML file (inline CSS, JS and SVG) |
| Regenerated | by `dev dashboard`, after every `dev sync` (session ends, debounced turn ends) and by the hourly background job |
| Refresh | an open tab reloads every 2 minutes while visible (not while a task drawer is open), keeping its view, filters and scroll position |
| Data | The registry (projects, git state, worktrees, PRs, initiatives, live sessions with progress, providers, drift). From the handoff repo: **work items** (`items/<scope>/<id>.md`, with lock and changelog), **presence** (`presence/<user>@<host>.json`), **session logs** (`sessions/<scope>/<YYYY-MM>.md`), **versioned plans** (`plans/<scope>/*.md`), notes and initiative READMEs. Also the PR cache, the instruction canary, guard decisions, the operations log, **timing records** and **active runs** |

The decisions are computed in Python (`lib/devkit/dashboard.py`) and unit-tested (`tests/test_dashboard.py`):

- action priority and commands, project health, *since yesterday*, guard rule grouping, task numbering;
- lock state, collisions, people, initiative-linked items and KPIs;
- session-log and status-line parsing, plan status, timing summaries and credential masking.

The page only renders them.

## Design

The page follows the Firefly Software brand:

- **Identity.** The official Firefly logo (paper colourway on dark, charcoal on light) next to the devkit spark;
  charcoal and paper surfaces; Manrope on the brand type scale.
- **Amber**, the firefly's light, is reserved for the single most important action on each view, the status-block
  chip and data marks. Chart and progress marks use validated amber steps (`#c38530` dark, `#9d671f` light). Status
  colours are fixed and always paired with an icon or a label.
- **Components.** Breadcrumbs, a sticky tab bar per page (arrow keys move between tabs), segmented controls, cards
  with a clear hierarchy, sticky table headers (a table wider than its panel scrolls inside it instead), consistent
  chips and badges, empty states that say what to do next, and instant rendering with no skeletons.
- **Layout.** An 8 px spacing scale and a dense mode. Responsive at 1440, 1280, 1024, 768 and 390 px with no
  horizontal page scroll. Keyboard-first with visible focus rings; respects reduced motion; prints cleanly. The theme
  toggle (◐ or the palette) is remembered, and *Theme: follow system* resets it.

## Privacy and security

The file stays on your Mac; the only network request is the Manrope web font (the page falls back to the system sans
offline). All text from notes, items, presence, logs, PR titles and git is inserted with DOM text APIs, never as HTML.
Links are limited to `http(s)`, `mailto` and in-page anchors, and the embedded data escapes `<`, `>` and `&`. Command
lines in timing records are masked for tokens, passwords and long secrets before they reach the page. The tests
render hostile content (`<img onerror>`, a `</script>` breakout) through every surface to keep it that way.

See also: [Concepts](concepts.md) · [Commands](commands.md) · [Agents](agents.md) · [Delivery](delivery.md)
