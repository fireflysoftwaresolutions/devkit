# devkit shell integration — sourced from ~/.zshrc (managed by `dev install`).
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0

export DEVKIT_HOME="${${(%):-%x}:A:h:h}"
typeset -U path
path=("$HOME/.local/bin" $path)

# Docker runs on Colima; SDKs (Testcontainers, docker-java, docker-py) read DOCKER_HOST, not docker contexts.
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock

# Java: Homebrew's openjdk is keg-only; expose it (and JAVA_HOME) unless a JDK is already registered with macOS.
# Homebrew lives in /opt/homebrew on Apple Silicon and /usr/local on Intel Macs.
_dk_brew="${HOMEBREW_PREFIX:-/opt/homebrew}"
[[ -x "$_dk_brew/bin/brew" ]] || _dk_brew=/usr/local
if ! /usr/libexec/java_home >/dev/null 2>&1 && [[ -d "$_dk_brew/opt/openjdk" ]]; then
  export JAVA_HOME="$_dk_brew/opt/openjdk/libexec/openjdk.jdk/Contents/Home"
  path=("$_dk_brew/opt/openjdk/bin" $path)
elif [[ -z "$JAVA_HOME" ]]; then
  export JAVA_HOME="$(/usr/libexec/java_home 2>/dev/null)"
fi

# Keep tools quiet and private.
export NG_CLI_ANALYTICS=false
export NEXT_TELEMETRY_DISABLED=1
export SUPERPOWERS_DISABLE_TELEMETRY=1
# Security tools run by people and agents: no telemetry, no update checks.
export TRIVY_DISABLE_TELEMETRY=true TRIVY_SKIP_VERSION_CHECK=true SEMGREP_SEND_METRICS=off
export SYFT_CHECK_FOR_APP_UPDATE=false GRYPE_CHECK_FOR_APP_UPDATE=false CLAUDE_SECURITY_SCAN_TIP=off

# dcd <name>: cd into a project by its alias, id or part of its name (several matches: dev lists them).
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
_dev_projects() { local -a p; p=(${(f)"$(dev __complete aliases 2>/dev/null)"}); _describe 'project' p; }

