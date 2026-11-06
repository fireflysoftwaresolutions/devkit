#!/usr/bin/env bash
# devkit bootstrap: makes sure Homebrew, Python and the GitHub CLI are present and signed in,
# then hands over to the setup wizard (`dev setup`). Safe to re-run.
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
#   gh repo clone fireflysoftwaresolutions/devkit ~/Development/fireflysoftwaresolutions/devkit
#   ~/Development/fireflysoftwaresolutions/devkit/install.sh            # interactive wizard
#   ~/Development/fireflysoftwaresolutions/devkit/install.sh --yes      # accept every default
#   ~/Development/fireflysoftwaresolutions/devkit/install.sh --skip-packages
#
set -euo pipefail

DEVKIT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
step() { printf '\n\033[1;38;2;255;179;74m✦\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }

[[ "$(uname -s)" == "Darwin" ]] || { echo "devkit supports macOS only"; exit 1; }

step "Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  # Homebrew's official installer (https://brew.sh): the documented way to install it on a new Mac.
  # nosemgrep: bash.curl.security.curl-pipe-bash.curl-pipe-bash
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
[[ -x /opt/homebrew/bin/brew ]] && eval "$(/opt/homebrew/bin/brew shellenv)"
ok "$(brew --version | head -1)"

step "Python and GitHub CLI"
command -v /opt/homebrew/bin/python3 >/dev/null 2>&1 || brew install python
command -v gh >/dev/null 2>&1 || brew install gh
ok "$(/opt/homebrew/bin/python3 --version) · $(gh --version | head -1)"
if ! gh auth status >/dev/null 2>&1; then
  gh auth login -h github.com -s repo,read:org,workflow,gist
fi
ok "$(gh api user --jq '"GitHub: \(.login)"')"

step "devkit setup wizard"
exec /opt/homebrew/bin/python3 "$DEVKIT_HOME/bin/dev" setup "$@"
