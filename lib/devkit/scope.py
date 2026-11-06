# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Is this turn development work?

devkit's process (the status block, required skills, progress nudges, lesson reminders) is for development:
writing, reviewing, debugging, testing, building, deploying or releasing software, operating developer tools,
repositories, CI and devkit, and questions about a codebase. Anything else (a recipe, a general question,
consulting work done with the Firefly consulting kit) gets a normal answer and devkit stays out of the way.

Decided per turn, in two steps:
  prompt   classify(prompt) -> "dev" | "other" | None. A follow-up with no signal ("continue", "yes") inherits
           the previous turn's class.
  repo     with no signal at all, a session inside a repository counts as development ("how is it going?").
  actions  at Stop, what the turn did. A turn of unknown class becomes development when it edited files in a
           repository or ran development commands; an "other" turn only through strong actions (commits, pushes,
           tags, test or build runs), so a deck built inside a repository stays consulting work.
"""
from __future__ import annotations

import json
import os
import re

DEV, OTHER = "dev", "other"

_STRONG = (
    # English
    r"code|codebase|coding|bugs?|bugfix|debug\w*|stack ?traces?|tracebacks?|exceptions?|deploy\w*|pull requests?|"
    r"PRs?|commits?|branch(es)?|rebase|merge conflicts?|refactor\w*|unit tests?|test suites?|tests? (fail|pass)\w*|"
    r"failing tests?|compil\w*|lint\w*|type ?check\w*|CI|CD|pipelines?|endpoints?|APIs?|migrations?|schemas?|SQL|"
    r"docker\w*|kubernetes|k8s|helm|terraform|git|github|gitlab|bitbucket|repos?|repository|repositories|devkit|"
    r"npm|pnpm|yarn|maven|mvn|gradle|pip|uv|python|java|kotlin|typescript|javascript|node(\.?js)?|angular|react|"
    r"next\.?js|vue|spring( boot)?|php|laravel|symfony|golang|rust|swift|backend|frontend|front-end|back-end|"
    r"scripts?|CLI|worktrees?|handoff|canary|hooks?|plugins?|MCP|runners?|workflows?|github actions|releases?|"
    r"changelog|semver|calver|regex|json|yaml|toml|html|css|tailwind|dashboard|components?|functions?|methods?|"
    r"classes|modules?|refactoring|dependenc(y|ies)|packages?|build(s|ing)? (fails?|breaks?|errors?)|"
    r"implement\w*|install\w*|configur\w*|set ?up|setup|server|servers|database|db|query|queries|"
    r"claude code|codex|subagents?|skills?|AGENTS\.md|CLAUDE\.md|README|"
    # Spanish
    r"c[oó]digo|programa(r|ción|cion)|depura\w*|despleg\w*|despliegue\w*|ramas?|compila\w*|implementa\w*|"
    r"arregla\w*|fallos?|errores?|pruebas?|instala\w*|configura\w*|servidor(es)?|base de datos|migraci[oó]n\w*|"
    r"refactoriza\w*|versi[oó]n(es)?|lanzamiento|integraci[oó]n continua|extensi[oó]n(es)?|componentes?|"
    r"funci[oó]n(es)?|m[oó]dulos?|tareas de desarrollo|desarroll\w*")

_OTHER = (
    # cooking, home, leisure
    r"recetas?|recipes?|cocina\w*|cook\w*|ingredientes?|ingredients?|horno|oven|postres?|desserts?|guisos?|"
    r"tortilla|paella|pasta|pizza|comidas?|meals?|cenas?|dinner|desayunos?|breakfast|restaurantes?|restaurants?|"
    r"vino|wine|viajes?|trips?|travel\w*|vacaciones|holidays?|hotel(es|s)?|vuelos?|flights?|pel[ií]culas?|movies?|"
    r"series de tv|novelas?|poemas?|poems?|chistes?|jokes?|regalos?|gifts?|deportes?|f[uú]tbol|gimnasio|workout|"
    # consulting deliverables (the Firefly consulting kit)
    r"decks?|slides?|diapositivas?|presentaci[oó]n(es)?|presentations?|powerpoint|pptx|docx|one-?pagers?|"
    r"propuestas? (comercial(es)?|para|de colaboraci[oó]n)|proposals? for|statements? of work|SOW|co-?brand\w*|"
    r"workshops?|talleres?|engagements?|consultor[ií]a|consulting (work|project|deliverable)s?|"
    r"informes? para (el )?cliente|business case|plan de negocio|estrategia (comercial|de negocio)|"
    r"pitch|due diligence|go-to-market|modelo operativo|operating model")

STRONG_RE = re.compile(r"(?<![\w-])(" + _STRONG + r")(?![\w-])", re.I)
OTHER_RE = re.compile(r"(?<![\w-])(" + _OTHER + r")(?![\w-])", re.I)
FOLLOW_UP_RE = re.compile(r"^\W*(s[ií]|yes|ok(ay)?|vale|go|dale|adelante|contin[uú]a\w*|continue|sigue|seguimos|"
                          r"perfecto|perfect|gracias|thanks?|thank you|de acuerdo|hazlo|do it|please continue)\b", re.I)
CONSULTING_SKILL_RE = re.compile(r"\bfirefly-(consult|deck|document|pitch|cobrand|review|event|storytelling|proposal|"
                                 r"research|problem-solving|business-design|transformation|architecture|"
                                 r"tech-documentation|house-style)\b")
# a path, a file name with a code extension or a stack-trace line is a development signal by itself
CODE_HINT_RE = re.compile(r"((~|\.{1,2})/[\w.-]+|\b[\w.-]+/[\w.-]+/[\w.-]+|\b\w+\.(py|ts|tsx|js|jsx|java|kt|go|rs|rb|"
                          r"php|cs|swift|sql|sh|ya?ml|toml|json|gradle|xml|vue|svelte|css|scss)\b|Traceback|"
                          r"\bat \w+\.\w+\()")

STRONG_CMD_RE = re.compile(
    r"\bgit\s+(-C\s+\S+\s+)?(commit|push|merge|tag|rebase|cherry-pick|revert|checkout\s+-b|switch\s+-c|worktree)\b|"
    r"\bgh\s+(pr|release|run|workflow)\b|"
    r"(^|[\s;&|(])(pytest|jest|vitest|mocha|tsc|eslint|ruff|mypy|phpunit|make|docker|kubectl|terraform|helm|"
    r"\./mvnw|mvn|\./gradlew|gradle|cargo|go\s+(test|build)|npm\s+(test|run|ci|install)|pnpm|yarn|composer|"
    r"python3?\s+-m\s+(unittest|pytest|compileall)|uv\s+(run|sync)|ng\s+(build|test|serve|generate))\b|"
    r"\bdev\s+(commit|push|pr|release|branch|rebase|ci|sec|docs|install|setup|wt|delivery|lesson|task|plan|"
    r"progress)\b")
WEAK_CMD_RE = re.compile(r"\bgit\b|\bdev\b|\bgh\b|\bnode\b|\bnpm\b|\bpython3?\b|\brg\b|\bgrep\b")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
SHELL_TOOLS = {"Bash", "exec_command", "shell", "local_shell", "unified_exec"}


def classify(prompt: str) -> str | None:
    """The request's class from its words: DEV, OTHER, or None when it gives no signal."""
    text = (prompt or "").strip()
    if not text:
        return None
    strong = STRONG_RE.search(text) or CODE_HINT_RE.search(text)
    other = OTHER_RE.search(text) or CONSULTING_SKILL_RE.search(text)
    if other and not strong:
        return OTHER
    if strong and other:  # "fix the deck renderer bug" is development; "a deck about our API strategy" is not
        bug_words = re.search(r"(?<![\w-])(bugs?|fix\w*|arregla\w*|debug\w*|depura\w*|errores?|fallos?|tests?|"
                              r"pruebas?|commit\w*|deploy\w*|despleg\w*|refactor\w*|c[oó]digo|code|"
                              r"implement\w*)(?![\w-])", text, re.I) or CODE_HINT_RE.search(text)
        return DEV if bug_words else OTHER
    if strong:
        return DEV
    return None  # no signal: a follow-up ("continue", "yes") or a general question