# The devkit segment in the prompt (spec P-fxs316lh3nw R9), on once `dev setup prompt` recorded how it reaches the
# prompt (the mode file below). Each prompt, _devkit_prompt reads the cache that devkit's hooks and commands write
# (lib/devkit/prompt.py, whose segment() has the same rule) with zsh builtins only: no Python, git or other program
# runs, so a prompt costs well under 5 ms. It exports DEVKIT_PROMPT and DEVKIT_PROMPT_ASK (plain text, for Starship,
# oh-my-posh and Powerlevel10k) and sets _dk_prompt_zsh (colored and %-escaped, for devkit's own zsh block).
# Outside devkit projects they are unset or empty.
typeset -g _dk_pdir="$HOME/.local/state/devkit/prompt" _dk_pmode= _dk_psig= _dk_prompt_zsh=
[[ -r $_dk_pdir/mode ]] && IFS= read -r _dk_pmode < $_dk_pdir/mode
if [[ -n $_dk_pmode && $_dk_pmode != off ]] && zmodload -F zsh/stat b:zstat 2>/dev/null \
    && zmodload zsh/datetime 2>/dev/null; then
  typeset -gA _dkp_alias _dkp_ts _dkp_crew _dkp_ask _dkp_ck _dkp_head _dkp_ref

  _devkit_prompt() {
    emulate -L zsh
    setopt extended_glob
    local f=$_dk_pdir/cache.zsh d a ck g c h t seg ask zs star run ok bad dots sep
    local -i n=0 q=0 wide=0 now=$EPOCHSECONDS
    local -A st
    # The cache is read again only when it changed (devkit replaces it, so its inode changes), and only when it
    # belongs to this user, because it is sourced.
    if ! zstat -H st -- $f 2>/dev/null || (( ${st[uid]} != UID )); then
      _dk_psig= _dk_prompt_zsh=
      unset DEVKIT_PROMPT DEVKIT_PROMPT_ASK
      return 0
    fi
    if [[ "${st[inode]}:${st[mtime]}:${st[size]}" != $_dk_psig ]]; then
      _dkp_alias=() _dkp_ts=() _dkp_crew=() _dkp_ask=() _dkp_ck=() _dkp_head=() _dkp_ref=()
      source $f
      _dk_psig="${st[inode]}:${st[mtime]}:${st[size]}"
    fi
    # The nearest devkit checkout at or above this folder.
    d=${PWD:A}
    while [[ -n $d ]] && (( ! ${+_dkp_alias[$d]} )); do d=${d%/*}; done
    if [[ -z $d ]]; then
      _dk_prompt_zsh=
      unset DEVKIT_PROMPT DEVKIT_PROMPT_ASK
      return 0
    fi
    if [[ ${LC_ALL:-${LC_CTYPE:-$LANG}} == (#i)*utf(-|)8* ]]; then
      star='✦' run='▸' ok='✓' bad='✗' dots='…' sep='·'
    else
      star='*' run='>' ok='ok' bad='x' dots='...' sep='-'
    fi
    (( ${COLUMNS:-80} >= 100 )) && wide=1
    a=${_dkp_alias[$d]}
    (( ${#a} > 16 )) && a="${a[1,16]}$dots"
    seg="$star $a" zs="%F{215}$star%f %B${a//\%/%%}%b"
    if (( now - ${_dkp_ts[$d]:-0} < 86400 )); then
      # Members whose heartbeat is newer than 15 minutes in a run still working: devkit wrote when each stops.
      for t in ${=_dkp_crew[$d]}; do (( t > now )) && (( n++ )); done
      if (( n )); then
        if (( wide )); then seg+=" $sep $n working" zs+=" $sep %F{cyan}$n working%f"
        else seg+=" $run$n" zs+=" %F{cyan}$run$n%f"; fi
      fi
      # Checks belong to the branch they were recorded for: hidden once HEAD is elsewhere.
      ck=${_dkp_ck[$d]}
      if [[ -n $ck && -n ${_dkp_head[$d]} ]]; then
        h=
        { IFS= read -r h < ${_dkp_head[$d]} } 2>/dev/null
        [[ $h == "${_dkp_ref[$d]}" ]] || ck=
      fi
      case $ck in
        passed) g=$ok c=green ;;
        failed) g=$bad c=red ;;
        running) g=$dots c=yellow ;;
        *) g= ;;
      esac
      if [[ -n $g ]]; then
        if (( wide )); then seg+=" $sep checks $g" zs+=" $sep checks %F{$c}$g%f"
        else seg+=" $g" zs+=" %F{$c}$g%f"; fi
      fi
      q=${_dkp_ask[$d]:-0}
      if (( q > 0 )); then
        if (( wide )); then ask="! $q question"; (( q > 1 )) && ask+=s; ask+=" for you"; else ask="!$q"; fi
      fi
    fi
    export DEVKIT_PROMPT=$seg
    if [[ -n $ask ]]; then export DEVKIT_PROMPT_ASK=$ask; else unset DEVKIT_PROMPT_ASK; fi
    if [[ -n $NO_COLOR ]]; then
      _dk_prompt_zsh="${seg//\%/%%} ${ask:+$ask }"
    else
      _dk_prompt_zsh="$zs ${ask:+%K{red}%F{white}%B $ask %b%f%k }"
    fi
  }
  # First of the precmd functions: oh-my-posh and Powerlevel10k draw the prompt in theirs, so the text is set before.
  precmd_functions=(_devkit_prompt ${precmd_functions:#_devkit_prompt})
  autoload -Uz add-zsh-hook

  # devkit's own zsh block, for a prompt without Starship, oh-my-posh or Powerlevel10k: the segment goes first in
  # PROMPT (and back again when a theme sets PROMPT anew), and ~/.zshrc stays as it is.
  if [[ $_dk_pmode == zsh ]]; then
    setopt prompt_subst
    _devkit_prompt_prefix() {
      [[ $PROMPT == *'${_dk_prompt_zsh}'* ]] || PROMPT='${_dk_prompt_zsh}'$PROMPT
    }
    add-zsh-hook precmd _devkit_prompt_prefix
  fi
fi
