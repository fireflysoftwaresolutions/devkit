# bash completion for devkit's `dev` command. Generated from dev's own parser (lib/devkit/cli.py, build_parser) by
# `dev completion bash`: do not edit it by hand. Load it from your bash startup file with:
#   eval "$(dev completion bash)"
# After changing a command, run: bin/dev completion bash > shell/completions/dev.bash
# CI fails while this file differs from the parser (selfcheck D13).

# _dev_values KIND [ARG]: _dev_reply = the values of `dev __complete KIND [ARG]` (descriptions dropped) for the project
# or initiative the line names (-p, -i), kept for $_dev_ttl seconds (10). A Tab waits at most $_dev_wait seconds (0.1;
# whole seconds before bash 4) for the command; past it, the last values are offered and the command finishes in the
# background for a later Tab.
_dev_values() {
  local kind=$1 arg=$2 key var at= f= line w ok= wait=${_dev_wait:-0.1} i
  local dir=${XDG_CACHE_HOME:-$HOME/.cache}/devkit/complete
  local -a cmd vals
  _dev_reply=()
  cmd=(dev __complete "$kind")
  for (( i = 1; i < COMP_CWORD; i++ )); do
    case ${COMP_WORDS[i]} in
      -p|--project|-i|--initiative)
        w=${COMP_WORDS[i+1]}
        [[ $w == = ]] && w=${COMP_WORDS[i+2]}
        case ${COMP_WORDS[i]} in
          -p|--project) cmd+=(--project "$w") ;;
          *) cmd+=(--initiative "$w") ;;
        esac ;;
    esac
  done
  [[ -n $arg && $arg != - ]] && cmd+=("$arg")
  key="${cmd[*]:2}"
  case " $_dev_here " in *" $kind "*) key="$key @$PWD" ;; esac
  var=_dev_c_${key//[^a-zA-Z0-9]/_}
  eval "at=\${${var}_at:-}; f=\${${var}_pending:-}"
  if [[ -n $at ]] && (( SECONDS - at < ${_dev_ttl:-10} )); then
    eval "_dev_reply=(\"\${${var}[@]}\")"
    return 0
  fi
  if [[ -z $f ]]; then
    [[ -d $dir ]] || mkdir -p -m 700 "$dir" 2>/dev/null || return 1
    f=$dir/$$.$RANDOM$RANDOM
    if (( BASH_VERSINFO[0] < 4 )); then
      wait=${wait%%.*}
      (( ${wait:-0} < 1 )) && wait=1
    fi
    exec 9< <("${cmd[@]}" >"$f.part" 2>/dev/null; mv -f "$f.part" "$f" 2>/dev/null; echo)
    IFS= read -r -t "$wait" -u 9 line && ok=1
    exec 9<&-
    [[ -n $ok ]] || eval "${var}_pending=\$f"
  fi
  if [[ -e $f ]]; then
    vals=()
    while IFS= read -r line; do
      line=${line//\\:/$'\001'}
      line=${line%%:*}
      [[ -n $line ]] && vals+=("${line//$'\001'/:}")
    done < "$f"
    rm -f "$f"
    eval "${var}_pending=; ${var}_at=\$SECONDS; ${var}=(\"\${vals[@]}\")"
  fi
  eval "_dev_reply=(\"\${${var}[@]}\")"
  return 0
}

# _dev_complete_with TOKEN: COMPREPLY = what TOKEN (from _dev_spec) offers for the word being typed: c:<words>
# (choices), m:<words> (a comma list), f (files), d (folders), l:<kinds>:<arg>:<words>:<f|d> (live values), n.
_dev_complete_with() {
  local t=$1 cur=$_dev_cur kinds arg words files k v q w pre IFS=$' \t\n'
  case $t in
    c:*) w=${t#c:}
         COMPREPLY=($(compgen -W "${w//,/ }" -- "$cur")) ;;
    m:*) w=${t#m:}
         pre=
         [[ $cur == *,* ]] && pre=${cur%,*},
         COMPREPLY=($(compgen -P "$pre" -W "${w//,/ }" -- "${cur##*,}")) ;;
    f) COMPREPLY=($(compgen -f -- "$cur")) ;;
    d) COMPREPLY=($(compgen -d -- "$cur")) ;;
    l:*) IFS=: read -r _ kinds arg words files <<< "$t"
         IFS=$' \t\n'
         [[ $arg == @* ]] && arg=${_dev_args[${arg#@} - 1]:--}
         COMPREPLY=($(compgen -W "${words//,/ }" -- "$cur"))
         for k in ${kinds//,/ }; do
           [[ $k == - ]] && continue
           _dev_values "$k" "$arg"
           for v in "${_dev_reply[@]}"; do
             [[ $v == "$cur"* ]] || continue
             printf -v q '%q' "$v"
             COMPREPLY+=("$q")
           done
         done
         [[ $files == f ]] && COMPREPLY+=($(compgen -f -- "$cur"))
         [[ $files == d ]] && COMPREPLY+=($(compgen -d -- "$cur")) ;;
  esac
  return 0
}

_dev() {
  local c o v p path= w i n=0 want= tok t _dev_cur=${COMP_WORDS[COMP_CWORD]} IFS=$' \t\n'
  local -a ps _dev_args
  COMPREPLY=()
  _dev_spec ""
  for (( i = 1; i < COMP_CWORD; i++ )); do
    w=${COMP_WORDS[i]}
    if [[ -n $want ]]; then
      [[ $w == = ]] && continue
      want=
      continue
    fi
    case $w in
      -*=*) continue ;;
      -*) for t in $v; do [[ ${t%%=*} == "$w" ]] && want=1; done
          continue ;;
    esac
    if (( n == 0 )) && [[ -n $c && " $c " == *" $w "* ]]; then
      path="${path:+$path }$w"
      _dev_spec "$path"
      continue
    fi
    _dev_args+=("$w")
    n=$((n + 1))
  done
  if [[ -n $want ]]; then
    w=${COMP_WORDS[COMP_CWORD-1]}
    [[ $w == = ]] && w=${COMP_WORDS[COMP_CWORD-2]}
    tok=
    for t in $v; do [[ ${t%%=*} == "$w" ]] && tok=${t#*=}; done
    _dev_complete_with "${tok:-n}"
    return 0
  fi
  if [[ $_dev_cur == -* ]]; then
    COMPREPLY=($(compgen -W "$o" -- "$_dev_cur"))
    return 0
  fi
  if [[ -n $c ]]; then
    COMPREPLY=($(compgen -W "$c" -- "$_dev_cur"))
    return 0
  fi
  ps=($p)
  tok=
  for (( i = 0; i < ${#ps[@]}; i++ )); do
    t=${ps[i]}
    if [[ $t == \** ]]; then
      tok=${t#\*}
      break
    fi
    if (( i == n )); then
      tok=$t
      break
    fi
  done
  _dev_complete_with "${tok:-n}"
  return 0
}

_dev_here='tasks steps plans branches worktrees'

# _dev_spec PATH: the commands (c), options (o), options that take a value with what completes it (v), and what
# completes each argument (p) of the dev command PATH.
_dev_spec() {
  c= o= v= p=
  case "$1" in
    '') c='help ls go alias status clone new adopt open claude codex handoff-import handoff session task note plan progress time delivery lesson sleep jobs audit report rm mv cp files find grep diff stubs team initiative overview standup dashboard config agents calver plugin guard wt backup sync du free gc canary ci branch commit rebase push pr run release issue git gh test mcp desktop crew impeccable doctor install docs sec diagram setup prompt update docker version completion' o='-h --help' ;;
    'help') o='-h --help' ;;
    'ls') o='-h --help --offline --names --all --json' ;;
    'go') o='-h --help' p='l:projects:-:: l:worktrees:@1::' ;;
    'alias') o='-h --help --move' p='c:rm l:projects:-::' ;;
    'status') o='-h --help --json --all' p='l:projects:-::' ;;
    'clone') o='-h --help' p='l:projects:ids::' ;;
    'new') o='-h --help --github --owner --public --no-git' v='--owner=n' p='n' ;;
    'adopt') o='-h --help' p='d' ;;
    'open') o='-h --help' p='l:projects:-::' ;;
    'claude') o='-h --help -w --worktree' v='-w=l:worktrees:@1:: --worktree=l:worktrees:@1::' p='l:projects:-:: *n' ;;
    'codex') o='-h --help -w --worktree' v='-w=l:worktrees:@1:: --worktree=l:worktrees:@1::' p='l:projects:-:: *n' ;;
    'handoff-import') o='-h --help --slug --dry-run' v='--slug=n' p='d' ;;
    'handoff') o='-h --help --edit --path --log --agent' v='--log=n --agent=n' p='l:projects:-::' ;;
    'session') o='-h --help --session --latest -p --project --for-chat --provider --from --members --all' v='--session=n -p=l:projects:ids:: --project=l:projects:ids:: --provider=n --from=n' p='c:brief,carry,compactions,forget f' ;;
    'task') c='add start claim release done block unblock cancel reopen show history link delegate move edit ls' o='-h --help -p --project -i --initiative --all' v='-p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' ;;
    'task add') o='-h --help --now --next --later --type --priority --due --body --why --claim --done --no-template -p --project -i --initiative' v='--type=c:task,feature,bug,spike --priority=c:high,normal,low --due=n --body=n --why=n --done=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*n' ;;
    'task start') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task claim') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task release') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task done') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task block') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task unblock') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task cancel') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task reopen') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:all::' ;;
    'task show') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:all::' ;;
    'task history') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:all::' ;;
    'task link') o='-h --help --to --why -p --project -i --initiative' v='--to=l:initiatives:-:: --why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task delegate') o='-h --help --to --why --due --no-copy -p --project -i --initiative' v='--to=n --why=n --due=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task move') o='-h --help --to --why -p --project -i --initiative' v='--to=c:now,next,later --why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task edit') o='-h --help --title --body-file --priority --due --why -p --project -i --initiative' v='--title=n --body-file=f --priority=c:high,normal,low --due=n --why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*l:tasks:-::' ;;
    'task ls') o='-h --help -p --project -i --initiative' v='-p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' ;;
    'note') o='-h --help -p --project -i --initiative' v='-p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='c:set *n' ;;
    'plan') c='new revise approve supersede ls' o='-h --help -p --project -i --initiative' v='-p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' ;;
    'plan new') o='-h --help --kind -p --project -i --initiative' v='--kind=c:plan,spec,spike,bugfix,adr -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*n' ;;
    'plan revise') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='l:plans:-::' ;;
    'plan approve') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='l:plans:-::' ;;
    'plan supersede') o='-h --help --why -p --project -i --initiative' v='--why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='l:plans:-::' ;;
    'plan ls') o='-h --help -p --project -i --initiative' v='-p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' ;;
    'progress') c='start note step block done assign begin add reopen skip history time' o='-h --help' ;;
    'progress start') o='-h --help --steps --estimate --mode --plan --replace --why' v='--steps=n --estimate=n --mode=c:guided,auto --plan=n --why=n' p='*n' ;;
    'progress note') o='-h --help --next' v='--next=n' p='*n' ;;
    'progress step') o='-h --help --next --check --step --force --no-check --approves' v='--next=n --check=n --step=l:steps:-:: --approves=l:steps:-::' p='*n' ;;
    'progress block') o='-h --help --next --check --step --force' v='--next=n --check=n --step=l:steps:-::' p='*n' ;;
    'progress done') o='-h --help --next --check --step --force --why' v='--next=n --check=n --step=l:steps:-:: --why=n' p='*n' ;;
    'progress assign') o='-h --help --to --scope --why --activity --allow' v='--to=l:crew:-:: --scope=n --why=n --activity=l:activities:-:: --allow=n' p='l:steps:-::' ;;
    'progress begin') o='-h --help' p='l:steps:-::' ;;
    'progress add') o='-h --help --why' v='--why=n' p='*n' ;;
    'progress reopen') o='-h --help --why' v='--why=n' p='l:steps:-::' ;;
    'progress skip') o='-h --help --why' v='--why=n' p='l:steps:-::' ;;
    'progress history') o='-h --help' ;;
    'progress time') o='-h --help --days -p --project' v='--days=n -p=l:projects:-:: --project=l:projects:-::' ;;
    'time') o='-h --help --days -p --project' v='--days=n -p=l:projects:-:: --project=l:projects:-::' ;;
    'delivery') c='apply' o='-h --help -p --project -i --initiative --json' v='-p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' ;;
    'delivery apply') o='-h --help --set --why --tasks -p --project -i --initiative' v='--set=n --why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' ;;
    'lesson') c='add anchor show recur retire restore prevent scan review ls' o='-h --help -p --project -i --initiative --global --all --search' v='-p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-:: --search=n' ;;
    'lesson add') o='-h --help --kind --severity --happened --cause --fix --prevent --detect --evidence --tag --retire-when --why --file --command --activity --error --stack -p --project -i --initiative --global' v='--kind=c:bug,mistake,process,tooling,security,performance,design --severity=c:low,medium,high --happened=n --cause=n --fix=n --prevent=n --detect=n --evidence=n --tag=n --retire-when=n --why=n --file=n --command=n --activity=l:activities:-:: --error=n --stack=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*n' ;;
    'lesson anchor') o='-h --help --why --file --command --activity --error --stack -p --project -i --initiative --global' v='--why=n --file=n --command=n --activity=l:activities:-:: --error=n --stack=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*n' ;;
    'lesson show') o='-h --help --evidence --why -p --project -i --initiative --global' v='--evidence=n --why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*n' ;;
    'lesson recur') o='-h --help --evidence --why -p --project -i --initiative --global' v='--evidence=n --why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*n' ;;
    'lesson retire') o='-h --help --evidence --why -p --project -i --initiative --global' v='--evidence=n --why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*n' ;;
    'lesson restore') o='-h --help --evidence --why -p --project -i --initiative --global' v='--evidence=n --why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*n' ;;
    'lesson prevent') o='-h --help --by --ref --why -p --project -i --initiative --global' v='--by=c:test,lint,type,guard,skill,ci,rule,doc --ref=n --why=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' p='*n' ;;
    'lesson scan') o='-h --help --days -p --project -i --initiative --global' v='--days=n -p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' ;;
    'lesson review') o='-h --help -p --project -i --initiative --global' v='-p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' ;;
    'lesson ls') o='-h --help -p --project -i --initiative --global' v='-p=l:projects:-:: --project=l:projects:-:: -i=l:initiatives:-:: --initiative=l:initiatives:-::' ;;
    'sleep') o='-h --help --dry-run --quiet' ;;
    'jobs') c='ls install show run enable disable logs set journal' o='-h --help' ;;
    'jobs ls') o='-h --help' ;;
    'jobs install') o='-h --help' ;;
    'jobs show') o='-h --help --why -n --lines' v='--why=n -n=n --lines=n' p='n' ;;
    'jobs run') o='-h --help --why -n --lines' v='--why=n -n=n --lines=n' p='n' ;;
    'jobs enable') o='-h --help --why -n --lines' v='--why=n -n=n --lines=n' p='n' ;;
    'jobs disable') o='-h --help --why -n --lines' v='--why=n -n=n --lines=n' p='n' ;;
    'jobs logs') o='-h --help --why -n --lines' v='--why=n -n=n --lines=n' p='n' ;;
    'jobs set') o='-h --help --every --at --weekly --why' v='--every=n --at=n --weekly=n --why=n' p='n' ;;
    'jobs journal') o='-h --help --days -n --lines' v='--days=n -n=n --lines=n' ;;
    'audit') o='-h --help --days --since --until --action --actor -p --project --member --category --severity --outcome --grep --team --format --json -o --out -n --lines --quiet' v='--days=n --since=n --until=n --action=n --actor=n -p=l:projects:ids:: --project=l:projects:ids:: --member=l:crew:-:: --category=c:change,decision,refusal,security,lifecycle,job,release,data,config --severity=c:info,notice,warning,critical --outcome=c:ok,blocked,failed,recorded --grep=n --format=n -o=f --out=f -n=n --lines=n' p='c:verify,export,show,stats,config n' ;;
    'report') o='-h --help --days -i --initiative -p --project -o --out' v='--days=n -i=l:initiatives:-:: --initiative=l:initiatives:-:: -p=l:projects:-:: --project=l:projects:-:: -o=f --out=f' ;;
    'rm') o='-h --help -r -R --recursive -f --tracked --discard --force-in-use --outside --dry-run --why --undo --list' v='--why=n --undo=n' p='*f' ;;
    'mv') o='-h --help -f --force --dry-run --cross' p='*f' ;;
    'cp') o='-h --help -f --force --dry-run -r -R --recursive --skip-heavy --include-heavy' p='*f' ;;
    'files') o='-h --help -d --depth --all --max --json' v='-d=n --depth=n --max=n' p='d' ;;
    'find') o='-h --help -e --ext -t --type -g --glob --regex -p --full-path --changed --since --size --all --max --json' v='-e=n --ext=n -t=c:f,d --type=c:f,d --since=n --size=n --max=n' p='n d' ;;
    'grep') o='-h --help -i --ignore-case -s --case-sensitive -F --fixed -w --word -g --glob -t --type -l --files -C --context --count --max --max-per-file --all --ast --lang --json' v='-g=n --glob=n -t=n --type=n -C=n --context=n --max=n --max-per-file=n --lang=n' p='n *f' ;;
    'diff') o='-h --help --staged --cached --word --names --syntax -U --unified --max --fold --no-index' v='-U=n --unified=n --max=n --fold=n' p='*l:branches:-::f' ;;
    'stubs') o='-h --help --staged --since --all --json -p --project' v='--since=l:branches:-:: -p=l:projects:-:: --project=l:projects:-::' p='*f' ;;
    'team') o='-h --help --hours' v='--hours=n' ;;
    'initiative') c='new add rm ls show' o='-h --help' ;;
    'initiative new') o='-h --help --title --projects' v='--title=n --projects=l:projects:ids::' p='n' ;;
    'initiative add') o='-h --help' p='l:initiatives:-:: l:projects:-::' ;;
    'initiative rm') o='-h --help' p='l:initiatives:-:: l:projects:-::' ;;
    'initiative ls') o='-h --help' ;;
    'initiative show') o='-h --help' p='l:initiatives:-::' ;;
    'overview') o='-h --help --json --days --all' v='--days=n' ;;
    'standup') o='-h --help --days' v='--days=n' ;;
    'dashboard') o='-h --help --no-open --status --stop --export --path' v='--export=f' ;;
    'config') o='-h --help --quiet --check' p='c:diff,repair,show,edit,validate,lock,unlock,status,set n n' ;;
    'agents') o='-h --help --section --json --model -v --verbose' v='--section=n --model=n' p='c:status,test,rules c:claude,codex,all' ;;
    'calver') o='-h --help --next' ;;
    'plugin') o='-h --help' p='c:validate' ;;
    'guard') o='-h --help --write --agent --cwd' v='--write=f --agent=c:claude,codex --cwd=d' p='c:check *n' ;;
    'wt') c='new ls rm' o='-h --help' ;;
    'wt new') o='-h --help --base --repo --no-deps' v='--base=l:branches:-:: --repo=l:projects:-::' p='n' ;;
    'wt ls') o='-h --help --all' ;;
    'wt rm') o='-h --help' p='l:worktrees:-::' ;;
    'backup') o='-h --help --all --accept-ci --public --dry-run -p --project --why --json' v='-p=l:projects:-:: --project=l:projects:-:: --why=n' p='*l:branches:-:policy:' ;;
    'sync') o='-h --help --offline --scheduled --no-push --quiet' ;;
    'du') o='-h --help --refresh --plan --json' p='l:projects:-::' ;;
    'free') o='-h --help --safe -y --yes' p='*n' ;;
    'gc') o='-h --help --safe -y --yes --apply --auto-only -v --verbose' p='*n' ;;
    'canary') o='-h --help --days --limit' v='--days=n --limit=n' ;;
    'ci') o='-h --help -p --project --sha --timeout --quiet --commands --runs --status --branch --all-branches --watch --limit --all-jobs --json --run --mark-passed --note --evidence --dry-run --keep-going --step-timeout --resume --provider --fix --strict --stack --force' v='-p=l:projects:-:: --project=l:projects:-:: --sha=n --timeout=n --branch=l:branches:-:: --limit=n --note=n --step-timeout=n --provider=c:github,gitlab,bitbucket,azure --stack=c:auto,python,node,java-maven,java-gradle,go,php,docker' p='l:projects:-:status,audit,init,watch:' ;;
    'branch') c='new rename ls clean embargo' o='-h --help' ;;
    'branch new') o='-h --help --ticket --base' v='--ticket=n --base=l:branches:-::' p='n' ;;
    'branch rename') o='-h --help --ticket' v='--ticket=n' p='n' ;;
    'branch ls') o='-h --help --offline --json' ;;
    'branch clean') o='-h --help --offline --json --apply' ;;
    'branch embargo') o='-h --help --why --until --lift --json' v='--why=n --until=n' p='l:branches:-::' ;;
    'commit') o='-h --help -m --message --why --body --verified --refs --breaking -a --all --amend --wip --unwip' v='-m=n --message=n --why=n --body=n --verified=n --refs=n --breaking=n' p='*f' ;;
    'rebase') o='-h --help --base --continue --abort --skip' v='--base=l:branches:-::' ;;
    'push') o='-h --help --force-with-lease --ci-passed --allow-behind --dry-run' ;;
    'pr') c='create view ls checks merge ready comment review diff' o='-h --help' ;;
    'pr create') o='-h --help --title --base --draft --ready --fill --body-file --edit --ci-passed --dry-run --lang' v='--title=n --base=l:branches:-:: --body-file=f --lang=n' ;;
    'pr view') o='-h --help --body --json' p='n' ;;
    'pr ls') o='-h --help --all --all-projects --offline --json' ;;
    'pr checks') o='-h --help --watch --fail-fast --interval --timeout --lines --json' v='--interval=n --timeout=n --lines=n' p='n' ;;
    'pr merge') o='-h --help --squash --merge --rebase --auto --keep-branch --dry-run' p='n' ;;
    'pr ready') o='-h --help --ci-passed --lang' v='--lang=n' p='n' ;;
    'pr comment') o='-h --help -m --message --body-file' v='-m=n --message=n --body-file=f' p='n' ;;
    'pr review') o='-h --help -m --message --body-file --approve --request-changes --comment' v='-m=n --message=n --body-file=f' p='n' ;;
    'pr diff') o='-h --help --stat --patch --name-only --json' p='n' ;;
    'run') c='ls view rerun' o='-h --help' ;;
    'run ls') o='-h --help --branch --all-branches --limit --json' v='--branch=l:branches:-:: --limit=n' ;;
    'run view') o='-h --help --failed --lines --json' v='--lines=n' p='n' ;;
    'run rerun') o='-h --help --flaky --all-jobs' v='--flaky=n' p='n' ;;
    'release') o='-h --help --version --notes-file --ci-passed --sign --draft --prerelease --dry-run' v='--version=n --notes-file=f' ;;
    'issue') c='ls view create' o='-h --help' ;;
    'issue ls') o='-h --help --mine --label --state --limit --json' v='--label=n --state=c:open,closed,all --limit=n' ;;
    'issue view') o='-h --help --comments --json' p='n' ;;
    'issue create') o='-h --help --title --body --body-file --label --assignee' v='--title=n --body=n --body-file=f --label=n --assignee=n' ;;
    'git') o='-h --help -C' v='-C=d' p='*f' ;;
    'gh') o='-h --help' p='*f' ;;
    'test') c='run plan prove audit' o='-h --help --failed --affected --all --since --runner --json -v --verbose -p --project' v='--since=l:branches:-:: --runner=n -p=l:projects:-:: --project=l:projects:-::' ;;
    'test run') o='-h --help --failed --affected --all --since --runner --json -v --verbose' v='--since=l:branches:-:: --runner=n' p='*f' ;;
    'test plan') o='-h --help --all --since --runner --json' v='--since=l:branches:-:: --runner=n' ;;
    'test prove') o='-h --help --since --runs --runner --json' v='--since=l:branches:-:: --runs=n --runner=n' p='*f' ;;
    'test audit') o='-h --help --changed --since --json --min-severity --rule' v='--since=l:branches:-:: --min-severity=c:low,medium,high --rule=c:debug-output,duplicate-test,empty-test,mock-only,no-assertion,no-production-reference,skipped-without-reason,sleep-in-test,smoke-only,snapshot-only,swallowed-failure,trivial-assertion,vague-name' p='*f' ;;
    'mcp') o='-h --help' ;;
    'desktop') o='-h --help --no-validate --json' p='c:pack,status' ;;
    'crew') c='raci set history rules graph show brief sync ask answer inbox mode decide decisions route delegate decline next' o='-h --help --json --latest' ;;
    'crew raci') o='-h --help --set --why --markdown' v='--set=n --why=n' p='l:activities,crew:-::' ;;
    'crew set') o='-h --help --why' v='--why=n' p='l:crew:-:: n *n' ;;
    'crew history') o='-h --help' ;;
    'crew rules') o='-h --help' ;;
    'crew graph') o='-h --help --latest --mermaid --json' ;;
    'crew show') o='-h --help' p='*l:crew:-::' ;;
    'crew brief') o='-h --help --scope --step --activity' v='--scope=n --step=l:steps:-:: --activity=l:activities:-::' p='l:crew:-:: *n' ;;
    'crew sync') o='-h --help --check' ;;
    'crew ask') o='-h --help --to --decision --options --recommend --irreversible --target --performs --approves' v='--to=l:crew:-:: --options=n --recommend=n --irreversible=n --target=n --performs=n --approves=l:steps:-::' p='*n' ;;
    'crew answer') o='-h --help' p='n *n' ;;
    'crew inbox') o='-h --help' ;;
    'crew mode') o='-h --help --approve' v='--approve=n' p='c:guided,auto' ;;
    'crew decide') o='-h --help' p='n *n' ;;
    'crew decisions') o='-h --help --pending --ask --json' v='--ask=n' ;;
    'crew route') o='-h --help --files --plan --apply' v='--files=n' p='*n' ;;
    'crew delegate') o='-h --help --to --scope --after --estimate --files' v='--to=l:crew:-:: --scope=n --after=l:steps:-:: --estimate=n --files=n' p='*n' ;;
    'crew decline') o='-h --help --why' v='--why=n' p='*l:steps:-::' ;;
    'crew next') o='-h --help --first --why' v='--first=l:steps:-:: --why=n' ;;
    'impeccable') o='-h --help' p='*n' ;;
    'doctor') o='-h --help' ;;
    'install') o='-h --help --only --version --unverified --why --commit-template' v='--only=m:config,tools,cli,git,handoff,instructions,skills,claude,codex,desktop,docker,docs,launchd,prompt,shell --version=l:versions:-:: --why=n --commit-template=c:yes,no' ;;
    'docs') o='-h --help --online --fix --no-prose' p='c:check *f' ;;
    'sec') o='-h --help --history --config -v --verbose' v='--config=n' p='c:scan l:projects:-::' ;;
    'diagram') o='-h --help' p='*l:-:-:render,check,new:f' ;;
    'setup') o='-h --help --remove -y --yes --skip-packages' p='c:prompt,commit-template,shell' ;;
    'prompt') o='-h --help' p='c:refresh' ;;
    'update') o='-h --help' ;;
    'docker') o='-h --help' p='c:up,down,status,trim,prune,autostart-on,autostart-off' ;;
    'version') o='-h --help' ;;
    'completion') o='-h --help' p='c:zsh,bash' ;;
  esac
}

complete -F _dev dev
