# Dashboard

`dev dashboard` opens the command centre: one page that answers four questions at a glance — **what needs me
now, where everything stands, what the agents are doing, and what changed** — and lets you act on each answer with
one copied command. It is a view over the handoff repository, so it is as current as the last `dev sync`.

```bash
dev dashboard            # regenerate and open in your browser
dev dashboard --no-open  # regenerate only
dev dashboard --path     # print the file path
```

## Views

| View | Keys | The job it does |
|---|---|---|
| **Home** | `g o` | *Today.* Up to four actionable tiles (PRs failing CI, tasks in progress, live agent sessions, worktrees and how many are reclaimable), each a link to the filtered list. **What needs me now**: one prioritised action list with the command that moves each item forward. **Now**: the tasks in progress across projects, grouped by project and ordered by health and priority. **Where everything stands**: every initiative with its progress, milestone and due date, and a one-line summary of the projects outside initiatives. **Agents right now**: live sessions (agent, project @ branch, how long they have been running, their latest `DevKit ·` status line) and provider chips (signed in, plan, quota). **What changed since yesterday**: sessions, commits, merged PRs and completed tasks. |
| **Portfolio** | `g i` | *Everything we carry.* A milestones-and-due-dates roadmap across initiatives and projects (today line, overdue in red), then every initiative — status and priority, milestone, due date, progress bar, member projects as chips with their own status and progress, Now tasks, latest agent status — then the projects outside any initiative with their progress, milestone and due date. Group by initiative, owner or status; sort by priority, due date, progress or activity; filter by status, priority, owner, stack, tag or *not cloned*. |
| **Projects** | `g p` | A dense table (cards optional): health dot (hover for the reasons), status and priority, progress, what is in progress, the latest agent status line, PRs with a CI dot each (green passing, red failing, amber running, grey none; ⚠ merge conflicts), 7-day activity, last activity and size. Sorted by *needs attention* by default. |
| **Tasks** | `g t` | A kanban (Now · Next · Later · Done) or a *by project* list of every task from the handoff notes. Each card copies `dev task done <n> -p <id>` (Now) or `dev task start <n> -p <id>` (Next, Later). |
| **Activity** | `g a` | Timeline first — agent sessions with their status lines, merged PRs, operations (cleanups, adoptions, configuration repairs) and canary misses — over 7 or 30 days; then a compact commits-per-day chart (with a table view) and the most active projects. |
| **Explorer** | `g e` | The handoff repository as a tree — Workspace files, Initiatives (README, source, docs), projects by owner (note and plans), the activity log — with a Markdown viewer and find-in-file (`↵` / `⇧↵` step through matches). Up to 2 MB is embedded, newest first; the viewer says when older files were left out. |
| **Health** | `g h` | Providers (sign-in, account, plan, version, quota with its reset time), guard decisions grouped by rule with counts (a rule that keeps firing means an agent is fighting the workspace rules), the instruction canary per agent with its deviations, configuration drift, every worktree, and disk usage per project. |

### The action list

Ordered by what blocks you most. Each item has its command; the most important one is the single amber button on
the page.

| Order | Item | Command |
|---|---|---|
| 1 | One of **your** PRs is failing CI | `gh pr checks <n> -R <owner/repo> --watch` |
| 2 | Changes requested on your PR | `gh pr view <n> -R <repo> --comments` |
| 3 | Your PR has merge conflicts | `cd <its worktree> && git fetch origin && git rebase origin/<default>` (or `dcd … && gh pr checkout …`) |
| 4 | Your PR waits for review · someone else's PR needs a review | `gh pr view <n> -R <repo> --web` · `gh pr diff <n> -R <repo>` |
| 5 | A project or initiative is blocked | `dev handoff <id>` · `dev initiative show <slug>` |
| 6 | Overdue tasks and initiatives | `dev task done <n> -p <id>` · `dev task done <n> -i <slug>` · `dev initiative show <slug>` |
| 7 | Uncommitted or unpushed work left idle | `cd <checkout> && git status -sb` |
| 8 | Finished worktrees (PR merged or closed, clean) | `dev wt rm <branch>`, or `dev gc --apply` for several |
| 9 | A repo outside the layout · a handoff note behind the code | `dev adopt <path>` · `dev handoff <id>` |
| 10 | Configuration drift · a provider not signed in | `dev config diff` · `claude` / `codex login` |

### Progress is honest

Progress bars show the computed share of tasks done — `done / (done + open)` from the note's Tasks section — unless
the note sets `progress:` manually (`progress_source: manual`), which the bar marks with ✎. Hover any bar to see
which one it is. A project or initiative with no tasks and no manual value shows *no tasks*, never a guessed number.

### Health

A project is **at risk** (red) when one of its PRs fails CI or has changes requested, it is marked blocked, a task is
overdue or it is past its due date; it **needs a look** (amber) for merge conflicts, uncommitted or unpushed work idle
for more than two days, unsaved work in a worktree, or a checkout outside the layout; otherwise it is **healthy**.
Projects that have a handoff note but are not cloned on this Mac are listed everywhere with a *not cloned* tag and a
`dev clone <id>` command.

## Drawers

Clicking a project or an initiative opens a drawer — a working surface, not a summary:

- **Project**: health reasons, initiatives, progress, launch commands (`dev claude`, `dev codex`, `dcd`, `dev handoff`,
  or `dev clone` when it is not cloned here), the latest status line, the rendered handoff note (purpose, current
  state, decisions, links, with a link to the full note), tasks by bucket with their commands, pull requests with CI,
  review, merge state, size and a copyable command each, worktrees (with `dev wt rm`), plans, the session log and the
  repository facts.
