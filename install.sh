#!/usr/bin/env bash
# devkit installer. devkit is for Macs with Apple silicon: Homebrew, which it needs, no longer installs on Intel
# Macs. An Intel Mac that already has Homebrew can still run it.
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# On a new Mac (gets the newest release into ~/Development/fireflysoftwaresolutions/devkit):
#   curl -fsSL https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh | bash -s -- --yes
# From a clone of devkit:
#   ./install.sh [--yes] [--skip-packages] [--no-setup]
#
# It makes sure Homebrew, git, Python, and the GitHub CLI are present, gets devkit, signs you in to GitHub, and
# starts the setup wizard (`dev setup`), whose `dev install` pins a read-only copy of devkit for the hooks and the
# guard in ~/.local/share/devkit/releases. Safe to run again. `install.sh --help` lists the options.
#
# Everything is inside functions, and only the last line runs them: a download cut short runs nothing.

usage() {
  cat <<EOF
devkit installer. devkit is for Macs with Apple silicon.

It makes sure Homebrew, git, Python, and the GitHub CLI are present, gets devkit (the newest
release), signs you in to GitHub, and starts the setup wizard. Safe to run again.

An Intel Mac can run it only if it already has Homebrew: Homebrew no longer installs on Intel.

Usage:
  curl -fsSL $INSTALL_URL | bash
  curl -fsSL $INSTALL_URL | bash -s -- [options]
  ./install.sh [options]                       (from a clone of devkit)

Options:
  -y, --yes          accept every default in the setup wizard
  --skip-packages    configure only; do not install the toolchain packages for your stacks
  --no-setup         get devkit and its prerequisites, but do not sign in or run the setup wizard
  -h, --help         show this help
  Other options are passed to the setup wizard (dev setup).

Environment:
  DEVKIT_HOME   where devkit goes (default: ~/Development/fireflysoftwaresolutions/devkit)
  DEVKIT_REPO   the repository to clone (default: $DEFAULT_REPO)
  DEVKIT_REF    a tag or branch to check out instead of the newest release
  GH_TOKEN      a GitHub token for installs without a terminal (read by the GitHub CLI)

Without a terminal (CI, scripts), Homebrew installs non-interactively, the GitHub sign-in is
skipped unless the GitHub CLI is already signed in, and the wizard takes every default.
EOF
}

# --------------------------------------------------------------------------- output

colors() {
  C_B="" C_DIM="" C_ACC="" C_OK="" C_WARN="" C_BAD="" C_OFF=""
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
    C_B=$'\033[1m' C_DIM=$'\033[2m' C_ACC=$'\033[1;38;2;255;179;74m' C_OK=$'\033[32m' C_WARN=$'\033[33m'
    C_BAD=$'\033[31m' C_OFF=$'\033[0m'
  fi
}

