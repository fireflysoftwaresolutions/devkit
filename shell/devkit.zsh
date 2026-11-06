# devkit shell integration — sourced from ~/.zshrc (managed by `dev install`).
# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.

export DEVKIT_HOME="${${(%):-%x}:A:h:h}"
typeset -U path
path=("$HOME/.local/bin" $path)

# Docker runs on Colima; SDKs (Testcontainers, docker-java, docker-py) read DOCKER_HOST, not docker contexts.
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock

# Java: Homebrew's openjdk is keg-only; expose it (and JAVA_HOME) unless a JDK is already registered with macOS.
if ! /usr/libexec/java_home >/dev/null 2>&1 && [[ -d /opt/homebrew/opt/openjdk ]]; then
  export JAVA_HOME=/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home
  path=(/opt/homebrew/opt/openjdk/bin $path)
elif [[ -z "$JAVA_HOME" ]]; then
  export JAVA_HOME="$(/usr/libexec/java_home 2>/dev/null)"
fi

# Keep tools quiet and private.
export NG_CLI_ANALYTICS=false
export NEXT_TELEMETRY_DISABLED=1
export SUPERPOWERS_DISABLE_TELEMETRY=1

# dcd <query>: cd into a project (fzf picker when several match).
dcd() {
  local p
  p="$(dev go "$@")" || return
  cd "$p" || return
}

# dwt <branch> [--base REF]: create (or reuse) a managed worktree and cd into it.
dwt() {
  local p
  p="$(dev wt new "$@")" || return
  cd "$p" || return
}

# Completion
fpath=("$DEVKIT_HOME/shell/completions" $fpath)
if (( $+functions[compdef] )); then
  autoload -Uz _dev && compdef _dev dev
  compdef '_dev_projects' dcd 2>/dev/null
else
  autoload -Uz compinit && compinit -C
  autoload -Uz _dev && compdef _dev dev
fi
_dev_projects() { local -a p; p=(${(f)"$(dev __complete projects 2>/dev/null)"}); _describe 'project' p; }
