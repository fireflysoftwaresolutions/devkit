---
name: functional-analysis
description: "Turn a request into a functional analysis that design, build, and tests share. Use when a request is a new feature, changes behavior users see, or is unclear, before design or code; when writing requirements, use cases or user stories, business rules, edge cases, or acceptance criteria (Given/When/Then); when an acceptance criterion needs a test that proves it; and when reviewing whether a feature's criteria are all tested. It is the analysis the Business analyst (The Oracle) produces."
---

# Functional analysis

Build the right thing before you build it right. A functional analysis turns a request into numbered rules and
acceptance criteria that can be tested, so the Software architect, the builders, and the QA engineer work from the
same list, and every criterion ends up with a test that proves it.

## The procedure

1. **Read first.** The code that owns the behavior, the docs, the handoff note, and the specs that exist
   (`dev plan ls`). Ask nothing the material already answers.
2. **Find the question behind the request.** What problem does it solve, for whom, and how do they know it is
   solved? The solution in the request is one option, not the requirement.
3. **Write the spec** with `dev plan new "<feature>" --kind spec`, section by section (the outline below).
4. **Walk the edge-case checklist** and turn each relevant row into a rule or a criterion.
5. **Turn open questions into decisions** (below), then revise the spec with the answers.
6. **Hand off** to the members who design, draw, and test it (below).

## The spec outline

The spec template's sections, and what goes in each:

| Section | What goes in it |
| --- | --- |
| Summary | The feature, who it is for, and the outcome, in one paragraph |
| Problem | The question behind the request, with evidence: what users cannot do today and what it costs them |
| Users and scenarios | The actors and their goals; a use case or user story per goal, with its main, alternative, and exception flows |
| Requirements | Business rules `R1`, `R2`, …, each testable, with its priority (must, should, could) |
| Out of scope | The non-goals, said plainly, so nobody builds them |
| User experience | Flows and states (empty, loading, error, success), copy, accessibility; links to the designs |
| API and data | The data and its states (a table of states and the events that move them); the contract and schema changes |
| Acceptance criteria | Given/When/Then criteria; each one has an id and names the rule it proves |
| Metrics | Non-functional needs with numbers, and how the team can tell it works after release |
| Risks and open questions | Each open question with its decision id once asked |

Add a **Glossary** section for the domain's terms (the `domain-modeling` skill), or update the project's glossary
when it keeps one. Use the same word for the same thing in the spec, the code, and the tests.

A use case, with its flows:

```markdown
### UC-1 Export a month of invoices
Actor: finance clerk · Goal: get a month's invoices into a spreadsheet · Trigger: month-end close
Main flow: 1. The clerk picks a month that has ended. 2. The system streams a CSV file. 3. The download starts.
Alternative: 1a. The month has no invoices: the system says so and creates no file.
Exception: 2x. The invoice store does not answer within 10 s: the export stops with a retry message.
```

## Business rules

One rule per line, numbered, with an outcome someone can observe:

| Untestable | Testable |
| --- | --- |
| Exports should be fast | `R4`: an export of up to 10,000 invoices finishes in under 5 s in 95 percent of exports |
| Handle invalid months | `R2`: a month that has not ended is refused with "Choose a month that has ended" |

## The edge-case checklist

Go through every row. Each one ends as a rule, a criterion, or "does not apply" with the reason.

| Edge | Ask |
| --- | --- |
| Empty | No data; empty input; a blank or missing field |
| One | Exactly one item |
| Many | The largest realistic volume; pagination; the time it takes |
| Limits | Zero, negative, minimum, maximum, maximum plus one; length limits; rounding and currency |
| Text | Unicode and emoji; right-to-left scripts; values at 10 times the usual length; spaces before or after a value |
| Time | Time zones, daylight saving changes, end of month, leap years, midnight, clock skew |
| Permissions | Each role; no permission; another tenant's data; access revoked during the flow |
| Concurrency | Two users at once; a double submit; a retry after a timeout (idempotency) |
| States | Each state's allowed transitions; a deleted, archived, or locked record |
| Dependencies | Each dependency slow, down, wrong, or partly done: one row per dependency |
| Existing data | Old records without the new field, duplicates, the migration of what is stored |
| Access and language | Keyboard only; screen readers; other languages; the formats of dates and numbers |

## Non-functional needs, with numbers

Performance (a percentile and a load), volume, availability, security and personal data, accessibility (WCAG 2.2
AA unless the project says otherwise), compatibility (browsers, API versions), and what is logged or measured.
"Fast," "secure," and "scalable" are not requirements until they have a number or a named standard.

## Acceptance criteria

Each criterion has an id, names the rule it proves, and uses concrete values:

```gherkin
AC-3 (R2) Refuse a month that has not ended
  Given a finance clerk on the invoice list on 2026-11-08
  When they export the month 2026-12
  Then the export is refused with "Choose a month that has ended"
  And no file is created
```

- **One behavior per criterion,** with an outcome someone can observe: a value, a message, a stored state, an event.
- **Ids are `AC-<n>`,** unique in the spec. When the repository keeps more than one spec, prefix the spec's short key
  (`EXPORT-AC-3`). Never reuse or renumber an id: a dropped criterion keeps its id, marked dropped.
- **Every rule has at least one criterion,** and every criterion names its rule.

## Traceability: every criterion has a test

The criterion's id appears in the name or the docstring of the test that proves it, so anyone can find the test
from the spec and the spec from the test:

```python
def test_ac3_refuses_a_month_that_has_not_ended():
    """AC-3: a month that has not ended is refused, and no file is created."""
```

In frameworks with text names, put the exact id in the name: `it("AC-3: refuses a month that has not ended")`,
`@DisplayName("AC-3: refuses a month that has not ended")`. `dev grep -F "AC-3" tests/` finds the test.

- The **QA engineer (Mouse)** maps each criterion to its test and lists every criterion without one.
- The **Code reviewer (Seraph)** refuses a feature whose criteria have no test.

## Open questions become decisions

Ask the developer only what the material cannot answer. Ask one decision per question, and give its options and
your recommendation:

```bash
dev crew ask --decision "Skip empty lines or reject the file?" --options "A: skip | B: reject" --recommend A
```

Record the answer in the spec and version it: `dev plan revise <file> --why "<decision>: <answer>"`. A guess
written as a rule becomes a defect that the tests then protect.

## Hand off

The Business analyst (The Oracle) does `requirements` work and the Product owner (Councillor Hamann) approves it.
The hand-off names the spec's path and who takes it next:

| To | For | From the spec |
| --- | --- | --- |
| Software architect (The Architect) | Design: components, contracts, an ADR | Rules, data and states, non-functional needs |
| UX designer (Sati) | Screens and flows | Use cases, states, edge cases, copy |
| QA engineer (Mouse) | The test plan and the criterion-to-test map | Acceptance criteria and their ids |
| Tech lead (Morpheus) | Routing the build steps, and the approval | The spec, and the decisions still open |

## Done when

- Every rule is numbered and testable, and every row of the edge-case checklist is answered.
- Every criterion has an id, the rule it proves, and concrete values in Given/When/Then.
- Open questions were asked as decisions, not guessed, and the spec is revised with the answers.
- The hand-off names the Software architect, the UX designer, the QA engineer, and the tech lead.
