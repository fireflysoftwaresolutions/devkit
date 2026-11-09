---
name: research
description: "Research a technology from current, version-matched sources and record what you found. Use when a decision depends on how a library, framework, API, service, or tool behaves: choosing, adding, or upgrading a technology; using an API this repository has not used at its pinned version; behavior that the docs, the code, and your memory disagree on; an architecture decision; or a spike. Also use before relying on a spike note, to check whether it is still current."
---

# Research from current, version-matched sources

A statement about a technology is true for one version on one date. Research pins both, reads primary sources
first, labels everything else, and records the result as a spike note, so the next session reuses it instead of
researching again or answering from memory.

Your own memory of an API is a secondary source of unknown date and version. Use it to know where to look, never as
the answer.

## The procedure

1. **State the question and the decision it serves.** One question ("Can the standard library write TOML?") and
   the decision it unblocks ("add a dependency or not"). When choosing between options, write the criteria first:
   fit, maturity, license, security, cost, lock-in.
2. **Look for what the team already knows.** `dev plan ls` lists the plans and spikes of the project (`-i <slug>`
   for an initiative); search their text with `dev grep -i "<technology>" ~/Development/.handoff/plans/<project id>/`.
   Then `dev lesson --search "<technology>"`. A spike note that is still current (below) answers the question:
   cite it.
3. **Pin the version before you read anything.** Take it from the lockfile, not from a range in the manifest:

   | Stack | The version that runs |
   | --- | --- |
   | Node | `pnpm-lock.yaml`, `package-lock.json`, or `yarn.lock`; installed: `node_modules/<pkg>/package.json` |
   | Python | `uv.lock` or `poetry.lock`; installed: `uv pip show <pkg>` |
   | Java and Kotlin | `./mvnw dependency:tree` or `./gradlew dependencies` (the BOM decides most versions) |
   | PHP, Go, Rust, Ruby | `composer.lock`, `go.mod`, `Cargo.lock`, `Gemfile.lock` |
   | Runtimes and tools | `.tool-versions`, `.nvmrc`, `.python-version`, the `Dockerfile` base image, the CI setup steps |

4. **Read primary sources, in order** (the table below). Stop when a source answers the question for the pinned
   version.
5. **Use secondary sources only when labeled as such:** give each one its date and the version it describes.
6. **Put a version, a link, and a date on every claim** (the claim format below), and keep a "could not verify"
   list. An empty list is a claim too: say that everything was verified.
7. **When sources disagree, run a spike.** Write the smallest program that shows the behavior, run it against the
   pinned version in your scratch folder (never in the repository), and quote its output. The output wins over any
   document.
8. **Record the result** as a spike note (below), so `dev grep` finds it by technology next time.

## Source order

| Order | Source | Where | Why it ranks here |
| --- | --- | --- | --- |
| 1 | The installed package: its docs, types, and changelog | `node_modules/<pkg>/` (`README.md`, `CHANGELOG.md`, `*.d.ts`), the Python package's source and docstrings, `vendor/<pkg>/`, the sources jar | It is exactly the code that runs |
| 2 | The vendor's tooling for that version | Framework MCP servers (the Angular CLI MCP's `search_documentation`, the Next.js MCP), Laravel Boost, Symfony AI Mate, the tool's own `--help` | The vendor answers for the installed version |
| 3 | The official docs for that version | The versioned docs site (its version selector or `/v<major>/` path) and API reference | Primary, once you checked the version it shows |
| 4 | Release notes and migration guides | The project's releases, `CHANGELOG.md`, upgrade guides from the pinned version to the target | They say what changed, and in which version |
| 5 | The source and its tests | The source at the tag of the pinned version, and its tests of the behavior | The ground truth when the docs are silent |
| 6 | Issues and advisories | The issue tracker (closed issues too), GitHub security advisories, OSV | Known bugs and workarounds; check which versions each one affects |
| Secondary | Blog posts, Q&A answers, tutorials, AI answers, your memory | Anywhere | Often written for another version: label, date, and verify them |

A documentation server that sends your queries to a third party is used only when the developer installed it, and
never with code, secrets, or customer data in the query. devkit removed `context7` for that reason.

## The claim format

Every claim in your report, the spike note, or an ADR carries five fields:

```markdown
| Claim | Version | Source | Checked | Kind |
| --- | --- | --- | --- | --- |
| `tomllib` reads TOML but cannot write it | Python 3.11 | https://docs.python.org/3.11/library/tomllib.html | 2026-11-08 | primary |

Could not verify:
- <the claim, and what you tried>
```

- **Claim:** one sentence someone can test.
- **Version:** the exact version the claim holds for, as pinned.
- **Source:** a link, or a path in the installed package.
- **Checked:** the date you read it.
- **Kind:** `primary` (sources 1 to 6) or `secondary`.

## The spike note

Record the result with `dev plan new "<technology>: <question>" --kind spike`. Fill the template's sections
(Question, Timebox, Options, Method, Findings, Recommendation with its confidence) and put this header block at the
top of the body, under the title; devkit manages the front matter before it:

```markdown
- **technology:** Python `tomllib`
- **version:** 3.11 (`.python-version`)
- **checked:** 2026-11-08
- **sources:** https://docs.python.org/3.11/library/tomllib.html (primary)
```

The claims go under Findings in the claim format, with the "could not verify" list. After each change, run
`dev plan revise <file> --why "…"`.

## When a note needs a re-check

A spike note is current while both hold:

- its `checked` date is less than 90 days old, and
- its `version` has the same major version the lockfile pins now (for a `0.x` package, the same minor version).

Otherwise, re-check it before you rely on it: read the sources again for the pinned version, update the claims and
`checked`, and run `dev plan revise <file> --why "re-checked for <version>"`. A claim that no longer holds is
corrected, not deleted, so the history shows what changed.

## Common mistakes

| Mistake | Instead |
| --- | --- |
| Reading the latest docs for an older pinned version | Pin the version first, then open the docs for that version |
| Taking `^4.2.0` in `package.json` as the version | Read the lockfile or the installed package |
| Answering from memory | Memory points to a source; the source answers |
| Citing a dated blog post as fact | Label it secondary, with its date, and verify it in a primary source |
| Research that ends with the session | A spike note with the header block, findable by `dev grep` |

## Hand off

The Technical researcher (Roland) does `research` work and the Software architect (The Architect) approves it.
Hand over the spike note's path, the recommendation with its confidence, and the risks. An architecture decision
cites the spike in its ADR.

## Done when

- The question, the decision it serves, and the pinned version (with the file it came from) are written.
- Every claim has a version, a source, a date, and its kind; the "could not verify" list exists.
- The result is a spike note with the header block, and the report names its path.
