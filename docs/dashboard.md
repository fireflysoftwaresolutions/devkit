# Dashboard

`dev dashboard` opens the command center. It is visual first: a number or a shape stands in for a sentence, and the
detail opens in a quick-look modal. **Home** answers "is anything wrong, and where do things stand?" in one screen.
From there you open any item in a modal, or drill into an **initiative** or a **project** page and down to a single
**task**. **Team** shows who is working on what across machines. Every item comes with the one command that moves it
forward. The page is a view over the handoff repository, so it is as current as the last `dev sync`.

```bash
dev dashboard            # regenerate and open in your browser
dev dashboard --no-open  # regenerate only
dev dashboard --path     # print the file path
```

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/dashboard/home-1440-dark.webp">
  <img src="images/dashboard/home-1440-light.webp" alt="The dashboard's Home view: the title says how many urgent items need you; four stat tiles with a segmented bar each (what needs you by urgency, initiatives and projects by on track, at risk, off track and done, open tasks by doing, blocked and to do); the top three Needs you groups; Team now with a progress bar per running session; four Since yesterday counters with 14-day sparklines; and one card per initiative with a progress ring, a waffle of its member projects and a due-date pill." width="100%">
</picture>

## Views

| View | Keys | The job it does |
| --- | --- | --- |
| **Home** | `g o` | _Is anything wrong, and where do things stand?_ The title answers it, for example _28 urgent items need you_ or _Nothing needs you_. **Four stat tiles** follow, each a number and one segmented bar with its legend: what needs you (urgent, needs a look, for information), initiatives and projects (on track, at risk, off track, done), and open tasks (doing, blocked, to do). Each tile links to its list. **Needs you** shows the top three groups; problems of the same kind are one row (for example _5 of your PRs are failing CI_). **Team now** shows each person with a thin progress bar per running session and their lock count; collisions are one warning chip. **Since yesterday** is four counters (sessions, commits, merged PRs, tasks done), each with its 14-day sparkline. Below, one card per **initiative**: a progress ring, a waffle of its member projects (one square each, colored by state), a due-date pill, and the people on it. Every item opens a quick-look modal. At 1440 × 900 the whole summary fits without scrolling. |
| **Initiatives** | `g i` | _How is each programme going?_ A card grid (ring, member waffle, due pill, people, open tasks) with a **Table** toggle for the compact table (status, name and goal, progress, the due date with the milestone under it, tasks, member projects, people, last activity). Filter by status and sort by priority, due date, progress, or activity. A milestones-and-due-dates roadmap follows. A card or a row opens the initiative's quick look; the modal links to the full page. |
| **Projects** | `g p` | A tile grid: a status dot, the name, a progress ring, a CI strip (one dot per open PR: green passing, red failing, yellow running), and the 14-day commit sparkline. **Table** switches to the dense table (health, status, progress, the task in progress, the latest agent status, PRs, 7-day activity on screens wider than 1600 px, last activity, people). It is sorted by _needs attention_, can be grouped by initiative, owner, status, or priority, and filtered by the facet row. A tile or a row opens the project's quick look. |
| **Team** | `g m` | _Who is doing what right now?_ One card per person (`user@host`): the avatar with a live dot, presence, a lock count (amber when a lock has expired), and one bar per running session (project, steps, progress; red when blocked). A person opens their quick look (sessions, claimed tasks with a countdown ring per lock, recent sessions); a session bar opens that session (the run's goal, progress, Latest, Check, Next). **Collisions** are listed first, each with a copy icon for its command. |
| **Tasks** | `g t` | Every work item across projects and initiatives, as a board (Now · Next · Later · Closed) or grouped by scope. Filter by status, scope, and who claimed it (anyone, or only you). Every card shows who holds the lock and opens the task's quick look. |
| **Activity** | `g a` | A calendar heatmap of the last 26 weeks (one square per day, darker for more commits, sessions, and finished tasks; the day's numbers on hover, and a **Table** view), three counters with 14-day sparklines, the timeline (live sessions, session logs, completed and blocked tasks, merged PRs, operations, canary misses over 7 or 30 days), and the most active projects. Commits are kept for 30 days. |
| **Explorer** | `g e` | The handoff repository as a tree, with a safe Markdown viewer and find-in-file (`↵` / `⇧↵` step through matches). Up to 2 MB is embedded, newest first. |
| **Health** | `g h` | Status tiles come first, for Claude Code and Codex (signed in, quota used), guard decisions, the share of replies that started with the status block, configuration drift, and worktrees with disk use. The detail follows: providers, guard decisions grouped by rule, the instruction canary per agent, drift, every worktree, disk usage per project, and **Time** (see below). |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/dashboard/team-1440-dark.webp">
  <img src="images/dashboard/team-1440-light.webp" alt="The Team view: collisions first (two sessions on the same branch, an expired lock that is still held), then one card per person with a live dot on the avatar, a lock count, and a progress bar per running session." width="100%">
</picture>

### Initiative page (`#/initiatives/<slug>`)

_What is its state, what is in progress, and who is on it?_ The page opens with a breadcrumb and a header: the
progress ring, the title, and one line with the state (on track, at risk, off track, done), a due-date pill, the
milestone, and the owner. A **README** link and a **Commands** menu hold `dev initiative show <slug>`,
`dev task -i <slug> add "…" --now`, and `dev initiative add <slug> <owner/repo>`, each with a copy icon. Four stat
tiles follow: member projects as a waffle with its legend, open tasks as a segmented bar, tasks done in 14 days as a
sparkline, and the people on it. Then the tabs:

| Tab | Content |
| --- | --- |
| Overview | **Doing or blocked** (with who holds each lock); **Off-track projects** as tiles; goal, scope, and current state; who is on it; risks and decisions; links |
| Projects | The member projects in collapsible groups, searchable (name, status, workstream, task in progress, people). Group by family, status, owner, workstream, or none. Each row has its status, progress, what is in progress or the latest status, who is on it and `dev clone <id>` when not cloned here. A 77-repository platform is a few groups, not a wall of chips |
| Tasks | The initiative's own items **plus member-project items linked to it** (`links: initiative:<slug>`, the same rule as `dev task -i`). Filter by status (open, doing, blocked, to do, closed) and source (initiative, from projects), grouped Now · Next · Later · Closed |
| Plans | Versioned plans and specs from `plans/initiatives/<slug>/`: status (draft, approved, superseded), version, last change and who made it, the full history, and _edited · needs a revision_ with the `dev plan revise … --why` command when the body changed without one |
| Sessions | The session log of the initiative and its member projects, by day, with who, agent, and branch |
| Docs & source | The source archives and the documents under `initiatives/<slug>/docs/` |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/dashboard/initiative-1440-dark.webp">
  <img src="images/dashboard/initiative-1440-light.webp" alt="An initiative page: a progress ring and the title with its state, due pill and milestone; a README link and a Commands menu; four stat tiles (a waffle of 77 member projects colored by state, open tasks as a segmented bar, a 14-day sparkline of tasks done, the people on it); and the Overview tab." width="100%">
</picture>

### Project page (`#/projects/<owner>/<repo>`)

_What is the state of this repo?_ The page opens with a breadcrumb (Projects / owner / name) and a header: the
progress ring, the name, and one line with the state, initiatives, branch, and last activity (live sessions count as
activity); the health reasons follow as short lines (for example, that a PR is failing CI). The amber button copies
`dev claude <id>`; **More commands** holds `dev codex`, `dcd`, and `dev handoff`. A project that is **not cloned on
this Mac** has `dev clone <id>` as its amber button instead; its note, tasks, plans, and sessions still come from the
handoff repo. Four stat tiles follow: open tasks as a segmented bar, open PRs as a CI strip, commits in 14 days as a
sparkline, and the live sessions as progress bars. The tabs are **Overview** (live sessions, each shown once; doing or blocked and up next; purpose and current state; the latest
status; repository facts; and, behind a disclosure, how long work takes here), **Tasks**, **Pull requests** (CI,
review, merge state, size, a command each), **Worktrees** (`dev wt rm`), **Plans**, **Sessions**, and **Handoff note**
(the full note, rendered).

A member of an initiative that has no handoff note yet still gets a page: the not-cloned call to action and the
initiatives that list it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/dashboard/project-1440-dark.webp">
  <img src="images/dashboard/project-1440-light.webp" alt="A project page: a progress ring and the name with its state, the health reason, an amber button that copies dev claude and a More commands menu, four stat tiles (open tasks, a CI strip of open PRs, a 14-day commit sparkline, live session bars), then the live sessions, the tasks that are doing or blocked, the latest status, and repository facts." width="100%">
</picture>

### Quick-look modals (`?modal=<kind>:<id>`, `?task=<id>`)

Clicking a summary item opens a quick look: a native dialog with the item's visuals and key facts, the next action as
the one amber button, and secondary actions such as copying a command or **Open full page**. Focus stays inside the
dialog, **Esc** or a click in the backdrop closes it, and focus returns to what opened it. Each modal has its own link,
so the back button closes it and a link opens it again.

| Kind | Opens from | Shows | Primary action |
| --- | --- | --- | --- |
| `initiative:<slug>` | Home and Initiatives cards and rows | Ring, member waffle, task bar, current state, milestone, goal, people | Open full page |
| `project:<owner/repo>` | Projects tiles and rows, off-track tiles | Ring, health reasons, CI strip, commit sparkline, task bar, doing or blocked, latest status | Copy `dev claude` (or `dev clone`) |
| `person:<user@host>` | Home Team now, Team cards | Session bars, claimed tasks with lock countdown rings, recent sessions | Copy the release command when a lock expired |
| `session:<user@host>~<n>` | Session bars | The run's goal, progress, Latest, Check, Next | Open project |
| `action:<key>` · `actions:<kind>` | Needs you rows | The problem, where it is, the PR strip, the command | Copy command |
| `since:<sessions\|commits\|merged\|done>` | Since yesterday counters | The full list | — |
| `task:<id>` (`?task=<id>`) | Every task row and card | See below | The next `dev task` command |

A task's quick look, wherever the task appears (Home, an initiative or project page, Tasks, Team, search, the
palette):

- **Header.** The title and one line with status, bucket, priority, type, a due-date pill, and version.
- **Lock state.** A lock with a countdown ring (the share of the lock's time that is left), who holds it, with which
  agent, and since when. An expired lock is amber and says that anyone can release it.
- **The next command.** The amber button names the next step (Start, Mark done) and copies its command, which is also
  shown in the dialog's body.
- **Description**, then **Other commands**: `dev task -p <id>|-i <slug> start|done|release|move|block|cancel|history
  <task-id>`, each on its own row with a copy icon, with `--why "…"` wherever the change must be explained. `-p` and
  `-i` work before or after the verb.
- **Details.** Id, project or initiative, assignee, created and updated (who and when), links, and the item file in
  the handoff repo.
- **History.** Every version, newest first: version, operation, detail, who (user@host and agent), when, and why.

Checkbox tasks from notes that have no work items yet are shown read-only, with their `dev task` number. The next
`dev sync` imports them.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/dashboard/drawer-1440-dark.webp">
  <img src="images/dashboard/drawer-1440-light.webp" alt="A task's quick look over a project page: the lock with its countdown ring (claimed by you, with the agent), the next command, the other commands with copy icons, the details, and an amber Mark done button in the footer." width="100%">
</picture>

### Live runs and time

Agents report progress on long runs (`dev progress start|step|block|done`). Summary views draw each running session
as a thin bar (steps done of the total; red when blocked). The session's quick look and the project page show:

- the run's goal, a thin progress bar, steps done of the total, elapsed time against the estimate (the calibrated one
  when known), and the time left;
- **Latest**, **Check**, and **Next**, each once: from the run, and from the saved status block when the run does not
  say. **Blocked** runs say so in red, with the blocking reason;
- _N subagents running_, and how long the current turn has been running. That is flagged when it is longer than 90%
  of turns here.

**Time** (Health, and on a project page behind _How long work takes here_) shows how predictable the work is:

- the median and p90 turn length over 30 days;
- **actual / estimate**, the median ratio over finished runs that had an estimate; new estimates are scaled by it;
- the slowest commands of the last 7 days, with credentials masked;
- subagent durations, and recent runs with their actual time against the estimate.

### The status block

Agents start every final message with the status block: a `**[Firefly> DevKit]** <project> · <branch> · <task>`
header followed by **Done**, **Check** (a `✓`, `⚠`, `✗`, or `–` glyph, then the evidence), **Progress**, and **Next** (see
[Concepts › The status block](concepts.md#the-status-block)). devkit saves it as one line,
`<project> · <branch> ❯ <task> ❯ <done> ❯ <✓ check> [❯ ▸ <progress>] ❯ next → <next>`.

The dashboard parses saved lines in Python (`status_parts`) and draws them as a quiet list: the task, then labeled
Done, Check (colored by its glyph), Progress, and Next rows. The agent and the time sit under them. Tables and lists
use a compact one-line form (the check glyph, what was done, and the next step). Lines saved in older one-line forms, including the legacy
`DevKit ·` marker, keep rendering as written, with the marker removed.

## The action list

It is ordered by what blocks you most. Home shows the top three groups (items of the same kind are one row) and
opens each in a quick look, where the command is the amber button. **Needs you** (`#/actions`) lists them all. It has
a filter per kind and a copy icon per item.

| Order | Item | Command |
| --- | --- | --- |
| 1 | One of **your** PRs is failing CI | `gh pr checks <n> -R <owner/repo> --watch` |
| 2 | Changes requested on your PR | `gh pr view <n> -R <repo> --comments` |
| 3 | Your PR has merge conflicts | `cd <its worktree> && git fetch origin && git rebase origin/<default>` (or `dcd … && gh pr checkout …`) |
| 4 | Your PR waits for review · someone else's PR needs a review | `gh pr view <n> -R <repo> --web` · `gh pr diff <n> -R <repo>` |
| 5 | An expired lock is still held · two sessions on the same branch | `dev task -p <id> release <task-id> --why "lock expired"` · `dev team` |
| 6 | A project or initiative is blocked · a task you hold is blocked | `dev handoff <id>` · `dev initiative show <slug>` · `dev task -p <id> show <task-id>` |
| 7 | Overdue tasks and initiatives | `dev task -p <id> done <task-id>` · `dev task -i <slug> done <task-id>` · `dev initiative show <slug>` |
| 8 | Uncommitted or unpushed work left idle | `cd <checkout> && git status -sb` |
| 9 | Finished worktrees (PR merged or closed, clean) | `dev wt rm <branch>`, or `dev gc --apply` for more than one |
| 10 | A repo outside the layout · a handoff note behind the code | `dev adopt <path>` · `dev handoff <id>` |
| 11 | Configuration drift · a provider not signed in | `dev config diff` · `claude` / `codex login` |

## Progress, health, people

- **Progress is honest.** It is the share of work items done, `done / (done + open)`, unless the note sets
  `progress:` manually. The number sits inside the progress ring; hover or focus the ring to see where it comes from
  (_4 of 8 tasks done_ or _Set manually in the note_), quoting counts only when they produce the percentage. With no
  tasks and no manual value the ring shows a dash, never a guess.
- **State.** A project is _off track_ (red) when one of its PRs fails CI or has changes requested, it is blocked, a
  task is overdue, or it is past its due date. It is _at risk_ (yellow) when paused, or for merge conflicts, idle
  uncommitted or unpushed work, or a checkout outside the layout. An initiative is off track when blocked or past its
  due date, and at risk when paused, due within a week, or holding blocked tasks. _Done_ is grey.
- **People** are `user@host`. This Mac's own sessions come from the local registry, which is fresher than its
  presence file. Presence older than 24 hours is ignored.
- **Locks** appear wherever tasks appear, as a lock icon with who holds it and the time left (_bob, 3 h left_); in
  quick looks the lock has a countdown ring. Yours read _you_; an expired lock is amber.

## Search, filters, and the command palette

- **Search** (`/`) covers initiatives, projects, people, tasks (by id or title), PRs, notes, plans, session logs and
  every Markdown file in the handoff repo. Results have type facets and highlighted matches. In Initiatives,
  Projects, Tasks and Activity it filters the view in place.
- **Filters**: the project facet row (health, CI, status, priority, owner, stack, tag, live session, open PRs, not
  cloned, outside initiatives) sits at the top of Projects, Activity, and Search. Pages use segmented controls for their own
  filters.
- **Command palette** (`⌘K` / `Ctrl K`) jumps to any view, initiative, project, person, task, PR, or file with fuzzy
  matching, and copies every command.

## Keyboard

| Keys | Action |
| --- | --- |
| `/` | Search |
| `⌘K` · `Ctrl K` | Command palette |
| `g` then `o` `i` `p` `m` `t` | Home · Initiatives · Projects · Team · Tasks |
| `g` then `a` `e` `h` | Activity · Explorer · Health |
| `u` | Up: from a page back to its list |
| `←` `→` (`Home` `End`) | Switch tabs when the tab bar has focus · read the chart day by day when it has focus |
| `j` / `k` | Next / previous item in the current list or modal |
| `↵` | Open the focused item |
| `Esc` | Close the quick-look modal, palette or sheet; clear the search |
| `?` | Shortcut sheet |

## Routes and the URL hash

Every page, tab, open modal and filter lives in the URL, so any view can be bookmarked, shared with someone who has
the same handoff repo, or reached with the back button. The format is `#/<view>[/<id>][?<key>=<value>&…]`, with
defaults omitted. Ids keep their slashes and dots (`#/projects/gitlab.com/firefly/platform/api`). Links from earlier
versions (`#/portfolio`, `?p=<project>`, `?i=<initiative>`) still resolve.

| Key | Values | Example |
| --- | --- | --- |
| view (path) | `overview` `initiatives` `projects` `team` `tasks` `activity` `explorer` `health` `search` `actions` | `#/team` |
| id (path) | an initiative slug · a project id | `#/initiatives/lending` · `#/projects/acme/api` |
| `tab` | initiative: `projects` `tasks` `plans` `sessions` `docs` · project: `tasks` `prs` `worktrees` `plans` `sessions` `note` | `#/projects/acme/api?tab=prs` |
| `task` | a task id: opens the task's quick look | `#/projects/acme/api?tab=tasks&task=T-fwk6cesh07u` |
| `modal` | `<kind>:<id>` opens a quick look: `initiative` `project` `person` `session` `action` `actions` `since` | `#/overview?modal=initiative:lending` |
| `mq` · `mg` | member search · grouping (`family` `status` `owner` `ws` `none`) | `#/initiatives/jafly?tab=projects&mg=status` |
| `tst` · `src` | task status (`open` `doing` `blocked` `todo` `done` `all`) · initiative task source (`all` `own` `projects`) | `#/initiatives/lending?tab=tasks&tst=blocked` |
| `q` | search text | `#/search?q=export` |
| `health` `ci` `status` `priority` `owner` `stack` `tag` | comma-separated values | `#/projects?ci=failing` |
| `live` `prs` `nc` `loose` | `1` (live session · open PRs · not cloned · outside initiatives) | `#/projects?loose=1` |
| `sort` `group` `layout` `dense` | Projects: `attention` `activity` `priority` `tasks` `name` `size` · `none` `initiative` `owner` `status` `priority` · `cards` (tiles, the default) `table` · `1` | `#/projects?layout=table` |
| `ist` `psort` `ilayout` | Initiatives: status · `priority` `due` `progress` `activity` · `cards` (the default) `table` | `#/initiatives?ilayout=table` |
| `tp` `who` `tmode` | Tasks: scope · claimed by (`me` or `user@host`) · `board` `list` | `#/tasks?who=me&tst=doing` |
| `kind` | Needs-you kind filter | `#/actions?kind=CI` |
| `f` · `fq` | Explorer file · find-in-file text | `#/explorer?f=initiatives/lending/README.md` |
| `range` | `7` or `30` (Activity) | `#/activity?range=30` |
| `types` | search result types | `#/search?q=api&types=initiative,task` |

## Where it comes from

| | |
| --- | --- |
| File | `~/.local/state/devkit/dashboard/index.html`: one self-contained HTML file (inline CSS, JS and SVG) |
| Regenerated | by `dev dashboard`, after every `dev sync` (session ends, debounced turn ends) and by the hourly background job |
| Refresh | an open tab reloads every 2 minutes while visible (not while a modal is open), keeping its view, filters, and scroll position |
| Data | The registry (projects, git state, worktrees, PRs, initiatives, live sessions with progress, providers, drift). From the handoff repo: **work items** (`items/<scope>/<id>.md`, with lock and changelog), **presence** (`presence/<user>@<host>.json`), **session logs** (`sessions/<scope>/<YYYY-MM>.md`), **versioned plans** (`plans/<scope>/*.md`), notes, and initiative READMEs. Also the PR cache, the instruction canary, guard decisions, the operations log, **timing records**, and **active runs** |

The decisions are computed in Python (`lib/devkit/dashboard.py`) and unit-tested (`tests/test_dashboard.py`):

- action priority and commands, project health, _since yesterday_, guard rule grouping, task numbering;
- lock state, collisions, people, initiative-linked items and KPIs;
- session-log and status-line parsing, plan status, timing summaries, and credential masking.

The page only renders them.

## Design

The page follows the Firefly Software brand and is visual first: each view answers one question in its first screen,
a number or a shape stands in for a sentence, text is a label, and the explanation lives in a quick-look modal.

- **Identity.** The official Firefly logo (the paper version on dark, the charcoal one on light) next to the devkit
  spark; charcoal and paper surfaces; Manrope.
- **Amber**, the firefly's light, appears once per screen at most: on the button for the most important next action.
  Progress rings, bars and sparklines are neutral (ink on light, stone on dark; today in the darker step).
- **Data colors.** Status uses one fixed palette in both themes: green `#0ca30c` on track, yellow `#fab219` at risk,
  red `#d03b3b` off track, grey for done. The set was checked with a colorblind-safety validator (adjacent pairs pass
  for the common color-vision deficiencies) and always comes with a legend, a label or a tooltip, never color alone.
  The heatmap uses one warm-grey ramp, light to dark. Text is never in a data color.
- **Visuals.** Inline SVG only, no chart library: progress rings with the number inside, thin segmented bars with a
  legend, member-project waffles, 14-day sparklines, CI strips, a calendar heatmap, lock countdown rings, and due-date
  pills. Every visual has an accessible name and a tooltip with the exact value on hover, and on focus where it can be
  focused; tables carry the same numbers.
- **Type.** Four sizes (13, 15, 18, and 28 px) and two weights (400 and 600), sentence case everywhere. Monospace is
  only for commands and paths.
- **Components.** One set used everywhere: stat tiles, cards and tiles on a soft surface (never nested), sections (a
  heading, a count, one quiet link), rows divided by hairlines whose title is a real link, quiet tables, avatars with
  live dots, session bars, underlined tabs, breadcrumbs, quiet buttons, copy icons, command menus, and one modal.
  Icons are drawn SVG in one stroke. Sticky table headers (a table wider than the page scrolls inside itself), empty states that
  say what to do next, and instant rendering with no skeletons.
- **Layout.** A full-height page: the header, a main area that grows, and the footer at the bottom of the window on
  short pages (after the content on long ones). A 12-column grid up to 1600 px puts panels side by side, and one
  spacing scale (4, 8, 12, 16, 24, 32, 48, 64 px). Home fits a 1440 × 900 screen without scrolling. Responsive at
  1440, 1280, 1100, 768, and 390 px with no horizontal page scroll; copy icons on rows appear on hover and focus, and
  always on phones and tablets. Keyboard-first with visible focus rings; with reduced motion the modal fades without
  moving; prints cleanly. The theme toggle (the half-filled circle, or the palette) is remembered, and _Theme: follow
  system_ resets it.

<img src="images/dashboard/home-390-dark.webp" alt="The Home view on a 390-pixel-wide phone screen in the dark theme: the stat tiles sit two by two, then Needs you, Team now with session bars, the Since yesterday counters, and the initiative cards." width="320">

## Privacy and security

The file stays on your Mac; the only network request is the Manrope web font (the page falls back to the system sans
offline). All text from notes, items, presence, logs, PR titles and git is inserted with DOM text APIs, never as HTML.
Links are limited to `http(s)`, `mailto` and in-page anchors, and the embedded data escapes `<`, `>` and `&`. Command
lines in timing records are masked for tokens, passwords and long secrets before they reach the page. The tests
render hostile content (`<img onerror>`, a `</script>` breakout) through every surface to keep it that way.

See also [Concepts](concepts.md), [Commands](commands.md), [Teams and machines](teams.md), and [Agents](agents.md).
