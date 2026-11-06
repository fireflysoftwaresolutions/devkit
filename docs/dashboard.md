# Dashboard

`dev dashboard` opens the project command centre: one page that shows where every project stands, what is in
progress, what the agents did last and what needs attention. It is a view over the handoff repository, so it is
always as current as the last `dev sync`.

```bash
dev dashboard            # regenerate and open in your browser
dev dashboard --no-open  # regenerate only
dev dashboard --path     # print the file path
```

## Where it comes from

| | |
|---|---|
| File | `~/.local/state/devkit/dashboard/index.html` — one self-contained HTML file (inline CSS, JS and SVG) |
| Regenerated | by `dev dashboard`, after every `dev sync` (session end, debounced turn ends) and by the hourly background job |
| Refresh | an open tab reloads itself every 2 minutes while visible, keeping its view, filters and scroll position |
| Data | the registry (`registry.json`: projects, git state, worktrees, PRs, activity, latest `DevKit ·` status lines), the handoff notes (status, priority, tasks, sessions, plans), the instruction canary, guard decisions from `hooks.log`, the operations log (`log/`) and the Markdown files of the handoff repo for the Explorer |
| Explorer cap | up to 2 MB of Markdown is embedded, newest files first; the Explorer says when older files were left out |

Nothing leaves the machine. The only network request is the Manrope web font from Google Fonts; offline, the page
falls back to the system sans and looks the same otherwise.

## Views

| View | Shortcut | What it answers |
|---|---|---|
| **Overview** | `g o` | Where do we stand? KPIs (active projects, tasks in progress, open PRs, live sessions, worktrees, commits in 7 days vs the previous 7, canary score), what needs attention, commits per day, the latest agent status lines, the Now board and recent activity. |
| **Projects** | `g p` | Every project as cards or a dense table: status and priority badges, stack, branch and git state, latest agent status, Now tasks and task progress, last session, 30-day activity sparkline, PRs, worktrees and size. Group by owner, status or priority; sort by activity, priority, attention, open tasks, name or size; a dense mode for big portfolios. |
| **Tasks** | `g t` | A kanban of every task across projects — Now, Next, Later, Done — from the Tasks section of each handoff note, filterable by project, with due and overdue markers. |
| **Activity** | `g a` | Commits per day for the last 7 or 30 days, the most active projects, and a timeline of sessions, operations (cleanups, adoptions, configuration repairs) and canary misses. |
| **Explorer** | `g e` | The handoff repository as a tree — Workspace files, then owner → project → note and plans, then the activity log — with a Markdown viewer (headings, lists, checkboxes, code, tables, links) and find-in-file with match highlighting (`↵` / `⇧↵` to step through matches). |
| **Health** | `g h` | Instruction canary per agent with the list of deviations, recent guard decisions, configuration drift (when the registry carries a `dev config diff` report), every worktree with its PR and session state, reclaimable items and disk usage per project. |

Clicking a project anywhere opens its **detail drawer**: purpose, current state, all tasks by bucket, decisions,
links, plans, repository facts, open PRs (linked to GitHub), worktrees, the session log, and copy buttons for
`dcd <id>`, `dev claude <id>`, `dev codex <id>`, `dev task add "…" --now -p <id>`, `dev handoff <id>` and the `cd`
path.

## Search, filters and the command palette

- **Global search** (`/`) looks through projects, tasks, notes, plans, sessions and every Markdown file in the
  handoff repo, ranks title matches first and highlights matches. Result-type facets narrow it to one kind. In
  Projects, Tasks and Activity the search filters the view in place instead.
- **Filters** apply to everything below them: status, priority, owner/host, stack, tag, has a live session, has
  open PRs, needs attention. Active filters show as chips; *Clear filters* resets them.
- **Command palette** (`⌘K` / `Ctrl K`) jumps to any view, project, file or task with fuzzy matching (several words
  match independently) and runs actions: copy `dcd`, `dev claude`, `dev codex`, `dev handoff` and `dev task add`
  commands for a project, switch theme, density or layout, clear filters, print.

## Keyboard

| Keys | Action |
|---|---|
| `/` | Search |
| `⌘K` · `Ctrl K` | Command palette |
| `g` then `o` `p` `t` `a` `e` `h` | Overview · Projects · Tasks · Activity · Explorer · Health |
| `j` / `k` | Next / previous item in the current list (or in the open drawer) |
| `↵` | Open the focused item |
| `Esc` | Close the drawer, palette or sheet; clear the search |
| `←` `→` | Read the activity chart day by day when it has focus |
| `?` | Shortcut sheet |

## Shareable state: the URL hash

Every view, filter and selection lives in the URL hash, so a view can be bookmarked, shared with someone who has
the same handoff repo, or reached with the back button. Format:

```
#/<view>?<key>=<value>&…
```

| Key | Values | Example |
|---|---|---|
| view (path) | `overview` `projects` `tasks` `activity` `explorer` `health` `search` | `#/tasks` |
| `q` | search text | `#/search?q=payments` |
| `status` `priority` `owner` `stack` `tag` | comma-separated values | `#/projects?status=active,blocked` |
| `live` `prs` `attn` | `1` | `#/projects?attn=1` |
| `sort` | `activity` `priority` `attention` `tasks` `name` `size` | `#/projects?sort=priority` |
| `group` | `none` `owner` `status` `priority` | `#/projects?group=owner` |
| `layout` / `dense` | `cards` `table` / `1` | `#/projects?layout=table&dense=1` |
| `p` | project id (opens the drawer) | `#/projects?p=acme/widget` |
| `f` / `fq` | Explorer file / find-in-file text | `#/explorer?f=projects/acme/widget.md&fq=TODO` |
| `range` | `7` or `30` | `#/activity?range=7` |
| `types` | search result types | `#/search?q=export&types=task,plan` |
| `tp` | Tasks view project filter | `#/tasks?tp=acme/widget` |

Defaults are omitted. The last state is also remembered in the browser's local storage, so opening the file without
a hash returns to where you were.

## Design

Firefly Software brand: charcoal (dark theme, the default when your system is dark) and paper (light theme)
surfaces, Manrope type, and amber — the firefly's light — used sparingly for the mark, the full stop and the one
accent. Chart marks use amber-family steps validated for contrast and lightness on each surface (`#c38530` dark,
`#9d671f` light); status uses fixed status colours always paired with an icon and a label. The theme toggle (◐ or the
palette) is remembered; *Theme: follow system* resets it. The layout is tuned for a 1440 px screen, works down to phone
width, respects reduced-motion settings and prints cleanly (`⌘P` or the palette's *Print this view*).

## Security

All text from notes, files and git is inserted with DOM text APIs, never as HTML; links are limited to `http(s)`,
`mailto` and in-page anchors, and relative links to handoff files open in the Explorer. The embedded data escapes
`<`, `>` and `&`. The tests (`tests/test_dashboard.py`) check this with hostile note content.

See also: [Concepts](concepts.md) · [Commands](commands.md) · [Configuration](configuration.md) · [Agents](agents.md)
