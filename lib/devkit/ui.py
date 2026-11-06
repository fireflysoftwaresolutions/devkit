# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Terminal output: colours, the banner, tables. Plain text when not a TTY or NO_COLOR is set."""
from __future__ import annotations

import os
import re
from pathlib import Path
import shutil
import sys

_TTY = sys.stdout.isatty()
COLOR = _TTY and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
TRUECOLOR = COLOR and os.environ.get("COLORTERM", "") in ("truecolor", "24bit")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _c(code):
    return (lambda s: f"\x1b[{code}m{s}\x1b[0m") if COLOR else (lambda s: str(s))


bold, dim, italic = _c("1"), _c("2"), _c("3")
red, green, yellow, blue, magenta, cyan = _c("31"), _c("32"), _c("33"), _c("34"), _c("35"), _c("36")
gray = _c("90")

OK, FAIL, WARN, INFO, ARROW = green("✓"), red("✗"), yellow("!"), cyan("•"), cyan("→")
ACTIVE, IDLE = green("●"), gray("○")

BANNER = [
    "██████╗ ███████╗██╗   ██╗██╗  ██╗██╗████████╗",
    "██╔══██╗██╔════╝██║   ██║██║ ██╔╝██║╚══██╔══╝",
    "██║  ██║█████╗  ██║   ██║█████╔╝ ██║   ██║   ",
    "██║  ██║██╔══╝  ╚██╗ ██╔╝██╔═██╗ ██║   ██║   ",
    "██████╔╝███████╗ ╚████╔╝ ██║  ██╗██║   ██║   ",
    "╚═════╝ ╚══════╝  ╚═══╝  ╚═╝  ╚═╝╚═╝   ╚═╝   ",
]
# The firefly (Firefly Software Solutions) and its colour mask:
#   a antenna · w wing · e eye · b body · g glow · s spark
FIREFLY = [
    "    ╲  ╱    ",
    " ▗▄▖ ◉◉ ▗▄▖ ",
    " ▝▀▘▐██▌▝▀▘ ",
    "    ▐▓▓▌    ",
    "     ▀▀  ✦  ",
    "   ·   ˚    ",
]
FIREFLY_MASK = [
    "    a  a    ",
    " www ee www ",
    " wwwbbbbwww ",
    "    bggb    ",
    "     gg  s  ",
    "   s   s    ",
]
TRAIL = "·   ˚  ✦   ⋆  ·  ✧"
_PAL24 = {"a": (140, 140, 165), "w": (185, 228, 255), "e": (255, 255, 255), "b": (96, 96, 124),
          "g": (205, 255, 70), "s": (255, 214, 90),
          # Firefly brand: amber is the light (the spark), paper and charcoal the ink, stone the quiet text
          "amber": (255, 179, 74), "paper": (243, 241, 235), "charcoal": (16, 17, 15), "stone": (191, 184, 171)}
_PAL256 = {"a": 245, "w": 153, "e": 231, "b": 240, "g": 154, "s": 221}
# firefly lime -> cyan -> violet
_STOPS = [(190, 255, 60), (0, 214, 255), (170, 90, 255)]
_LINE256 = [154, 49, 45, 39, 63, 99]
_SHADOW = set("╗╔╝╚║═")
FRAME_INNER = 68
TAGLINE = "Every repo in its place. Every agent on the same page."
TIPS = [
    "`dcd <query>` jumps into any project; `dwt <branch>` creates a worktree and jumps into it.",
    "`dev claude -w feat/x` starts Claude Code in its own worktree: parallel work without sibling copies.",
    "`dev ci` shows exactly what CI runs. Run it locally, then push once: one push, one CI run.",
    "`dev gc` is a dry run; it only ever removes work that is already safe on the remote.",
    "Say “handoff” to Claude or Codex before you stop: the note is waiting for the next session.",
    "`dev doctor` checks git, gh, Claude, Codex, skills, Docker and the background sync in one go.",
    "Worktrees reuse node_modules copy-on-write when the lockfile matches: no reinstall, ~0 bytes.",
    "Personal agent rules go in ~/.config/devkit/AGENTS.personal.md; `dev install` merges them.",
    "`dev docker up` starts Colima with Rosetta, so amd64 images run on Apple silicon.",
    "`dev canary` shows whether every Claude/Codex reply started with “[Firefly> DevKit]”, proof they followed the rules.",
    "Branches are `<type>/<ticket>-<topic>`: feat/PROJ-142-invoice-export, fix/login-timeout, release/26.11.4.",
]


