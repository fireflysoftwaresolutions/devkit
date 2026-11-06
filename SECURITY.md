# Security policy

devkit runs inside developers' agent sessions. Its hooks see every tool call, its guard decides what an agent may
run, and its installer writes the agents' settings, hooks, and instructions. A vulnerability in devkit can reach every
machine that installs it, so report it privately, as described here.

## Supported versions

| Version | Supported |
| --- | --- |
| The latest release (see [Releases](https://github.com/fireflysoftwaresolutions/devkit/releases)) | Yes |
| Older releases | No: update with `dev update` |

Fixes ship as a new patch version (CalVer `YY.MM.PATCH`), with notes in the [CHANGELOG](CHANGELOG.md).

## Report a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a
vulnerability**, or go straight to
[the new advisory form](https://github.com/fireflysoftwaresolutions/devkit/security/advisories/new). Only you and the
maintainers see the report.

Do not report a vulnerability in a public issue, pull request, or discussion.

Include what helps the maintainers reproduce it:

- the devkit version (`dev version`) and your macOS version;
- the agent and the surface, such as Claude Code in the terminal or in Claude Desktop's Code tab, Codex in the
  terminal or the ChatGPT app, or the Claude Desktop extension;
- the steps, the impact you see, and a proof of concept if you have one.

Redact secrets before you send logs or transcripts: devkit's logs can contain command lines and file paths.

## What happens next

1. The maintainers confirm in the advisory that they received the report, and keep you updated there.
2. They reproduce the issue and fix it in the advisory's private fork.
3. They release the fix as a patch version, publish the advisory, and request a CVE when the issue qualifies.
4. The advisory credits you, unless you prefer not to be named.

## Scope

In scope:

- the `dev` CLI and everything under `lib/` and `bin/`;
- the hooks, including the guard: a way to make an agent run what the guard should refuse is a vulnerability;
- the installer (`install.sh`, `dev setup`, `dev install`) and the settings, hooks, and instructions it writes;
- the MCP server (`dev mcp`) and the Claude Desktop extension;
- the dashboard, a generated HTML file: data it shows without escaping is in scope;
- the CI/CD templates that `dev ci init` writes.

Out of scope:

- third-party skills, plugins, and tools that devkit installs: report those upstream ([NOTICE](NOTICE) lists their
  sources);
- issues in the agents themselves (Claude Code, Codex, Claude Desktop, the ChatGPT app): report those to their
  vendors;
- attacks that need an already compromised machine or account.

## How this repository is protected

- Secret scanning and push protection are on.
- CI runs a security job on every pull request: gitleaks, Semgrep, osv-scanner, Trivy, and zizmor.
- GitHub Actions are pinned by commit SHA and container images by digest, with least-privilege tokens.
- Dependabot keeps the pinned actions current.
- devkit bundles no third-party code.

See also [Guard rules](docs/guard.md) and the [contributing guide](CONTRIBUTING.md).
