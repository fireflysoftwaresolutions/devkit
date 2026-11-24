### Added

- Change fragments: in a repository with a `changes/` folder, each branch writes its CHANGELOG lines in
  `changes/<branch>.md` (its name with `/` as `-`) instead of editing `CHANGELOG.md`, so parallel branches no longer
  collide on it. `dev release` on a `release/`, `hotfix/` or `integration/` branch writes every fragment into that
  version's section of `CHANGELOG.md`, in Keep a Changelog order, and removes the files, and refuses a version that
  is already tagged; on the default branch it refuses while a fragment is left. `dev ci --run` and
  `dev ci --mark-passed` fail a branch that edits `CHANGELOG.md` outside a release branch, and a fragment or file in
  `changes/` they cannot use; the error names its file and line number. devkit takes its own CHANGELOG lines from
  fragments from this release on.

### Fixed

- `dev release` refuses a version whose tag the repository wrote without the `v` (`26.11.4`), as it already refused
  one tagged `v26.11.4`, so a repository whose tag format has no `v` cannot release a shipped version twice.
- `dev release` creates, pushes, and names its GitHub release with the tag in the project's format: `26.11.4` where
  the delivery policy's tag format, or the repository's own version tags, have no `v`, and `v26.11.4` otherwise. It
  tagged `v26.11.4` everywhere, so the release workflow that `dev ci init` generates for a repository without the
  `v` never started. `dev calver` and devkit's CI watch name the tag the same way.