def width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def _grad(t: float, k: float = 1.0):
    t = min(1.0, max(0.0, t))
    seg = 0 if t < 0.5 else 1
    u = t * 2 - seg
    a, b = _STOPS[seg], _STOPS[seg + 1]
    return tuple(int((a[i] + (b[i] - a[i]) * u) * k) for i in range(3))


def _fg(rgb, text, bold_=False):
    return f"\x1b[{'1;' if bold_ else ''}38;2;{rgb[0]};{rgb[1]};{rgb[2]}m{text}\x1b[0m"


def _gradient(line: str, t0: float = 0.0, t1: float = 1.0) -> str:
    if not TRUECOLOR:
        return line
    n = len(line)
    return "".join(_fg(_grad(t0 + (t1 - t0) * (i / max(1, n - 1))), ch) for i, ch in enumerate(line))


def logo_inline() -> str:
    """Compact one-line logo for narrow terminals and sub-headers."""
    s = "✦ devkit"
    if TRUECOLOR:
        return _fg(_PAL24["amber"], "✦", True) + " " + bold("devkit")
    return bold(cyan(s)) if COLOR else s


ART_DIR = Path(__file__).resolve().parent / "art"


def terminal_theme() -> str:
    """dark | light — DEVKIT_THEME wins; else COLORFGBG ("fg;bg", bg 7/15 = light); default dark."""
    t = os.environ.get("DEVKIT_THEME", "").lower()
    if t in ("dark", "light"):
        return t
    bg = os.environ.get("COLORFGBG", "").split(";")[-1]
    return "light" if bg in ("7", "15") else "dark"


# The devkit wordmark in half blocks, drawn by hand on the Manrope letterforms; ✦ (amber) is the dot of the i.
WORDMARK = [
    "     ██                 ██       ✦  ██  ",
    " ▄▄▄▄██  ▄▄▄▄▄  ▄▄   ▄▄ ██  ▄▄  ▄▄ ▄██▄▄",
    "██   ██ ██▄▄▄██ ██   ██ ██▄█▀   ██  ██  ",
    "██   ██ ██▀▀▀▀▀ ▀█▄ ▄█▀ ███▄    ██  ██  ",
    "▀█▄▄▄██ ▀█▄▄▄▄   ▀███▀  ██ ▀█▄  ██  ▀█▄▄",
]


def wordmark() -> list:
    """The wordmark rows, inked for the terminal's theme (paper on dark, charcoal on light)."""
    dark = terminal_theme() == "dark"
    if TRUECOLOR:
        ink = _PAL24["paper"] if dark else _PAL24["charcoal"]
        return [_fg(ink, r.replace("✦", "\x00"), False).replace("\x00", _fg(_PAL24["amber"], "✦", True) +
                                                                       f"\x1b[38;2;{ink[0]};{ink[1]};{ink[2]}m")
                for r in WORDMARK]
    if COLOR:
        return [r.replace("✦", yellow("✦")) for r in WORDMARK]
    return list(WORDMARK)


