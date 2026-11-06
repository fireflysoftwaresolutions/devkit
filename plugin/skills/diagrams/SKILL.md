---
name: diagrams
description: Draw architecture, flow, sequence, state, C4, ER and timeline diagrams as branded, accessible SVG (dark and light) from a small JSON spec, validated for overlaps, crossings, label collisions, contrast and type size. Use whenever a README, doc, ADR, PR description or design needs a diagram, when asked to draw or redraw one, or to replace Mermaid/ASCII diagrams with real ones.
---

# Diagrams

devkit draws diagrams from a JSON spec with `dev diagram`: Firefly brand (charcoal/paper, amber as the one
highlight, Manrope), a light and a dark SVG every time, and a validator that refuses what a careful designer would
refuse. The full spec format per kind is in `reference/kinds.md`.

## Decide first

Draw only when a picture shows a **mechanism** faster than prose: where data flows, which parts talk, what changes
between two options, what states something moves through, what happens in what order. If one sentence says it, write
the sentence. Show the parts the point depends on — the boundary crossed, the hop added, the data that moves — and
leave the rest out. Comparing options? Draw the difference (the edge one option adds or removes), not two lists.

| You need to show | Kind |
|---|---|
| Components and how they connect; an architecture; a pipeline | `flow` |
| Software at C4 levels (person, system, container, component, external) | `c4` |
| Who calls whom, in what order, over time | `sequence` |
| The life of one thing (states and the events that move it) | `state` |
| Tables, keys and cardinalities | `er` |
| Dated milestones | `timeline` |

## Workflow

1. `dev diagram new <kind> <name> --dir docs/diagrams` — scaffold a spec (or copy one from `reference/kinds.md`).
2. Write the spec: nodes on a grid (`col`, `row`, fractions allowed), edges with **labels**, groups for boundaries,
   `title` (the kicker) and `desc` (one or two sentences for people who cannot see it — required).
3. `dev diagram render docs/diagrams/<name>.json --png` — writes `docs/images/diagrams/<name>-dark.svg` and
   `-light.svg` (+ PNG previews next to them; do not commit PNGs).
4. `dev diagram check docs/diagrams/<name>.json` — must print nothing but `ok`. Fix the layout, never silence it.
5. **Look at the PNG preview** (light and dark). Fix anything that reads awkwardly even if the validator is happy:
   uneven gaps, a label far from its edge, a long detour, an arrow pointing against the reading direction.
6. Embed with both themes:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/NAME-dark.svg">
  <img src="images/diagrams/NAME-light.svg" alt="The same claim as desc, in one or two sentences." width="100%">
</picture>
```

Commit the spec and both SVGs together; re-render whenever the spec changes (tests compare them).

## Quality rules

**Layout**
- Choose one reading direction: left → right for flows and pipelines, top → bottom for layers and hierarchies.
  Arrows go with it; the few that go back (failure loops, responses) are dashed.
- Use the grid. Same-kind nodes share a row or column; leave the default `equal_widths` on (or `"all"` for a calm,
  uniform diagram). Let the auto grid size columns and rows; set `grid` only for special cases.
- Width budget for docs and READMEs: **≤ ~1200 px** so 16 px labels stay ≥ 12 px in GitHub's column. Go vertical
  (layered) before going wide. Slides may be wider.
- Balance whitespace: no node crammed against another, no empty quadrant. Remove nodes before shrinking anything.

**Edges and arrows**
- Every edge has a verb or a payload as its label (`writes`, `JSON/HTTPS`, `on merge`). An unlabeled arrow means
  "related somehow" — remove it or label it.
- Orthogonal routing with rounded bends is automatic. Help it: put connected nodes in the same row or column so the
  edge is straight; use `from_side`/`to_side`, `route` (`hv`, `vh`, `straight`) or `via` only to remove a detour.
- Styles carry meaning, consistently: `data` (default) · `api` · `dashed` (async, optional, failure/fix loops) ·
  `return` (responses in sequences) · `event` (amber dashed, events) · `primary` (the one path the diagram is about).
- Connect to a whole `group` (use its id) instead of fanning arrows out from each member when the group acts as one.
- Never route through a node, a label or a group title; never let labels collide (the validator enforces this).

**Colour and emphasis**
- Colour means something: `neutral` (default), `firefly` (devkit/Firefly-owned), `steel` (third party, external),
  `slate` (data, generated, storage), `olive` (control, automation, metadata), `note`.
- **Amber is punctuation, never decoration: one focal element per diagram** — either one `emphasis: true` node or one
  `primary` path. The validator counts them.
- Add a `legend` only when an encoding (family or edge style) repeats and is not self-explanatory.

**Type and text**
- Labels are nouns (2–4 words); `sub` lines are short qualifiers. Two lines at most each; type never below 12 px —
  boxes grow, type never shrinks. Sentences belong in the caption or the surrounding prose.
- Name things exactly as the reader knows them (command names, file paths, product names); no customer names or
  invented metrics.

**Accessibility and files**
- `title` and `desc` are required; the `alt` of the `<img>` repeats the claim of `desc`.
- Text contrast ≥ 4.5:1, meaningful marks ≥ 3:1, in both themes (checked).
- Files: specs in `docs/diagrams/<name>.json`, output `docs/images/diagrams/<name>-{dark,light}.svg`, kebab-case
  names. SVGs are self-contained (no scripts, no external references).

## Validator codes

| Code | Fix |
|---|---|
| `overlap` | Move one node to another row/column, or remove it; do not shrink text |
| `overflow` | Shorten the label, drop the fixed `width`, or let it wrap |
| `crossing` | Put endpoints in line, change `from_side`/`to_side`, add a `via`, or move the obstacle |
| `label` | Increase the gap (auto grid does this for labels it sees), shorten the label, or set `label_at` |
| `contrast` / `type` | Use the family colours; never custom colours for text |
| `amber` | Keep one emphasised node or one primary path |
| `bounds` / `a11y` | Keep content inside the canvas; add `title` and `desc` |
