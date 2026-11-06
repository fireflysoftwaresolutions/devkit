---
name: docs
description: Write and maintain project documentation to the devkit standard — README anatomy with a branded hero, Diátaxis structure (tutorials, how-to guides, reference, explanation), Google developer documentation style, SVG diagrams, CHANGELOG with CalVer, and `dev docs check` (markdownlint, link and anchor checks, Vale) until clean. Use when creating or updating a README, a docs/ folder, a guide, a reference page, a runbook, a CHANGELOG or release notes, or when the user asks to document something or to make docs look professional.
---

# Documentation to the devkit standard

Documentation is part of the change: update it in the same branch as the code it describes.

## 1. Decide what you are writing (Diátaxis)

| Reader needs | Type | Shape |
|---|---|---|
| To learn by doing | Tutorial | One path, every step works, no choices, ends in a visible result |
| To get a task done | How-to guide | Goal in the title, numbered steps, prerequisites, expected result |
| Exact facts | Reference | Complete, consistent tables and entries; generated where possible |
| To understand why | Explanation | Concepts, trade-offs, diagrams; no step lists |

Never mix types on one page; link between them. The `documentation-writer` skill applies Diátaxis in depth;
`doc-coauthoring` runs a structured workflow for specs, decision documents and proposals.

## 2. README anatomy

1. Hero: `<picture>` with dark and light images (`docs/images/hero-{dark,light}.*`), meaningful alt text.
2. `# Name` and one paragraph: what it is, who it is for, why it exists.
3. A navigation line of anchor links to the main sections.
4. **Why** (problem → answer, a table works well) · **Install** (copy-paste, fewer than five commands) ·
   **Quick start / everyday use** · **How it works** (one SVG diagram, see the `diagrams` skill) · **What's inside**
   (paths table) · **Documentation** (guides table) · **Credits and licence**.
5. Firefly projects use the brand (palette, Manrope, the spark) for images; never redraw or type the Firefly logo.

## 3. docs/ folder

- `docs/README.md` index, one line per guide; guides named by reader task (`getting-started.md`, `configuration.md`).
- Every page: one `#` title, an opening sentence that says what the page is for, sections in reader order, a
  closing "See also" line.
- Images in `docs/images/`, diagrams as SVG specs in `docs/diagrams/` rendered to `docs/images/diagrams/`
  (`dev diagram render`), always dark and light, always alt text.

## 4. Style (Google developer documentation style)

- Second person, present tense, active voice, imperative for steps ("Run `dev doctor`").
- American spelling; Oxford comma; em dashes without spaces; sentence-case headings; no "simply", "just", "easy".
- Code, commands, paths, keys and identifiers in backticks; code blocks always declare a language.
- Tables for comparisons and options; numbered lists only for sequences.
- Link text says where it goes ("see [Configuration](configuration.md)"), never "click here".
- Keep facts in one place and link to them; generated reference (CLI help, config keys) must match the code.

## 5. CHANGELOG and releases

Keep a Changelog sections (`Added`, `Changed`, `Fixed`, `Removed`, `Security`) per version, newest first. Versions
follow the project's scheme, otherwise CalVer `YY.MM.PATCH` (`dev calver`). Release notes = the CHANGELOG entry.

## 6. Check before you finish

```bash
dev docs check            # markdownlint (structure), lychee (links + anchors), Vale (prose), SVG diagrams
dev docs check --fix      # apply markdownlint's safe fixes first
dev docs check --online   # also verify external links (before a release)
```

Fix every error; treat Vale warnings as review comments and fix them unless the wording is deliberate (product
names, quoted UI text). Then read the rendered page (GitHub preview or the dashboard Explorer) once, as a newcomer.