def banner(context: str = "", framed: bool = True) -> str:
    """Welcome: the devkit wordmark, the tagline, then version, context and copyright in one quiet line."""
    from .core import COPYRIGHT, VERSION
    meta = f"v{VERSION}" + (f" · {context}" if context else "")
    if width() < len(WORDMARK[0]) + 6:
        return f"\n  {logo_inline()}  {dim(meta)}\n"
    out = [""] + ["  " + r for r in wordmark()] + [""]
    out.append("  " + (_fg(_PAL24["stone"], TAGLINE, False) if TRUECOLOR else TAGLINE))
    out.append("  " + dim(f"{meta}  ·  {COPYRIGHT}"))
    return "\n".join(out) + "\n"


def greeting(name: str = "") -> str:
    import datetime as _dt
    h = _dt.datetime.now().hour
    part = "Good morning" if 5 <= h < 12 else "Good afternoon" if h < 19 else "Good evening"
    first = (name or "").split()[0] if name else ""
    return f"{part}{', ' + first if first else ''}."


def tip() -> str:
    import random
    return random.choice(TIPS)


def visible_len(s: str) -> int:
    return len(ANSI_RE.sub("", str(s)))


def trunc(s: str, n: int) -> str:
    s = str(s)
    if visible_len(s) <= n:
        return s
    plain = ANSI_RE.sub("", s)
    return plain[: max(1, n - 1)] + "…"


def table(rows, headers, max_widths=None, indent=2) -> str:
    """Aligned columns; ANSI-aware. max_widths: {col_index: n} truncates long cells."""
    max_widths = max_widths or {}
    rows = [[str(c) if c is not None else "" for c in r] for r in rows]
    rows = [[trunc(c, max_widths[i]) if i in max_widths else c for i, c in enumerate(r)] for r in rows]
    widths = [visible_len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], visible_len(c))
    pad = " " * indent

    def fmt(cells, style=None):
        parts = []
        for i, c in enumerate(cells):
            c = str(c)
            parts.append(c + " " * (widths[i] - visible_len(c)))
        line = pad + "  ".join(parts).rstrip()
        return style(line) if style else line

    out = [fmt(headers, lambda s: bold(s)), pad + dim("  ".join("─" * w for w in widths))]
    out += [fmt(r) for r in rows]
    return "\n".join(out)


def section(title: str) -> str:
    """A heading: the title in bold and a quiet rule to the right (no marker, so it never reads as a selection)."""
    total = min(width() - 4, 78)  # every heading's rule ends in the same column
    rule = "─" * max(3, total - visible_len(title) - 1)
    return "\n  " + bold(title) + " " + dim(rule)


def kv(key: str, value, key_width=14) -> str:
    return f"  {dim(key.ljust(key_width))} {value}"


def err(msg: str):
    print(f"{FAIL} {msg}", file=sys.stderr)


def warn(msg: str):
    print(f"{WARN} {msg}", file=sys.stderr)


def core_cfg() -> dict:
    try:
        from . import core
        return core.CFG
    except Exception:
        return {}


def status_pills(line: str, color: bool = None) -> str:
    """A saved [Firefly> DevKit] status line drawn like a shell prompt: colored pills, then the segments."""
    from .canary import segments
    segs = segments(line or "")
    if not segs:
        return ""
    color = (sys.stdout.isatty() and not os.environ.get("NO_COLOR")) if color is None else color
    if not color:
        return "[Firefly> DevKit] " + " ❯ ".join(segs)

    def pill(text, bg, fg):
        return f"\033[48;2;{bg[0]};{bg[1]};{bg[2]}m\033[38;2;{fg[0]};{fg[1]};{fg[2]}m {text} \033[0m"

    amber, charcoal, ink, paper, graphite = (255, 179, 74), (16, 17, 15), (39, 40, 32), (243, 241, 235), (71, 74, 66)
    spec = [("Firefly❯ DevKit", amber, charcoal), (segs[0], ink, paper)]
    rest = segs[1:]
    current = " · " in segs[0] and rest and rest[-1].lower().startswith("next")  # status-block form
    if rest and (current or rest[0].startswith(("T-", "F-", "B-", "S-")) or rest[0] == "-"):
        if rest[0] != "-":
            spec.append((rest[0], graphite, paper))
        rest = rest[1:]
    if (core_cfg().get("ui") or {}).get("powerline"):  # Nerd Font arrows between pills, like powerlevel10k
        pills = []
        for i, (txt, bg, fg) in enumerate(spec):
            pills.append(pill(txt, bg, fg))
            nxt = spec[i + 1][1] if i + 1 < len(spec) else None
            arrow = f"\033[38;2;{bg[0]};{bg[1]};{bg[2]}m" + (f"\033[48;2;{nxt[0]};{nxt[1]};{nxt[2]}m" if nxt else "")
            pills.append(arrow + "\ue0b0\033[0m")
    else:
        pills = [pill(txt, bg, fg) for txt, bg, fg in spec]
    styled = []
    for s_ in rest:
        g = s_[:1]
        styled.append(green(s_) if g == "✓" else yellow(s_) if g == "⚠" else red(s_) if g == "✗" else
                      cyan(s_) if s_.lower().startswith("next") else dim(s_) if g in "▸–" else s_)
    return "".join(pills) + ("  " + dim(" ❯ ").join(styled) if styled else "")


