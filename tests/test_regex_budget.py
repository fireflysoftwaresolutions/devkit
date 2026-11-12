# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Every regular expression compiled at module level in the modules the hooks run stays fast on hostile text. A hook has
# a time budget: past it the guard fails open and the call runs unjudged, so one pattern that backtracks (a lazy match
# that ends on a back-reference, an unanchored \b[\w.]+ that restarts at every word boundary) is a way around the guard.
# Each pattern runs over each input below, about 20 KB of what makes patterns backtrack, the way its module calls it
# (a pattern the modules only call with .match or .fullmatch is timed that way; any other use, or none seen, is timed
# with .search), and must finish within BOUND seconds. The runs happen in a worker process: one still running after
# HARD seconds is stopped there (a cubic pattern on 20 KB runs for hours) and the worker goes on with the next pattern.
# A pattern over the bound that is not fixed yet goes in KNOWN_SLOW with its follow-up: it is checked by an expected
# failure, so the rest stays guarded and its fix shows as an unexpected success.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_regex_budget
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

MODULES = ("guard", "hooks", "canary", "language", "identity", "scope", "gates")
BOUND = 0.2  # seconds for one search over one input
HARD = 1.0  # seconds after which a search is stopped and counted over the bound

N = 20000
INPUTS = {
    "backticks": "`" * N, "double quotes": '"' * N, "single quotes": "'" * N, "dots": "." * N,
    "a dotted word": "a." * (N // 2), "spaces": " " * N, "slashes": "/" * N, "dashes": "-" * N,
    "open parentheses": "(" * N, "parentheses": "()" * (N // 2), "command substitutions": "$(" * (N // 2),
    "a word repeated": "word " * (N // 5), "a crew name repeated": "Senior engineer (Neo) " * (N // 22),
    "one long word": "a" * N, "newlines": "\n" * N, "short lines": "a\n" * (N // 2), "backslashes": "\\" * N,
    "equals signs": "=" * N, "colons": ":" * N, "brackets": "[" * N, "braces": "{" * N, "angle brackets": "<" * N,
    "mixed punctuation": "a-b.c/d_e:f " * (N // 12), "code spans": "`a` " * (N // 4), "links": "http://a." * (N // 9),
    "options": "--x " * (N // 4), "tabs": "\t" * N, "pipes": "| " * (N // 2), "semicolons": "; " * (N // 2),
    "and-lists": "&& " * (N // 3), "dollar signs": "$" * N, "stars": "*" * N, "hashes": "#" * N,
    "at signs": "a@" * (N // 2), "digits": "1" * N, "commas": "a," * (N // 2),
    "quoted words": '"a" ' * (N // 4), "paths": "a/b/" * (N // 4), "env assignments": "A=b " * (N // 4),
}

# (module, pattern name) -> the follow-up that fixes it; checked by an expected failure until it is fixed. Reported
# to the tech lead in crew message M55, with the times measured on this Mac (Python 3.15).
KNOWN_SLOW = {
    ("guard", "_SED_EDIT"): "Security engineer: _SED_EDIT.match takes 55 s on 2.5 KB of spaces (the \\s* runs around "
                            "the optional sed address backtrack into each other); one whitespace run per gap",
    ("guard", "_EXPECT_RUN"): "Security engineer: _EXPECT_RUN.search is quadratic on newlines (\\s* after each "
                              "separator spans the lines after it); [ \\t]* within the line",
    ("hooks", "_HOOK_TEXT"): "Security engineer: _HOOK_TEXT.search is quadratic on quotes, backticks and parentheses "
                             "((?:\\S*/)? rescans to the end from each one); a path prefix that cannot start inside "
                             "a run of them",
    ("language", "_LOGLINE"): "tech lead: _LOGLINE.search is quadratic on newlines (^\\s+ and ^\\s* in multiline mode "
                              "span the lines after each line start); [ \\t] within the line",
    ("language", "_LABEL"): "tech lead: _LABEL.search is quadratic on newlines (^(\\s*[-*•] spans the lines after each "
                            "line start); [ \\t]* within the line",
    ("gates", "_LESSON_DONE"): "tech lead: _LESSON_DONE.search is quadratic on newlines (^\\s*✓ spans the lines after "
                               "each line start); [ \\t]* within the line",
}


def _walk(name, value, out, depth=0):
    if isinstance(value, re.Pattern):
        out.append((name, value))
    elif depth < 3 and isinstance(value, (tuple, list)):
        for i, x in enumerate(value):
            _walk(f"{name}[{i}]", x, out, depth + 1)
    elif depth < 3 and isinstance(value, dict):
        for k, x in value.items():
            _walk(f"{name}[{str(k)[:40]!r}]", x, out, depth + 1)


def patterns() -> list:
    """[(module, name, pattern, method)]: each module-level pattern once, under the first module that holds it, in a
    fixed order (the worker and the test count them the same way), with the method it is timed by."""
    from devkit import canary, gates, guard, hooks, identity, language, scope
    mods = {"guard": guard, "hooks": hooks, "canary": canary, "language": language, "identity": identity,
            "scope": scope, "gates": gates}
    source = "\n".join((LIB / "devkit" / f"{m}.py").read_text() for m in MODULES)
    seen, out = set(), []
    for m in MODULES:
        found = []
        for name, value in list(vars(mods[m]).items()):
            _walk(name, value, found)
        for name, p in found:
            if id(p) not in seen:
                seen.add(id(p))
                out.append((m, name, p, _method(source, name)))
    return out


def _method(source, name) -> str:
    """How the modules call pattern `name`: `match` or `fullmatch` when that is all they do with it, else `search`
    (a pattern in a tuple or passed along is searched)."""
    if "[" in name:
        return "search"
    used = set(re.findall(r"(?<![\w.])" + re.escape(name) + r"\.(\w+)\(", source))
    return used.pop() if len(used) == 1 and used <= {"match", "fullmatch"} else "search"


def _search_time(p, method, text) -> float:
    t = time.perf_counter()
    run = {"search": p.search, "match": p.match, "fullmatch": p.fullmatch}[method]
    run(text if isinstance(p.pattern, str) else text.encode())
    return time.perf_counter() - t


def _worker(indices) -> None:
    """Run each pattern of `indices` over each input; print `B <i> <input>` before and `T <i> <input> <seconds>`
    after, so the parent knows which run did not end."""
    found = patterns()
    for i in indices:
        p, method = found[i][2], found[i][3]
        for label, text in INPUTS.items():
            print(f"B {i} {label}", flush=True)
            secs = _search_time(p, method, text)
            for _ in range(2):  # a pause of a busy machine is not the pattern's: the fastest of three
                if secs <= BOUND:
                    break
                secs = min(secs, _search_time(p, method, text))
            print(f"T {i} {label}\t{secs:.4f}", flush=True)
    print("D", flush=True)


WORKERS = 4  # the patterns within budget are shared among this many workers that run at the same time


def _measure(found) -> dict:
    """-> {(index, input): seconds} for every pattern, a stopped run counted as HARD. Each known slow pattern has a
    worker of its own, so the runs it makes stop wait for no other pattern."""
    total = len(found)
    known = [i for i, (m, name, _, _) in enumerate(found) if (m, name) in KNOWN_SLOW]
    rest = [i for i in range(total) if i not in known]
    groups = [rest[k::WORKERS] for k in range(WORKERS)] + [[i] for i in known]
    times = {}
    threads = [threading.Thread(target=_measure_group, args=(g, times)) for g in groups if g]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    errors = [v for k, v in times.items() if k[0] == "error"]
    missing = sorted(set(range(total)) - {k[0] for k in times})
    if errors or missing:
        raise AssertionError("; ".join(errors) or f"no time measured for pattern(s) {missing[:10]}")
    return times


def _pump(stream, lines) -> None:
    for ln in stream:
        lines.put(ln)
    lines.put(None)


def _measure_group(indices, times: dict) -> None:
    while indices:
        start = indices[0]
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker",
                                 ",".join(str(i) for i in indices)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = queue.Queue()
        pump = threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True)
        pump.start()
        running, nxt = None, None
        while True:
            try:
                ln = lines.get(timeout=60 if running is None else HARD)
            except queue.Empty:
                proc.kill()
                proc.wait()
                if running is None:
                    times[("error", start)] = "the pattern worker printed nothing for 60 s"
                    pump.join(5)
                    proc.stdout.close()
                    return
                times[running] = HARD
                nxt = indices[indices.index(running[0]) + 1:]
                break
            if ln is None:
                proc.wait()
                if nxt is None:
                    times[("error", start)] = f"the pattern worker ended early (exit {proc.returncode})"
                    proc.stdout.close()
                    return
                break
            kind, _, rest = ln.rstrip("\n").partition(" ")
            if kind == "B":
                i, _, label = rest.partition(" ")
                running = (int(i), label)
            elif kind == "T":
                head, _, secs = rest.partition("\t")
                i, _, label = head.partition(" ")
                times[(int(i), label)] = float(secs)
                running = None
            elif kind == "D":
                nxt = []
        pump.join(5)
        proc.stdout.close()
        indices = nxt


_RESULT = {}


def result():
    if not _RESULT:
        _RESULT["patterns"] = patterns()
        _RESULT["times"] = _measure(_RESULT["patterns"])
    return _RESULT["patterns"], _RESULT["times"]


def _over(only_known: bool) -> list:
    found, times = result()
    out = []
    for (i, label), secs in sorted(times.items()):
        m, name, p, method = found[i]
        if secs > BOUND and ((m, name) in KNOWN_SLOW) == only_known:
            shown = p.pattern if isinstance(p.pattern, str) else p.pattern.decode("utf-8", "replace")
            out.append(f"{m}.{name}.{method}() on {label} ({len(INPUTS[label])} characters): "
                       f"{'stopped after ' if secs >= HARD else ''}{secs:.2f} s, pattern {shown[:100]!r}")
    return out


class RegexBudgetTests(unittest.TestCase):
    # the collector sees every module and the patterns held in tuples, lists and dicts, so the budget below cannot
    # pass by finding nothing
    def test_every_hook_module_has_its_patterns_collected(self):
        found, _ = result()
        self.assertEqual(sorted({m for m, _, _, _ in found}), sorted(MODULES))
        self.assertTrue(any("[" in name for _, name, _, _ in found), [n for _, n, _, _ in found][:20])

    def test_every_pattern_searches_each_hostile_input_within_its_budget(self):
        over = _over(only_known=False)
        self.assertEqual(over, [], f"{len(over)} search(es) over {BOUND} s; a hook past its time budget lets the "
                                   "call run unjudged:\n  " + "\n  ".join(over))


if KNOWN_SLOW:
    class KnownSlowPatternTests(unittest.TestCase):
        # each known slow pattern is still over the bound until its follow-up lands; an unexpected success says it
        # is fixed and its KNOWN_SLOW entry can go
        @unittest.expectedFailure
        def test_known_slow_patterns_search_within_their_budget(self):
            over = _over(only_known=True)
            self.assertEqual(over, [], "known slow, follow-ups: " + "; ".join(sorted(set(KNOWN_SLOW.values()))))


if __name__ == "__main__" and sys.argv[1:2] == ["--worker"]:
    _worker([int(i) for i in sys.argv[2].split(",")])
