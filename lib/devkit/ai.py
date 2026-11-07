# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Background AI, opt-in: jobs that run with no model in the loop ask the installed agent, headless.

devkit never decides what a developer meant by matching words (crew gap review, C13). In a session the model
understands any language and declares ids. A background job (`dev sleep` merging duplicate lessons written in
different languages) has no model, so when the developer turns this on (`dev setup` asks once), it asks one:

  claude   `claude -p --model haiku`, with no tools, nothing saved, and devkit's hooks told to stay out
  codex    `codex exec -m gpt-6-luna --sandbox read-only`, nothing saved

Every call runs outside any repository, has a timeout, and counts against a daily budget (`ai.daily_calls`). Off,
over budget, or failing, `ask` answers None and the caller compares language-neutral anchors instead.

Config (`ai` in devkit's config): enabled (false), agent (claude | codex; default: the first one installed),
model.claude, model.codex, daily_calls (20), timeout (60 seconds).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import core
from .core import STATE

USAGE = STATE / "ai-usage.json"
DEFAULTS = {"enabled": False, "agent": "", "model": {"claude": "haiku", "codex": "gpt-6-luna"}, "daily_calls": 20,
            "timeout": 60}


def config() -> dict:
    return core.deep_merge(DEFAULTS, core.CFG.get("ai") or {})


def enabled() -> bool:
    return bool(config().get("enabled"))


def agent() -> str:
    """The agent that answers: the configured one, else the first one installed ('' when none is)."""
    want = str(config().get("agent") or "").strip()
    if want in ("claude", "codex"):
        return want
    return next((x for x in ("claude", "codex") if core.have(x)), "")


def command(who: str, model: str, out_file: str = "") -> list:
    """The headless call that reads its prompt from stdin."""
    if who == "claude":
        argv = ["claude", "-p", "--model", model, "--tools", "", "--no-session-persistence", "--output-format", "text"]
        if os.environ.get("ANTHROPIC_API_KEY"):
            argv.append("--bare")  # no hooks or plugins at all; it needs an API key, not a subscription login
        return argv
    return ["codex", "exec", "-m", model, "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
            "-o", out_file, "-"]


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def used(now: float | None = None) -> int:
    """Calls made today."""
    return int((core.read_json(USAGE, {}) or {}).get(_day(now or time.time())) or 0)


def _count(now: float):
    day = _day(now)
    u = core.read_json(USAGE, {}) or {}
    u = {k: v for k, v in u.items() if k >= _day(now - 30 * 86400)}  # a month of history is enough
    u[day] = int(u.get(day) or 0) + 1
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    USAGE.write_text(json.dumps(u, indent=2) + "\n")


def ask(prompt: str, purpose: str, now: float | None = None):
    """The model's answer, or None (off, no agent, over today's budget, or the call failed). `purpose` is logged."""
    now = now or time.time()
    cfg = config()
    if not cfg.get("enabled"):
        return None
    who = agent()
    if not who or used(now) >= int(cfg.get("daily_calls") or 0):
        return None
    _count(now)  # a call that fails still costs: count it before it runs
    model = str((cfg.get("model") or {}).get(who) or DEFAULTS["model"][who])
    env = dict(os.environ, DEVKIT_HEADLESS="1")
    with tempfile.TemporaryDirectory(prefix="devkit-ai-") as cwd:
        out_file = str(Path(cwd) / "answer.txt")
        try:
            res = subprocess.run(command(who, model, out_file), input=prompt, capture_output=True, text=True,
                                 cwd=cwd, env=env, timeout=float(cfg.get("timeout") or 60))
        except (OSError, subprocess.SubprocessError):
            _log(purpose, who, "failed to run")
            return None
        if res.returncode != 0:
            _log(purpose, who, f"exit {res.returncode}")
            return None
        text = Path(out_file).read_text() if who == "codex" and Path(out_file).exists() else res.stdout
    _log(purpose, who, "ok")
    return text.strip() or None


def _log(purpose: str, who: str, outcome: str):
    try:
        log = STATE / "logs" / "ai.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M')} {who} {purpose}: {outcome}\n")
    except OSError:
        pass


def same_mistake(a: str, b: str):
    """Do two lessons, in any language, describe the same mistake? True, False, or None (no answer)."""
    ans = ask("Two lessons from a software team follow, possibly in different languages. Do they describe the same "
              "mistake, with the same root cause? Answer with one word: yes or no.\n\n"
              f"Lesson A:\n{a.strip()[:2000]}\n\nLesson B:\n{b.strip()[:2000]}\n", "lessons: same mistake?")
    if ans is None:
        return None
    word = ans.strip().split()[0].strip(".,!").lower() if ans.strip() else ""
    return True if word == "yes" else False if word == "no" else None
