#!/usr/bin/env bash
# devkit bootstrap: toolchain + configuration. Safe to re-run.
# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
#
#   gh repo clone ancongui/devkit ~/Development/ancongui/devkit && ~/Development/ancongui/devkit/install.sh
#
set -euo pipefail

DEVKIT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;36m▸\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

[[ "$(uname -s)" == "Darwin" ]] || { echo "devkit supports macOS only"; exit 1; }

SKIP_PACKAGES=0
for a in "$@"; do [[ "$a" == "--skip-packages" ]] && SKIP_PACKAGES=1; done

step "Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
ok "$(brew --version | head -1)"

if [[ $SKIP_PACKAGES == 0 ]]; then
  step "Toolchain (Brewfile)"
  brew bundle --file="$DEVKIT_HOME/Brewfile" --no-upgrade
  ok "brew bundle complete"

  step "Global npm packages"
  grep -vE '^\s*(#|$)' "$DEVKIT_HOME/config/npm-globals.txt" | xargs npm install -g --no-fund --no-audit
  ok "$(grep -vcE '^\s*(#|$)' "$DEVKIT_HOME/config/npm-globals.txt") packages"
  if command -v playwright-cli >/dev/null 2>&1; then
    playwright-cli install --skills >/dev/null 2>&1 && ok "playwright-cli skills installed" || warn "playwright-cli skills not installed"
  fi
fi

step "Claude Code"
if ! command -v claude >/dev/null 2>&1; then
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "$(claude --version 2>/dev/null || echo 'installed — run `claude` once to sign in')"

step "GitHub CLI"
if ! gh auth status >/dev/null 2>&1; then
  gh auth login -h github.com -s repo,read:org,workflow,gist
fi
ok "$(gh api user --jq '"signed in as \(.login)"')"

step "devkit configuration"
/usr/bin/env python3 "$DEVKIT_HOME/bin/dev" install

bold ""
bold "Next: open a new terminal (or run: exec zsh), then: dev doctor"