def status_block(line: str, color: bool = None, indent: str = "  ") -> str:
    """A saved status drawn as the block agents write: a header pill row, then Done / Check / Progress / Next."""
    from .canary import segments
    segs = segments(line or "")
    if not segs:
        return ""
    current = " · " in segs[0] and len(segs) >= 3 and segs[-1].lower().startswith("next")
    if not current:
        return indent + status_pills(line, color)
    color = (sys.stdout.isatty() and not os.environ.get("NO_COLOR")) if color is None else color
    where, task, rest = segs[0], segs[1], segs[2:]
    nxt = rest.pop()[len("next →"):].strip() if rest and rest[-1].lower().startswith("next") else ""
    prog = next((x[1:].strip() for x in rest if x.startswith("▸")), "")
    rest = [x for x in rest if not x.startswith("▸")]
    check = next((x for x in rest if x[:1] in "✓⚠✗–"), "")
    done = " ".join(x for x in rest if x != check)
    head = brand_pills([where] + ([task] if task != "-" else []), color)
    rows = [("Done", done), ("Check", check), ("Progress", prog), ("Next", nxt)]
    out = [indent + head]
    for k, val in rows:
        if not val:
            continue
        if color and k == "Check":
            val = green(val) if val[:1] == "✓" else yellow(val) if val[:1] == "⚠" else red(val) if val[:1] == "✗" else val
        out.append(f"{indent}  {dim(k.ljust(9)) if color else k.ljust(9)} {val}")
    return "\n".join(out)



def brand_pills(parts, color: bool = None) -> str:
    """`Firefly❯ DevKit` followed by one pill per part (project · branch, task), like a shell prompt."""
    color = (sys.stdout.isatty() and not os.environ.get("NO_COLOR")) if color is None else color
    if not color:
        return "[Firefly> DevKit] " + " · ".join(parts)
    amber, charcoal, ink, paper, graphite = (255, 179, 74), (16, 17, 15), (39, 40, 32), (243, 241, 235), (71, 74, 66)
    spec = [("Firefly❯ DevKit", amber, charcoal)] + [(p, ink if i == 0 else graphite, paper) for i, p in enumerate(parts)]
    arrows = (core_cfg().get("ui") or {}).get("powerline")
    out = []
    for i, (txt, bg, fg) in enumerate(spec):
        out.append(f"\033[48;2;{bg[0]};{bg[1]};{bg[2]}m\033[38;2;{fg[0]};{fg[1]};{fg[2]}m {txt} \033[0m")
        if arrows:
            nxt = spec[i + 1][1] if i + 1 < len(spec) else None
            out.append(f"\033[38;2;{bg[0]};{bg[1]};{bg[2]}m" + (f"\033[48;2;{nxt[0]};{nxt[1]};{nxt[2]}m" if nxt else "")
                       + "\ue0b0\033[0m")
    return "".join(out)