- **Initiative**: progress and where it comes from, goal, scope and current state, one workstream per member project
  (status, progress, role, Now tasks, latest status line, `dev clone` when needed; click through to the project),
  initiative tasks with `-i` commands, risks, decisions and links, the source archives and documents, and the session
  log.

## Search, filters and the command palette

- **Search** (`/`) covers initiatives, projects, PRs, tasks, notes, plans, sessions and every Markdown file in the
  handoff repo, with result-type facets and highlighted matches. In Portfolio, Projects, Tasks and Activity it filters
  the view in place.
- **Filters** sit in one row above the content they scope: health, CI, status, priority, owner/host, stack, tag,
  live session, open PRs, not cloned.
- **Command palette** (`⌘K` / `Ctrl K`) jumps to any view, initiative, project, PR, task or file with fuzzy matching
  (several words match independently) and copies every command from the action list and the per-project commands.

## Keyboard

| Keys | Action |
|---|---|
| `/` | Search |
| `⌘K` · `Ctrl K` | Command palette |
| `g` then `o` `i` `p` `t` `a` `e` `h` | Home · Portfolio · Projects · Tasks · Activity · Explorer · Health |
| `j` / `k` | Next / previous item in the current list or drawer |
| `↵` | Open the focused item |
| `Esc` | Close the drawer, palette or sheet; clear the search |
| `←` `→` | Read the activity chart day by day when it has focus |
| `?` | Shortcut sheet |

## Shareable state: the URL hash

Every view, filter and open drawer lives in the URL hash, so a view can be bookmarked, shared with someone who has
the same handoff repo, or reached with the back button. Format: `#/<view>?<key>=<value>&…` (defaults omitted).

| Key | Values | Example |
|---|---|---|
| view (path) | `overview` `portfolio` `projects` `tasks` `activity` `explorer` `health` `search` | `#/portfolio` |
| `q` | search text | `#/search?q=export` |
| `health` `ci` `status` `priority` `owner` `stack` `tag` | comma-separated values | `#/projects?ci=failing` |
| `live` `prs` `nc` | `1` (live session · open PRs · not cloned) | `#/portfolio?nc=1` |
| `sort` `group` `layout` `dense` | Projects: `attention` `activity` `priority` `tasks` `name` `size` · `none` `owner` `status` `priority` · `table` `cards` · `1` | `#/projects?layout=cards` |
| `pgroup` `psort` | Portfolio: `initiative` `owner` `status` · `priority` `due` `progress` `activity` | `#/portfolio?psort=due` |
| `p` · `i` | open the project · initiative drawer | `#/portfolio?i=lending-os` |
| `f` · `fq` | Explorer file · find-in-file text | `#/explorer?f=initiatives/lending-os/README.md` |
| `range` | `7` or `30` (Activity) | `#/activity?range=30` |
| `tp` · `tmode` | Tasks project filter · `board` or `list` | `#/tasks?tmode=list` |
| `types` | search result types | `#/search?q=api&types=initiative,pr` |

## Where it comes from

| | |
|---|---|
| File | `~/.local/state/devkit/dashboard/index.html` — one self-contained HTML file (inline CSS, JS and SVG) |
| Regenerated | by `dev dashboard`, after every `dev sync` (session ends, debounced turn ends) and by the hourly background job |
| Refresh | an open tab reloads every 2 minutes while visible, keeping its view, filters, drawer and scroll position |
| Data | the registry (projects, git state, worktrees, PRs with CI and review, initiatives, live sessions, providers, drift), the handoff notes and initiative READMEs, the PR cache (merged PRs), the instruction canary, guard decisions from `hooks.log`, the operations log and the Markdown files of the handoff repo |

Decisions — action priority and commands, project health, *since yesterday*, guard rule grouping, task numbering —
are computed in Python (`lib/devkit/dashboard.py`) and unit-tested (`tests/test_dashboard.py`); the page only renders
them.

## Design

Firefly Software brand: the official Firefly logo (generated by the brand-kit, never redrawn; the paper colourway on
the dark theme, charcoal on the light one) next to the devkit spark in the navigation bar; charcoal and paper surfaces;
Manrope following the brand type scale; amber — the firefly's light — for the single most important action on each
view, the mark and data marks only. Chart and progress marks use amber-family steps validated for contrast on each
surface (`#c38530` dark, `#9d671f` light); status uses fixed status colours always paired with an icon or a label.
Spacing follows an 8 px scale; there is a dense mode for big portfolios. Responsive at 1440, 1024, 768 and 390 px;
keyboard-first with visible focus rings; respects reduced motion; prints cleanly. The theme toggle (◐ or the palette)
is remembered; *Theme: follow system* resets it.

## Privacy and security

The file stays on your Mac; the only network request is the Manrope web font (the page falls back to the system sans
offline). All text from notes, files, PR titles and git is inserted with DOM text APIs, never as HTML; links are
limited to `http(s)`, `mailto` and in-page anchors; the embedded data escapes `<`, `>` and `&`. The tests render
hostile content (`<img onerror>`, a `</script>` breakout) through every surface to keep it that way.

See also: [Concepts](concepts.md) · [Commands](commands.md) · [Agents](agents.md) · [Delivery](delivery.md)