def resolve(prompt: str, previous: str | None) -> str | None:
    """The turn's class: the prompt's own, or the previous turn's for a follow-up with no signal."""
    return classify(prompt) or previous


def _tail_lines(path: str, max_bytes: int = 3_000_000) -> list:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            return fh.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return []


def _is_prompt(rec: dict) -> bool:
    """A real user prompt (not a tool result) in a Claude or Codex transcript."""
    if rec.get("type") == "user":
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return True
        if isinstance(content, list):
            return not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
    p = rec.get("payload") or {}
    return p.get("type") == "user_message"


def turn_actions(transcript_path: str) -> dict:
    """What the last turn did: {"edits": [paths], "commands": [str], "skills": [names], "tools": {names}}."""
    out = {"edits": [], "commands": [], "skills": [], "tools": set()}
    recs = []
    for ln in _tail_lines(transcript_path):
        try:
            recs.append(json.loads(ln))
        except ValueError:
            continue
    start = max((i for i, r in enumerate(recs) if _is_prompt(r)), default=-1)
    for r in recs[start + 1:]:
        calls = []
        for c in ((r.get("message") or {}).get("content") or []) if r.get("type") == "assistant" else []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                calls.append((c.get("name") or "", c.get("input") or {}))
        p = r.get("payload") or {}
        if p.get("type") in ("function_call", "custom_tool_call"):
            args = p.get("arguments") if p.get("type") == "function_call" else p.get("input")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"input": args}
            calls.append((p.get("name") or "", args or {}))
        for name, inp in calls:
            out["tools"].add(name)
            if name in EDIT_TOOLS:
                fp = inp.get("file_path") or inp.get("notebook_path") or ""
                if not fp and isinstance(inp.get("input"), str):  # Codex apply_patch envelope
                    fp = " ".join(re.findall(r"\*\*\* (?:Add|Update|Delete) File: (\S+)", inp["input"]))
                out["edits"].append(fp)
            elif name in SHELL_TOOLS:
                cmd = inp.get("command") or inp.get("cmd") or ""
                out["commands"].append(" ".join(cmd) if isinstance(cmd, list) else str(cmd))
            elif name == "Skill":
                out["skills"].append(str(inp.get("skill") or ""))
    return out


