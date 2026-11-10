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
# In a terminal it first says what it will do and waits for Enter (nothing changes before that). Then, in five
# steps, it checks the Mac, makes sure Homebrew, git, Python, and the GitHub CLI are present, gets devkit, signs you
# in to GitHub, and starts the setup wizard (`dev setup`), whose `dev install` pins a read-only copy of devkit for the
# hooks and the guard in ~/.local/share/devkit/releases. git's and Homebrew's own output goes to
# ~/.local/state/devkit/logs/install.log and is shown only when a step fails. Safe to run again.
# `install.sh --help` lists the options.
#
# Everything is inside functions, and only the last line runs them: a download cut short runs nothing.

usage() {
  cat <<EOF
devkit installer. devkit is for Macs with Apple silicon.

It makes sure Homebrew, git, Python, and the GitHub CLI are present, gets devkit (the newest
release), signs you in to GitHub, and starts the setup wizard. Safe to run again.

In a terminal it first says what it will do and waits for Enter: nothing changes before that.

An Intel Mac can run it only if it already has Homebrew: Homebrew no longer installs on Intel.

Usage:
  curl -fsSL $INSTALL_URL | bash
  curl -fsSL $INSTALL_URL | bash -s -- [options]
  ./install.sh [options]                       (from a clone of devkit)

Options:
  -y, --yes          start without waiting at the welcome, and accept every default in the setup wizard
  --skip-packages    configure only; do not install the toolchain packages for your stacks
  --no-setup         get devkit and its prerequisites, but do not sign in or run the setup wizard
  -h, --help         show this help
  Other options are passed to the setup wizard (dev setup).

Environment:
  DEVKIT_HOME   where devkit goes (default: ~/Development/fireflysoftwaresolutions/devkit)
  DEVKIT_REPO   the repository to clone (default: $DEFAULT_REPO)
  DEVKIT_REF    a tag or branch to check out instead of the newest release
  GH_TOKEN      a GitHub token for installs without a terminal (read by the GitHub CLI)

Without a terminal (CI, scripts), it starts at once, Homebrew installs non-interactively, the
GitHub sign-in is skipped unless the GitHub CLI is already signed in, and the wizard takes every
default. The output of git and Homebrew is in ~/.local/state/devkit/logs/install.log.
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

# One of the five steps: "N of 5  <name>", then, on a first run, one plain sentence on what it does and why.
step() {
  STEP="$2"
  printf '\n%s%s of 5  %s%s\n' "$C_B" "$1" "$2" "$C_OFF"
  if [ "$FIRST" = 1 ] && [ -n "${3:-}" ]; then printf '  %s%s%s\n' "$C_DIM" "$3" "$C_OFF"; fi
}
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
note() { printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
fail() { printf '  %s✗%s %s\n' "$C_BAD" "$C_OFF" "$*" >&2; }
die()  { fail "$*"; exit 1; }
tildify() { case "$1" in "$HOME"/*) printf '~%s\n' "${1#"$HOME"}" ;; *) printf '%s\n' "$1" ;; esac; }

# In a terminal, a line that says what runs now (a command whose output goes to the log); unbusy clears it.
busy() {
  if [ -t 1 ]; then printf '  %s⠹%s %s' "$C_ACC" "$C_OFF" "$1"; BUSY=1; fi
}
unbusy() {
  if [ "$BUSY" = 1 ]; then printf '\r\033[K'; BUSY=0; fi
}

# The local installer with the options of this run, once devkit is on this Mac: the command to run again. Before
# the download there is none, and messages say "the same command you started with" instead: the person may have
# used `gh api … | bash` (a private repository refuses curl), so a URL here could be one they never used.
again_cmd() {
  printf '%s%s\n' "$(tildify "$DEVKIT_HOME/install.sh")" "${ARGS:+ $ARGS}"
}

# --------------------------------------------------------------------------- the log, and failures

# git's and Homebrew's own output goes here, not on the screen; a failed step shows its error line and this path.
open_log() {
  local f="$HOME/.local/state/devkit/logs/install.log"
  if mkdir -p "$(dirname "$f")" 2>/dev/null && (umask 077 && : >"$f") 2>/dev/null; then
    chmod 600 "$f"
    LOG="$f"
  fi
  printf 'devkit installer, %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$LOG"
  log_done
}

log_done() { LOG_MARK="$(wc -l <"$LOG" | tr -d ' ')"; }

# Before a command whose output goes to the log: names it there, and marks where its output starts.
log_start() {
  printf '\n$ %s\n' "$*" >>"$LOG"
  log_done
}

# The line that says why a command failed, from its output on stdin: its first error line (fatal:, error:, Error:),
# else its last line that says anything; without colors or leading spaces, at most 200 characters.
error_line() {
  local text first
  text="$(sed $'s/\x1b\\[[0-9;?]*[A-Za-z]//g' | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e '/^$/d' || true)"
  first="$(printf '%s\n' "$text" | grep -m 1 -E '^(fatal|error|Error|ERROR)[: ]' || true)"
  [ -n "$first" ] || first="$(printf '%s\n' "$text" | tail -n 1)"
  printf '%s\n' "$first" | cut -c 1-200
}

# Runs a command with its output in the log, never on the screen; when it fails, the step fails with `what`.
run_logged() {
  local what="$1" rc=0
  shift
  log_start "$@"
  "$@" </dev/null >>"$LOG" 2>&1 || rc=$?
  unbusy
  [ "$rc" = 0 ] || fail_step "$what" "$rc" "$1"
  log_done
}

# A step that did not finish: what did not happen, the reason (the failed command's own error line from the log, or
# `reason`), the fix, and where the full output is. Exits 1: every step of the installer is needed for the next.
fail_step() {
  local what="$1" rc="$2" cmd="${3:-the command}" reason="${4:-}"
  trap - ERR INT
  unbusy
  [ -n "$reason" ] || reason="$(tail -n +"$((LOG_MARK + 1))" "$LOG" 2>/dev/null | error_line || true)"
  [ -n "$reason" ] || reason="$cmd stopped with exit code $rc and gave no reason."
  {
    printf '\n'
    printf '  %s✗%s %s\n' "$C_BAD" "$C_OFF" "$what"
    printf '    Reason: %s\n' "$reason"
    if [ "$DOWNLOADED" = 1 ]; then
      printf '    Fix: run the installer again:\n      %s\n' "$(again_cmd)"
    else
      printf '    Fix: run the installer again (the same command you started with).\n'
    fi
    printf '    If it fails the same way, send the full output file to your devkit contact.\n'
    printf '    Running it again is safe: finished steps are not repeated.\n'
    if [ "$LOG" != /dev/null ]; then printf '    Full output: %s\n' "$(tildify "$LOG")"; fi
    printf '\n'
  } >&2
  exit 1
}

# Any other command that fails stops the installer the same way. A failure inside $(…) or a pipeline also fails the
# command that ran it: report it once, from there.
on_error() {
  local rc=$? cmd="$BASH_COMMAND"
  [ "${BASH_SUBSHELL:-0}" -eq 0 ] || exit "$rc"
  fail_step "\"$STEP\" did not finish." "$rc" "$cmd"
}

# Ctrl+C during a step: it stops there; what is done stays done.
on_interrupt() {
  trap - INT ERR
  stop_reader
  unbusy
  {
    printf '\n'
    printf '  %s!%s Stopped during "%s".\n' "$C_WARN" "$C_OFF" "$STEP"
    printf '    Nothing is broken.\n'
    if [ "$DOWNLOADED" = 1 ]; then
      printf '    To continue, run:  %s\n' "$(again_cmd)"
      printf '    Finished steps are not repeated.\n\n'
    else
      printf '    To continue, run the same command again. Finished steps are not repeated.\n\n'
    fi
  } >&2
  exit 130
}

# --------------------------------------------------------------------------- the terminal

# Run a command that may ask questions: from the terminal, also when this script arrives through a pipe; with
# no input at all when there is no terminal.
with_tty() {
  case "$TTY" in
    stdin) "$@" ;;
    "") "$@" </dev/null ;;
    *) "$@" <"$TTY" ;;
  esac
}

# A line typed in the terminal, into ANSWER; fails when the terminal is gone. A background `head` reads it while
# the installer waits: bash 3.2 runs a Ctrl+C trap only after its own `read` returns (Ctrl+C at a prompt would wait
# for Enter), but `wait` returns at once. The trap stops the reader (stop_reader), so it cannot keep the next line.
read_answer() {
  local f
  ANSWER=""
  f="$(mktemp "${TMPDIR:-/tmp}/devkit-answer.XXXXXX")"
  case "$TTY" in
    stdin) head -n 1 <&0 >"$f" & ;;
    *) head -n 1 <"$TTY" >"$f" & ;;
  esac
  READER=$!
  wait "$READER" || true
  READER=""
  if [ ! -s "$f" ]; then rm -f "$f"; return 1; fi
  IFS= read -r ANSWER <"$f" || true
  rm -f "$f"
}

stop_reader() {
  if [ -n "$READER" ]; then kill "$READER" 2>/dev/null || true; READER=""; fi
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

# Whether Homebrew, git, Python and the GitHub CLI are all here, looked up without running any of them.
tools_present() {
  local b
  b="$(find_brew)" || return 1
  command -v git >/dev/null 2>&1 && command -v gh >/dev/null 2>&1 && [ -x "$(dirname "$(dirname "$b")")/bin/python3" ]
}

# The consent: what the installer will do, then it waits for Enter. Ctrl+C here ends it with nothing changed.
welcome() {
  local tools=0 done_mark=" ${C_DIM}(already done)${C_OFF}" where
  where="$(tildify "$DEVKIT_HOME")"
  if tools_present; then tools=1; fi
  printf '\n'
  if [ "$tools" = 1 ] && [ "$DOWNLOADED" = 1 ]; then
    printf 'This sets up devkit on this Mac.\nThis takes a minute, then continues your setup.\n\n'
  else
    printf 'This sets up devkit on this Mac. It takes about 15 to 30 minutes, most of it\ninstalling on its own.\n\n'
  fi
  printf '  %s1%s  Check this Mac\n' "$C_DIM" "$C_OFF"
  if [ "$tools" = 1 ]; then
    printf '  %s2%s  Install Homebrew, git, Python and the GitHub CLI%s\n' "$C_DIM" "$C_OFF" "$done_mark"
  else
    printf '  %s2%s  Install Homebrew, git, Python and the GitHub CLI, if they are missing\n' "$C_DIM" "$C_OFF"
  fi
  if [ "$DOWNLOADED" = 1 ]; then
    printf '  %s3%s  Download devkit to %s%s\n' "$C_DIM" "$C_OFF" "$where" "$done_mark"
  else
    printf '  %s3%s  Download devkit to %s\n' "$C_DIM" "$C_OFF" "$where"
  fi
  printf '  %s4%s  Sign you in to GitHub, in your browser\n' "$C_DIM" "$C_OFF"
  printf '  %s5%s  Ask which agents and tools you use, then install and set them up\n\n' "$C_DIM" "$C_OFF"
  if ! find_brew >/dev/null; then printf 'Your Mac password may be asked once, to install Homebrew.\n'; fi
  printf 'Nothing has changed yet.\n\n'
  trap welcome_stopped INT
  printf '%sPress Enter%s to start, or Ctrl+C to stop. ' "$C_B" "$C_OFF"
  if ! read_answer; then welcome_stopped; fi
  trap - INT
}

welcome_stopped() {
  trap - INT
  stop_reader
  printf '\n\n  Stopped. Nothing has changed.\n\n'
  exit 1
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
  step 1 "This Mac" "devkit runs on macOS; this checks the system before anything changes."
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
      if [ "$DOWNLOADED" = 1 ]; then
        printf '    3. Open Terminal again and run the installer:\n      %s\n' "$(again_cmd)"
      else
        printf '    3. Open Terminal again and run the same command you started with.\n'
      fi
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

prepare_tools() {
  step 2 "Homebrew, git, Python and the GitHub CLI" \
    "devkit needs git, Python and the GitHub CLI; Homebrew installs them."
  ensure_homebrew
  ensure_tools
}

ensure_homebrew() {
  local brew_bin installer on_path=1 v rc=0
  command -v brew >/dev/null 2>&1 || on_path=0
  if ! brew_bin="$(find_brew)"; then
    printf '  Homebrew is the standard way to install developer tools on a Mac.\n'
    note "Its installer asks for the password you use to log in to this Mac."
    printf '    Nothing appears while you type it; that is normal. Then press Enter.\n'
    [ -n "$TTY" ] || export NONINTERACTIVE=1
    log_start curl -fsSL "$BREW_INSTALLER"
    installer="$(curl -fsSL "$BREW_INSTALLER" 2>>"$LOG")" || fail_step "Homebrew did not download." $? curl
    log_done
    # Homebrew's installer talks to the person (the password), so its output stays on the screen.
    with_tty /bin/bash -c "$installer" || rc=$?
    [ "$rc" = 0 ] || fail_step "Homebrew did not install." "$rc" "Homebrew's installer" \
      "Homebrew's installer stopped; its own messages are above."
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

# Installs one Homebrew formula, its output in the log. Homebrew asks nothing: the welcome was the consent, and
# HOMEBREW_NO_ASK turns off its "Do you want to proceed?".
install_formula() {
  busy "Installing $1 …"
  run_logged "$1 did not install." brew install "$2"
}

ensure_tools() {
  local v
  git --version >/dev/null 2>&1 || install_formula git git
  PY="$BREW_PREFIX/bin/python3"
  [ -x "$PY" ] || install_formula Python python
  command -v gh >/dev/null 2>&1 || install_formula "GitHub CLI" gh
  v="$(git --version)"
  v="${v#git version }"
  ok "git ${v%% *}"
  ok "$("$PY" --version 2>&1)"
  v="$(gh --version)"
  v="${v%%$'\n'*}"
  v="${v#gh version }"
  ok "GitHub CLI ${v%% *}"
}

get_devkit() {
  step 3 "devkit" "devkit itself: the newest release, in your Development folder."
  local ref tags where at
  where="$(tildify "$DEVKIT_HOME")"
  if [ -e "$DEVKIT_HOME/.git" ]; then
    [ -f "$DEVKIT_HOME/bin/dev" ] || die "$where is a git checkout, but not of devkit. Choose another folder with DEVKIT_HOME."
    update_existing "$where"
    DOWNLOADED=1
    return 0
  fi
  if [ -e "$DEVKIT_HOME" ] && [ -n "$(ls -A "$DEVKIT_HOME")" ]; then
    die "$where exists and is not a git checkout, so it was left untouched. Move it away, or choose another folder with DEVKIT_HOME."
  fi
  ref="${DEVKIT_REF:-}"
  busy "Downloading devkit …"
  if [ -z "$ref" ]; then
    log_start git ls-remote --tags --refs "$DEVKIT_REPO"
    tags="$(git ls-remote --tags --refs "$DEVKIT_REPO" 2>>"$LOG")" || fail_step "devkit did not download." $? git
    log_done
    ref="$(printf '%s\n' "$tags" | sed 's#.*refs/tags/##' | newest_release)"
    [ -n "$ref" ] || { unbusy; note "$DEVKIT_REPO has no release tag yet: using its default branch"; }
  fi
  mkdir -p "$(dirname "$DEVKIT_HOME")"
  if [ -n "$ref" ]; then
    run_logged "devkit did not download." git clone --quiet --no-checkout "$DEVKIT_REPO" "$DEVKIT_HOME"
    check_out "$ref"
  else
    run_logged "devkit did not download." git clone --quiet "$DEVKIT_REPO" "$DEVKIT_HOME"
  fi
  unbusy
  DOWNLOADED=1
  at="$(describe)"
  case "$at" in v*) at="" ;; *) at=" ($at)" ;; esac
  ok "Downloaded devkit $(cat "$DEVKIT_HOME/VERSION" 2>/dev/null || printf 'unknown') to $where$at"
}

# Checks out a fresh clone at a release tag (detached, on the tag's commit) or at a branch, and removes the default
# branch the clone made. Not `git clone --branch <tag>`: for an annotated tag, which every release is, git prints
# "warning: refs/tags/… is not a commit!". The tag stays on HEAD, which dev install checks before it pins devkit.
check_out() {
  local ref="$1" made
  made="$(git -C "$DEVKIT_HOME" symbolic-ref -q --short HEAD || true)"
  if git -C "$DEVKIT_HOME" rev-parse -q --verify "refs/tags/$ref^{commit}" >/dev/null ||
    git -C "$DEVKIT_HOME" fetch --quiet origin "refs/tags/$ref:refs/tags/$ref" >>"$LOG" 2>&1; then
    run_logged "devkit did not download." \
      git -C "$DEVKIT_HOME" -c advice.detachedHead=false checkout --quiet --detach "refs/tags/$ref^{commit}"
  else
    run_logged "devkit did not download." git -C "$DEVKIT_HOME" checkout --quiet "$ref"
  fi
  if [ -n "$made" ] && [ "$(git -C "$DEVKIT_HOME" symbolic-ref -q --short HEAD || true)" != "$made" ]; then
    run_logged "devkit did not download." git -C "$DEVKIT_HOME" branch --quiet -D "$made"
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
  busy "Looking for a newer devkit …"
  run_logged "devkit did not update." git -C "$DEVKIT_HOME" fetch --quiet --tags
  unbusy
  target="${DEVKIT_REF:-}"
  if [ -z "$target" ]; then
    target="$(git -C "$DEVKIT_HOME" tag --list 'v*' | newest_release)"
    if [ "$(printf '%s\n%s\n' "$current" "$target" | newest_release)" = "$current" ]; then
      ok "$where is at the newest release, $current"
      return 0
    fi
  fi
  run_logged "devkit did not update." git -C "$DEVKIT_HOME" -c advice.detachedHead=false checkout --quiet "$target"
  ok "$where moved from ${current:-its commit} to $(describe)"
}

use_checkout() {
  step 3 "devkit" "devkit itself: the checkout this script is in."
  ok "this checkout: $(tildify "$DEVKIT_HOME") ($(describe))"
  if [ -z "$(git -C "$DEVKIT_HOME" tag --points-at HEAD 2>/dev/null | newest_release || true)" ]; then
    note "not a release: dev install pins it for the hooks only after its tests pass here (bin/dev ci --run)"
  fi
}

# Sets GITHUB=0 when the GitHub CLI is not signed in and there is no terminal to sign in from. In a terminal, a
# sign-in that does not finish offers to try again or to quit; the installer never goes on without GitHub.
sign_in_github() {
  step 4 "GitHub sign-in"
  local login
  GITHUB=1
  if ! gh auth status >/dev/null 2>&1; then
    if [ -z "$TTY" ]; then
      note "no terminal to sign in from: skipped"
      GITHUB=0
      return 0
    fi
    github_login
  fi
  login="$(gh api user --jq .login 2>/dev/null || true)"
  ok "Signed in to GitHub${login:+ as $login}"
}

github_login() {
  local help clip=() code="Type the one-time code shown below."
  help="$(gh auth login --help 2>/dev/null || true)"
  case "$help" in
    *--clipboard*) clip=(--clipboard) code="Paste the one-time code shown below (it is copied for you)." ;;
  esac
  printf '  %sdevkit keeps your project notes in a private GitHub repository.%s\n\n' "$C_DIM" "$C_OFF"
  printf '  Your browser opens GitHub. There:\n'
  printf '    1. %s\n' "$code"
  printf "    2. Choose Authorize. If your company's organization is listed,\n"
  printf '       authorize it too.\n'
  printf '  Then come back here. This waits up to 10 minutes; Ctrl+C stops it.\n\n'
  while :; do
    gh_login ${clip[@]+"${clip[@]}"}
    if [ "$GH_RC" = 0 ] && gh auth status >/dev/null 2>&1; then return 0; fi
    printf '\n  %s✗%s You are not signed in to GitHub.\n' "$C_BAD" "$C_OFF"
    printf '    Reason: %s\n' "$GH_REASON"
    printf '    devkit needs GitHub for your project notes, so it cannot go on without it.\n\n'
    printf '  Press Enter to try again, or type q and press Enter to quit. '
    read_answer || ANSWER=q
    case "$ANSWER" in
      q | Q | quit)
        printf '\n  Stopped before setup. devkit is downloaded; nothing else is set up yet.\n'
        printf '  To continue, run:  %s\n\n' "$(again_cmd)"
        exit 2
        ;;
    esac
    printf '\n'
  done
}

# One browser sign-in to GitHub, with the terminal for its questions, its error lines kept, and a time limit (a
# watchdog stops it) -> GH_RC and GH_REASON. Ctrl+C stops this sign-in only.
gh_login() {
  local dir pidf flag errf wd limit="${DEVKIT_TEST_GH_LIMIT:-600}"
  case "$limit" in '' | *[!0-9]*) limit=600 ;; esac
  dir="$(mktemp -d "${TMPDIR:-/tmp}/devkit-gh.XXXXXX")"
  pidf="$dir/pid" flag="$dir/timeout" errf="$dir/err"
  : >"$errf"
  GH_STOPPED=0 GH_RC=0
  (
    trap - ERR
    set +e
    sleep "$limit" &
    s=$!
    trap 'kill "$s" 2>/dev/null; exit 0' TERM
    wait "$s"
    : >"$flag"
    [ -s "$pidf" ] && kill "$(cat "$pidf")" 2>/dev/null
    exit 0
  ) &
  wd=$!
  trap 'GH_STOPPED=1' INT
  trap - ERR
  set +e
  # gh's stderr (its code, its errors) goes through tee: to the screen as it comes, and to a file for the reason. The
  # wrapper writes gh's process id first, for the watchdog.
  # shellcheck disable=SC2016  # $$, $0 and $@ are the wrapper's own, expanded by /bin/sh
  {
    with_tty /bin/sh -c 'echo $$ >"$0"; exec "$@"' "$pidf" gh auth login -h github.com -p https -w \
      -s repo,read:org,workflow,gist "$@" 2>&1 1>&3 3>&- | tee -i -a "$errf" >&2
    GH_RC=${PIPESTATUS[0]}
  } 3>&1
  kill "$wd" 2>/dev/null
  wait "$wd" 2>/dev/null
  set -e
  trap on_error ERR
  trap on_interrupt INT
  if [ -f "$flag" ]; then
    GH_REASON="the browser page was not finished within 10 minutes."
  elif [ "$GH_STOPPED" = 1 ] || [ "$GH_RC" = 130 ]; then
    GH_REASON="you stopped the sign-in."
  else
    GH_REASON="$(error_line <"$errf")"
    GH_REASON="${GH_REASON:+GitHub said: $GH_REASON}"
    GH_REASON="${GH_REASON:-the sign-in did not finish.}"
  fi
  { printf '\n$ gh auth login (exit %s)\n' "$GH_RC"; cat "$errf"; } >>"$LOG"
  rm -rf "$dir"
}

run_setup() {
  step 5 "Your setup"
  local args=()
  args=(${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"})
  if [ -z "$TTY" ] && [ "$YES" = 0 ]; then
    note "no terminal for questions: the wizard takes every default (--yes)"
    args+=(--yes)
  else
    printf '  Opening the setup wizard. Press Enter to keep a recommended answer.\n'
  fi
  # The wizard's "run it again" lines name this local installer.
  export DEVKIT_INSTALLER="$DEVKIT_HOME/install.sh"
  SETUP_RC=0
  # Ctrl+C belongs to the wizard now: it stops one of its steps, or asks before it quits. The installer only waits.
  trap ':' INT
  with_tty "$PY" "$DEVKIT_HOME/bin/dev" setup ${args[@]+"${args[@]}"} || SETUP_RC=$?
  trap on_interrupt INT
}

# The installer's own last words, only for what the wizard does not report: no setup asked for, no GitHub, or a
# wizard that stopped before its own summary.
summary() {
  local log="$HOME/.local/state/devkit/logs/setup.log"
  [ -f "$log" ] || log="$LOG"
  printf '\n'
  case "$1" in
    no-setup)
      note "devkit is downloaded; nothing else is set up yet (--no-setup)."
      printf '    To set it up, run:  %s\n\n' "$(tildify "$DEVKIT_HOME/install.sh")"
      ;;
    no-github)
      note "Not finished: the GitHub CLI is not signed in, and there is no terminal to sign in from."
      printf '\n  %sTo finish%s, in a terminal:\n' "$C_B" "$C_OFF"
      printf '  1. gh auth login -h github.com -p https -w -s repo,read:org,workflow,gist   (or set GH_TOKEN)\n'
      printf '  2. %s\n\n' "$(again_cmd)"
      ;;
    wizard)
      {
        printf '  %s✗%s The setup wizard could not start.\n' "$C_BAD" "$C_OFF"
        printf '    Reason: dev setup stopped with exit code %s before its own summary.\n' "$SETUP_RC"
        printf '    Full output: %s\n' "$(tildify "$log")"
        printf '    To try again, run:  %s\n\n' "$(again_cmd)"
      } >&2
      ;;
  esac
}

# --------------------------------------------------------------------------- main

main() {
  set -eEuo pipefail
  INSTALL_URL="https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh"
  DEFAULT_REPO="https://github.com/fireflysoftwaresolutions/devkit.git"
  DOCS_URL="https://github.com/fireflysoftwaresolutions/devkit/blob/main/docs"
  BREW_INSTALLER="https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
  # Where Homebrew installs brew, for when it is not on PATH: Apple silicon, then Intel.
  BREW_ARM=/opt/homebrew/bin/brew BREW_INTEL=/usr/local/bin/brew
  # Test hooks only; not supported settings. DEVKIT_TEST_BREW_PATHS: "<Apple silicon brew>:<Intel brew>" in a
  # temporary folder, so that the tests never reach this Mac's Homebrew; find_brew skips an entry that is not an
  # absolute path. DEVKIT_TEST_GH_LIMIT: the GitHub sign-in's time limit in seconds (gh_login), instead of 10 minutes.
  if [ -n "${DEVKIT_TEST_BREW_PATHS:-}" ]; then IFS=: read -r BREW_ARM BREW_INTEL <<<"$DEVKIT_TEST_BREW_PATHS"; fi
  SETUP_ARGS=() NO_SETUP=0 YES=0 STEP="start" BREW_PREFIX="" PY="" SETUP_RC=0 GITHUB=0 BUSY=0
  LOG=/dev/null LOG_MARK=0 DOWNLOADED=0 FIRST=1 ARGS="$*" READER=""
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
  printf '%sdevkit installer%s\n' "$C_B" "$C_OFF"

  # From a clone (./install.sh): set up that checkout. Through a pipe, or as a file on its own: get devkit first.
  src="${BASH_SOURCE[0]:-}"
  if [ -n "$src" ] && [ -f "$src" ]; then here="$(cd "$(dirname "$src")" && pwd)"; fi
  if [ -n "$here" ] && [ -f "$here/bin/dev" ] && [ -f "$here/VERSION" ]; then
    MODE=local DEVKIT_HOME="$here" DOWNLOADED=1
  else
    MODE=bootstrap
    DEVKIT_HOME="${DEVKIT_HOME:-$HOME/Development/fireflysoftwaresolutions/devkit}"
    case "$DEVKIT_HOME" in /*) ;; *) DEVKIT_HOME="$PWD/$DEVKIT_HOME" ;; esac
    DEVKIT_REPO="${DEVKIT_REPO:-$DEFAULT_REPO}"
    if [ -e "$DEVKIT_HOME/.git" ] && [ -f "$DEVKIT_HOME/bin/dev" ]; then DOWNLOADED=1; fi
  fi
  if [ "$DOWNLOADED" = 1 ] && tools_present; then FIRST=0; fi

  # Questions go to the terminal, also when this script arrives through a pipe; without a terminal, nothing asks.
  # Through a pipe, stdin is this script: every other command reads /dev/null instead, never what is left of it.
  if [ -t 0 ] && [ "$(tty 2>/dev/null || true)" != /dev/tty ]; then TTY=stdin
  elif TTY="$(terminal_device)"; then exec </dev/null
  else TTY="" && exec </dev/null
  fi
  # Homebrew asks before it installs ("Do you want to proceed? [y/n]") unless HOMEBREW_NO_ASK is set: the switch it
  # documents. The welcome below was the consent; --yes or no terminal stand for it.
  export HOMEBREW_NO_ASK=1 HOMEBREW_NO_ENV_HINTS=1

  if [ -n "$TTY" ] && [ "$YES" = 0 ]; then welcome; fi
  open_log
  trap on_error ERR
  trap on_interrupt INT
  check_mac
  prepare_tools
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
  # The wizard ended with its own summary (0 ready, 1 not ready or quit, 2 no GitHub, 130 paused): nothing to add.
  case "$SETUP_RC" in 0 | 1 | 2 | 130) exit "$SETUP_RC" ;; esac
  summary wizard
  exit 1
}

main "$@"
