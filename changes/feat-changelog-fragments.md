### Added

- Change fragments: in a repository with a `changes/` folder, each branch writes its CHANGELOG lines in
  `changes/<branch>.md` (its name with `/` as `-`) instead of editing `CHANGELOG.md`, so parallel branches no longer
  collide on it. `dev release` on a `release/`, `hotfix/` or `integration/` branch writes every fragment into that
  version's section of `CHANGELOG.md`, in Keep a Changelog order, and removes the files, and refuses a version that
  is already tagged; on the default branch it refuses while a fragment is left. `dev ci --run` and
  `dev ci --mark-passed` fail a branch that edits `CHANGELOG.md` outside a release branch, and a fragment or file in
  `changes/` they cannot use; the error names its file and line number. devkit takes its own CHANGELOG lines from
  fragments from this release on.