def _in_repo(path: str) -> bool:
    d = os.path.dirname(os.path.abspath(os.path.expanduser(path))) if path else ""
    while d and d != os.path.dirname(d):
        if os.path.exists(os.path.join(d, ".git")):
            return True
        d = os.path.dirname(d)
    return False


def is_dev_turn(cls: str | None, actions: dict) -> bool:
    """Combine the prompt's class with what the turn did."""
    cmds = actions.get("commands") or []
    strong = any(STRONG_CMD_RE.search(c) for c in cmds)
    consulting = any(CONSULTING_SKILL_RE.search(s or "") for s in actions.get("skills") or []) or \
        any("firefly" in t.lower() and t.startswith("mcp__") for t in actions.get("tools") or ())
    if cls == DEV:
        return True
    if cls == OTHER or consulting:
        return strong
    edits = [e for e in actions.get("edits") or [] if e and _in_repo(e)]
    return strong or bool(edits) or any(WEAK_CMD_RE.search(c) for c in cmds)


def decide(data: dict, sess: dict) -> bool:
    """The Stop hook's question: should devkit's process (status block, skills) apply to the turn that just ended?"""
    if os.environ.get("DEVKIT_SCOPE") == "all":
        return True
    cls = sess.get("turn_class")
    if cls is None and sess.get("top"):  # no signal inside a repository ("how is it going?"): development
        cls = DEV
    return is_dev_turn(cls, turn_actions(data.get("transcript_path")))