# A step: its name, then one plain sentence on what it does and why.
step() {
  STEP="$1"
  printf '\n%s✦%s %s%s%s\n' "$C_ACC" "$C_OFF" "$C_B" "$1" "$C_OFF"
  if [ -n "${2:-}" ]; then printf '  %s%s%s\n' "$C_DIM" "$2" "$C_OFF"; fi
}
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
note() { printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
fail() { printf '  %s✗%s %s\n' "$C_BAD" "$C_OFF" "$*" >&2; }
die()  { fail "$*"; exit 1; }
tildify() { case "$1" in "$HOME"/*) printf '~%s\n' "${1#"$HOME"}" ;; *) printf '%s\n' "$1" ;; esac; }

on_error() {
  local rc=$?
  # A failure inside $(…) or a pipeline also fails the command that ran it: report it once, from there.
  [ "${BASH_SUBSHELL:-0}" -eq 0 ] || exit "$rc"
  trap - ERR
  printf '\n' >&2
  fail "The step \"$STEP\" failed (exit code $rc). Nothing after it ran."
  printf '    Fix the problem shown above, then run the installer again (it is safe to repeat):\n      %s\n\n' \
    "$RETRY" >&2
  exit "$rc"
}

# Run a command that may ask questions: from the terminal, also when this script arrives through a pipe; with
# no input at all when there is no terminal.
with_tty() {
  case "$TTY" in
    stdin) "$@" ;;
    "") "$@" </dev/null ;;
    *) "$@" <"$TTY" ;;
  esac
}

# Prints the terminal's own device (/dev/ttys003), or fails when there is no terminal. Never /dev/tty, its alias:
# macOS's kqueue refuses a descriptor opened from /dev/tty (EINVAL), so Claude Code, whose runtime watches its input
# with kqueue, crashed at sign-in when it got /dev/tty.
terminal_device() {
  local fd d
  for fd in 0 1 2; do
    if [ -t "$fd" ]; then
      d="$(tty <&"$fd" 2>/dev/null || true)"
      case "$d" in /dev/tty) ;; /dev/*) if [ -c "$d" ]; then printf '%s\n' "$d"; return 0; fi ;; esac
    fi
  done
  d="$(ps -o tty= -p $$ 2>/dev/null | tr -d ' ' || true)"
  case "$d" in
    tty?*) if [ -c "/dev/$d" ] && (exec <"/dev/$d") 2>/dev/null; then printf '/dev/%s\n' "$d"; return 0; fi ;;
  esac
  return 1
}

# --------------------------------------------------------------------------- releases

# Reads tag names and prints the newest release tag (vYY.MM.PATCH), compared as numbers: v26.11.10 is newer
# than v26.11.2. Prints nothing when no tag is a release.
newest_release() {
  awk -F. '/^v[0-9]+\.[0-9]+\.[0-9]+$/ {
    a = substr($1, 2) + 0; b = $2 + 0; c = $3 + 0
    if (!n || a > A || (a == A && (b > B || (b == B && c > C)))) { A = a; B = b; C = c; T = $0; n = 1 }
  } END { if (n) print T }'
}

# Where the checkout is: its release tag, its branch, or its commit.
describe() {
  local b t
  if b="$(git -C "$DEVKIT_HOME" symbolic-ref -q --short HEAD)"; then
    printf 'branch %s\n' "$b"
    return 0
  fi
  t="$(git -C "$DEVKIT_HOME" tag --points-at HEAD | newest_release)"
  if [ -n "$t" ]; then printf '%s\n' "$t"; else git -C "$DEVKIT_HOME" rev-parse --short HEAD; fi
}

# --------------------------------------------------------------------------- steps

check_mac() {
  step "This Mac" "devkit runs on macOS; this checks the system before anything changes."
  local os machine sysctl arch ver
  os="$(uname -s)"
  if [ "$os" != Darwin ]; then
    fail "This computer runs $os. devkit is for Macs with Apple silicon; nothing was changed."
    exit 1
  fi
  machine="$(uname -m)"
  # Under Rosetta, uname -m says x86_64 on Apple silicon too; hw.optional.arm64 is 1 on Apple silicon only, Rosetta
  # included. Such a terminal would run the Intel Homebrew in /usr/local, or none: it needs a native one. sysctl is in
  # /usr/sbin, which a PATH may leave out. A Terminal that still runs keeps Rosetta in each new window: quit it first.
  sysctl="$(command -v sysctl || true)"
  sysctl="${sysctl:-/usr/sbin/sysctl}"
  if [ "$machine" = x86_64 ] && [ "$("$sysctl" -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
    fail "This terminal runs under Rosetta, so this Mac with Apple silicon reports an x86_64 processor." \
      "Nothing was changed."
    {
      printf '    devkit and Homebrew need a native terminal:\n'
      printf '    1. Quit Terminal (Command-Q).\n'
      printf '    2. In Finder, select Terminal in Applications > Utilities, choose File > Get Info,\n'
      printf '       and clear "Open using Rosetta".\n'
      printf '    3. Open Terminal again and run the installer:\n      %s\n' "$RETRY"
      printf '    In a shell started with arch -x86_64, type exit instead, then run the installer again.\n'
      printf '    Another terminal app, such as iTerm2: see\n      %s\n' \
        "$DOCS_URL/troubleshooting.md#the-installer-stops-on-an-intel-mac-or-in-a-rosetta-terminal"
    } >&2
    exit 1
  fi
  # Homebrew's own installer stops on Intel Macs (Homebrew/install PR #1140): only a Homebrew already there can serve.
  if [ "$machine" != arm64 ] && ! find_brew >/dev/null; then
    fail "devkit needs a Mac with Apple silicon for a new install, because Homebrew no longer installs on Intel Macs." \
      "Nothing was changed."
    printf '    This Mac reports an Intel processor (%s) and has no Homebrew.\n' "$machine" >&2
    printf '    An Intel Mac that already has Homebrew can still run this installer.\n' >&2
    exit 1
  fi
  case "$machine" in arm64) arch="Apple silicon" ;; x86_64) arch="Intel" ;; *) arch="$machine" ;; esac
  ver="$(sw_vers -productVersion 2>/dev/null || true)"
  ok "macOS${ver:+ $ver} on $arch"
  if [ "$machine" != arm64 ]; then
    note "Homebrew no longer supports Intel Macs: devkit uses the Homebrew you have, but some tools may build slowly" \
      "from source."
  fi
}

# Prints the brew to use: the one on PATH, else Homebrew's brew for Apple silicon, else the one for Intel, by absolute
# path only. Fails when there is none.
find_brew() {
  local b
  if command -v brew >/dev/null 2>&1; then command -v brew; return 0; fi
  for b in "$BREW_ARM" "$BREW_INTEL"; do
    case "$b" in /*) ;; *) continue ;; esac
    if [ -x "$b" ]; then printf '%s\n' "$b"; return 0; fi
  done
  return 1
}

ensure_homebrew() {
  step "Homebrew" "Homebrew is the standard installer for developer tools on a Mac; devkit uses it for everything else."
  local brew_bin installer on_path=1 v
  command -v brew >/dev/null 2>&1 || on_path=0
  if ! brew_bin="$(find_brew)"; then
    note "Homebrew is not installed: running its official installer (https://brew.sh)."
    note "It asks for the password you use to log in to this Mac. Nothing appears while you type it; that is normal."
    [ -n "$TTY" ] || export NONINTERACTIVE=1
    installer="$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    with_tty /bin/bash -c "$installer"
    brew_bin="$(find_brew)" || die "Homebrew's installer finished, but brew is in neither /opt/homebrew nor /usr/local."
  fi
  eval "$("$brew_bin" shellenv)"
  BREW_PREFIX="$(brew --prefix)"
  [ "$on_path" = 1 ] || add_brew_to_zprofile "$brew_bin"
  v="$(brew --version)"
  ok "${v%%$'\n'*} in $BREW_PREFIX"
}

# The step Homebrew's installer asks you to do by hand, so that new terminals find brew and what it installs.
# Intel's brew is in /usr/local/bin, which is on the default PATH of macOS already.
add_brew_to_zprofile() {
  local profile="$HOME/.zprofile"
  if [ "$1" = "$BREW_INTEL" ]; then return 0; fi
  if [ -f "$profile" ] && grep -qF "brew shellenv" "$profile"; then return 0; fi
  # shellcheck disable=SC2016  # the line is written as is, for zsh to run in each new terminal
  printf '\neval "$(%s shellenv)"\n' "$1" >>"$profile"
  ok "new terminals find Homebrew too (added to ~/.zprofile)"
}

ensure_tools() {
  step "git, Python, and the GitHub CLI" \
    "git keeps the history of your code, Python runs devkit, and the GitHub CLI connects this Mac to GitHub."
  local v
  git --version >/dev/null 2>&1 || with_tty brew install git
  PY="$BREW_PREFIX/bin/python3"
  [ -x "$PY" ] || with_tty brew install python
  command -v gh >/dev/null 2>&1 || with_tty brew install gh
  ok "$(git --version)"
  ok "$("$PY" --version) ($(tildify "$PY"))"
  v="$(gh --version)"
  ok "${v%%$'\n'*}"
}

get_devkit() {
  step "devkit" "devkit itself: the newest release, in your Development folder."
  local ref where
  where="$(tildify "$DEVKIT_HOME")"
  if [ -e "$DEVKIT_HOME/.git" ]; then
    [ -f "$DEVKIT_HOME/bin/dev" ] || die "$where is a git checkout, but not of devkit. Choose another folder with DEVKIT_HOME."
    update_existing "$where"
    return 0
  fi
  if [ -e "$DEVKIT_HOME" ] && [ -n "$(ls -A "$DEVKIT_HOME")" ]; then
    die "$where exists and is not a git checkout, so it was left untouched. Move it away, or choose another folder with DEVKIT_HOME."
  fi
  ref="${DEVKIT_REF:-}"
  if [ -z "$ref" ]; then
    ref="$(git ls-remote --tags --refs "$DEVKIT_REPO" | sed 's#.*refs/tags/##' | newest_release)"
    [ -n "$ref" ] || note "$DEVKIT_REPO has no release tag yet: using its default branch"
  fi
  mkdir -p "$(dirname "$DEVKIT_HOME")"
  if [ -n "$ref" ]; then
    git clone --quiet --no-checkout "$DEVKIT_REPO" "$DEVKIT_HOME"
    check_out "$ref"
  else
    git clone --quiet "$DEVKIT_REPO" "$DEVKIT_HOME"
  fi
  ok "downloaded $(describe) to $where"
}

# Checks out a fresh clone at a release tag (detached, on the tag's commit) or at a branch, and removes the default
# branch the clone made. Not `git clone --branch <tag>`: for an annotated tag, which every release is, git prints
# "warning: refs/tags/… is not a commit!". The tag stays on HEAD, which dev install checks before it pins devkit.
check_out() {
  local ref="$1" made
  made="$(git -C "$DEVKIT_HOME" symbolic-ref -q --short HEAD || true)"
  if git -C "$DEVKIT_HOME" rev-parse -q --verify "refs/tags/$ref^{commit}" >/dev/null ||
    git -C "$DEVKIT_HOME" fetch --quiet origin "refs/tags/$ref:refs/tags/$ref" 2>/dev/null; then
    git -C "$DEVKIT_HOME" -c advice.detachedHead=false checkout --quiet --detach "refs/tags/$ref^{commit}"
  else
    git -C "$DEVKIT_HOME" checkout --quiet "$ref"
  fi
  if [ -n "$made" ] && [ "$(git -C "$DEVKIT_HOME" symbolic-ref -q --short HEAD || true)" != "$made" ]; then
    git -C "$DEVKIT_HOME" branch --quiet -D "$made"
  fi
}

# An existing checkout at a release moves to the newest release (or to DEVKIT_REF); anything else stays as it is.
update_existing() {
  local where="$1" branch current target
  if branch="$(git -C "$DEVKIT_HOME" symbolic-ref -q --short HEAD)"; then
    note "$where is on branch $branch: left as it is (dev update pulls the branch)"
    return 0
  fi
  if [ -n "$(git -C "$DEVKIT_HOME" status --porcelain --untracked-files=no)" ]; then
    note "$where has local changes: left as it is"
    return 0
  fi
  current="$(git -C "$DEVKIT_HOME" tag --points-at HEAD | newest_release)"
  if [ -z "$current" ] && [ -z "${DEVKIT_REF:-}" ]; then
    note "$where is at commit $(describe), not at a release: left as it is"
    return 0
  fi
  git -C "$DEVKIT_HOME" fetch --quiet --tags
  target="${DEVKIT_REF:-}"
  if [ -z "$target" ]; then
    target="$(git -C "$DEVKIT_HOME" tag --list 'v*' | newest_release)"
    if [ "$(printf '%s\n%s\n' "$current" "$target" | newest_release)" = "$current" ]; then
      ok "$where is at the newest release, $current"
      return 0
    fi
  fi
  git -C "$DEVKIT_HOME" -c advice.detachedHead=false checkout --quiet "$target"
  ok "$where moved from ${current:-its commit} to $(describe)"
}

use_checkout() {
  step "devkit" "devkit itself: the checkout this script is in."
  ok "this checkout: $(tildify "$DEVKIT_HOME") ($(describe))"
  if [ -z "$(git -C "$DEVKIT_HOME" tag --points-at HEAD 2>/dev/null | newest_release || true)" ]; then
    note "not a release: dev install pins it for the hooks only after its tests pass here (bin/dev ci --run)"
  fi
}

# Sets GITHUB=0 when the GitHub CLI is not signed in and there is no terminal to sign in from.
sign_in_github() {
  step "GitHub sign-in" \
    "devkit keeps your project notes in a private GitHub repository and saves your work under your name."
  local login
  GITHUB=1
  if ! gh auth status >/dev/null 2>&1; then
    if [ -z "$TTY" ]; then
      note "no terminal to sign in from: skipped"
      GITHUB=0
      return 0
    fi
    note "Press Enter at each question: your browser opens GitHub. Type the code shown here, then choose Authorize."
    with_tty gh auth login -h github.com -p https -w -s repo,read:org,workflow,gist
  fi
  login="$(gh api user --jq .login 2>/dev/null || true)"
  ok "signed in to GitHub${login:+ as $login}"
}

run_setup() {
  step "Setup wizard" \
    "A few questions about how you work. Press Enter to keep the recommended answer; nothing changes until you apply."
  local args=()
  args=(${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"})
  if [ -z "$TTY" ] && [ "$YES" = 0 ]; then
    note "no terminal for questions: the wizard takes every default (--yes)"
    args+=(--yes)
  fi
  SETUP_RC=0
  with_tty "$PY" "$DEVKIT_HOME/bin/dev" setup ${args[@]+"${args[@]}"} || SETUP_RC=$?
}

# The read-only copy of devkit that the wizard's `dev install` pinned for the hooks and the guard, from its record:
# "<version> (<commit>) in <folder>", or nothing when devkit is not pinned.
pinned_line() {
  "$PY" -c 'import json, os, sys
try:
    r = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    r = {}
p = r.get("path") or "" if isinstance(r, dict) else ""
if p and os.path.isfile(os.path.join(p, "bin", "dev")):
    home = os.path.expanduser("~")
    print("%s (%s) in %s" % (r.get("version"), (r.get("commit") or "")[:7], "~" + p[len(home):] if p.startswith(home + "/") else p))' \
    "$HOME/.local/share/devkit/install.json" 2>/dev/null || true
}

# The agents chosen in the wizard, from its saved configuration.
agents_line() {
  local names
  names="$("$PY" -c 'import json, sys
a = json.load(open(sys.argv[1])).get("agents") or {}
print(" and ".join(n for k, n in (("claude", "Claude Code"), ("codex", "Codex")) if a.get(k, True)))' \
    "${DEVKIT_CONFIG:-$HOME/.config/devkit/config.json}" 2>/dev/null || true)"
  printf '%s\n' "${names:-your coding agents}"
}

summary() {
  local outcome="$1" where version again pinned
  where="$(tildify "$DEVKIT_HOME")"
  version="$(cat "$DEVKIT_HOME/VERSION" 2>/dev/null || printf 'unknown')"
  again="$where/install.sh"
  step "Summary"
  printf '  devkit %s (%s) in %s\n' "$version" "$(describe)" "$where"
  case "$outcome" in
    ready)
      printf '\n  %sWhat you have now%s\n' "$C_B" "$C_OFF"
      ok "devkit, and the tools for the kinds of projects you chose"
      ok "$(agents_line) with devkit's shared rules and its safety guard"
      pinned="$(pinned_line)"
      if [ -n "$pinned" ]; then
        ok "the hooks and the guard run a read-only copy of devkit $pinned"
      else
        note "the hooks are not pinned yet: run dev install (dev doctor says what is missing)"
      fi
      ok "a private GitHub repository for your project notes, tasks, and plans"
      printf '\n  %sWhat to do first%s\n' "$C_B" "$C_OFF"
      printf '  1. Open a new Terminal window (or run: exec zsh), so the dev command works.\n'
      printf '  2. dev doctor               checks that everything works\n'
      printf '  3. dev dashboard            shows your projects and tasks in the browser\n'
      printf '  4. dev new my-app, then dcd my-app, then claude (or codex) to start your first project\n'
      printf '  %sLater, dev update moves devkit to the newest release and pins it.%s\n\n' "$C_DIM" "$C_OFF"
      ;;
    no-setup)
      note "devkit is downloaded; nothing is configured yet (--no-setup)."
      printf '\n  %sNext%s: %s   signs in to GitHub and starts the setup wizard\n\n' "$C_B" "$C_OFF" "$again"
      ;;
    no-github)
      note "Not finished: the GitHub CLI is not signed in, and there is no terminal to sign in from."
      printf '\n  %sTo finish%s, in a terminal:\n' "$C_B" "$C_OFF"
      printf '  1. gh auth login -h github.com -p https -w -s repo,read:org,workflow,gist   (or set GH_TOKEN)\n'
      printf '  2. %s\n\n' "$again"
      ;;
    *)
      note "The setup wizard stopped (exit code $SETUP_RC). Above, it lists the steps done and what is left, each with"
      printf '    the command that finishes it.\n'
      printf '\n  %sNext%s: fix what it shows, if anything, then continue where it stopped:\n' "$C_B" "$C_OFF"
      printf '    %s/bin/dev setup --resume   continues with the answers you gave\n' "$where"
      printf '  %sOr run %s again: it is safe to repeat.%s\n\n' "$C_DIM" "$again" "$C_OFF"
      ;;
  esac
}

# --------------------------------------------------------------------------- main

main() {
  set -eEuo pipefail
  INSTALL_URL="https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh"
  DEFAULT_REPO="https://github.com/fireflysoftwaresolutions/devkit.git"
  DOCS_URL="https://github.com/fireflysoftwaresolutions/devkit/blob/main/docs"
  # Where Homebrew installs brew, for when it is not on PATH: Apple silicon, then Intel.
  BREW_ARM=/opt/homebrew/bin/brew BREW_INTEL=/usr/local/bin/brew
  # Test hook only; not a supported setting. "<Apple silicon brew>:<Intel brew>" in a temporary folder, so that the
  # tests never reach this Mac's Homebrew. find_brew skips an entry that is not an absolute path.
  if [ -n "${DEVKIT_TEST_BREW_PATHS:-}" ]; then IFS=: read -r BREW_ARM BREW_INTEL <<<"$DEVKIT_TEST_BREW_PATHS"; fi
  SETUP_ARGS=() NO_SETUP=0 YES=0 STEP="start" BREW_PREFIX="" PY="" SETUP_RC=0 GITHUB=0
  local arg src here=""
  for arg in "$@"; do
    case "$arg" in
      -h | --help) usage; return 0 ;;
      --no-setup) NO_SETUP=1 ;;
      -y | --yes) YES=1; SETUP_ARGS+=("$arg") ;;
      *) SETUP_ARGS+=("$arg") ;;
    esac
  done
  colors
  printf '%sdevkit installer%s · devkit is for Macs with Apple silicon\n' "$C_B" "$C_OFF"

  # From a clone (./install.sh): set up that checkout. Through a pipe, or as a file on its own: get devkit first.
  src="${BASH_SOURCE[0]:-}"
  if [ -n "$src" ] && [ -f "$src" ]; then here="$(cd "$(dirname "$src")" && pwd)"; fi
  if [ -n "$here" ] && [ -f "$here/bin/dev" ] && [ -f "$here/VERSION" ]; then
    MODE=local DEVKIT_HOME="$here"
    RETRY="$here/install.sh${*:+ $*}"
  else
    MODE=bootstrap
    DEVKIT_HOME="${DEVKIT_HOME:-$HOME/Development/fireflysoftwaresolutions/devkit}"
    case "$DEVKIT_HOME" in /*) ;; *) DEVKIT_HOME="$PWD/$DEVKIT_HOME" ;; esac
    DEVKIT_REPO="${DEVKIT_REPO:-$DEFAULT_REPO}"
    RETRY="curl -fsSL $INSTALL_URL | bash${*:+ -s -- $*}"
  fi

  # Questions go to the terminal, also when this script arrives through a pipe; without a terminal, nothing asks.
  # Through a pipe, stdin is this script: every other command reads /dev/null instead, never what is left of it.
  if [ -t 0 ] && [ "$(tty 2>/dev/null || true)" != /dev/tty ]; then TTY=stdin
  elif TTY="$(terminal_device)"; then exec </dev/null
  else TTY="" && exec </dev/null
  fi
  # Homebrew asks before it installs ("Do you want to proceed? [y/n]") unless this is set: the switch it documents.
  export HOMEBREW_NO_ASK=1

  trap on_error ERR
  check_mac
  ensure_homebrew
  ensure_tools
  if [ "$MODE" = local ]; then use_checkout; else get_devkit; fi
  if [ "$NO_SETUP" = 1 ]; then
    summary no-setup
    return 0
  fi
  sign_in_github
  if [ "$GITHUB" = 0 ]; then
    summary no-github
    exit 2
  fi
  run_setup
  if [ "$SETUP_RC" = 0 ]; then summary ready; else summary failed; fi
  exit "$SETUP_RC"
}

main "$@"
