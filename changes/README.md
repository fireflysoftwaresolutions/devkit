# Change fragments

This folder holds the CHANGELOG lines of the branches that have not been released yet. A branch never edits
`CHANGELOG.md` itself: it adds one file here, and the release writes every file into the new version's section of
`CHANGELOG.md` and removes it.

## Write your fragment

Name the file after your branch, with `/` as `-`: `feat/invoice-export` writes `changes/feat-invoice-export.md`. Put
each line under its Keep a Changelog heading (`### Added`, `### Changed`, `### Deprecated`, `### Removed`,
`### Fixed`, or `### Security`), start each entry with a `-` bullet, and indent its next lines by two spaces:

```markdown
### Added

- The invoice list exports a month as CSV.

### Fixed

- The login page keeps the last user for 30 days, as
  the settings page says.
```

Write each entry for a reader of the release notes: what changed for them and why, in plain words.

Branch names that differ only in `/` and `-` (`feat/a` and `feat-a`) share one file, so give one of them another
name; and once a release has written your fragment and removed it, put later lines in a new file such as
`changes/feat-a-2.md`, because a branch that edits the removed file conflicts when it is merged.

## What checks it

- `dev ci --run` and `dev ci --mark-passed` fail a branch that changes `CHANGELOG.md` when it is not a `release/`,
  `hotfix/` or `integration/` branch. They also fail a fragment they cannot read or that is not UTF-8, a symbolic
  link here, and a file here that is no `.md` fragment, and name its file and line.
- `dev release` on the release branch writes the fragments into `CHANGELOG.md` and removes them; an
  `integration/<topic>` or `hotfix/<topic>` branch names the version with `--version`. On the default branch it
  refuses while a fragment is left.

Keep this README here: it is no fragment, and it keeps the folder, which turns the checks on, after a release.

See also [Delivery › Change fragments](../docs/delivery.md#change-fragments).
