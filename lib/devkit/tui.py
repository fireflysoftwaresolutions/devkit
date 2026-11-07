# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A small terminal UI toolkit for `dev setup`: select, multiselect, confirm, text, a step header and a spinner.

Stdlib only (termios, select and ANSI escapes), Python 3.9 compatible, no curses. Each prompt is a small state
machine: `handle(key)` takes a key name and `lines(width)` returns what to draw, so prompts can be tested by
feeding them keys. Two drivers draw them: `Screen` repaints a full screen in the alternate buffer, and
`ask_inline` redraws one line in the normal buffer. `Checklist` shows long steps with a spinner and ✓/✗.

Ctrl-C raises `Cancelled`, the terminal is always restored (try/finally), lines are cut to the terminal width so
nothing wraps, and `available()` says whether this terminal can run it at all. Callers fall back to plain line
prompts when it cannot (not a TTY, TERM=dumb).
"""
from __future__ import annotations

import os
import re
import select as _select
import signal
import sys
import textwrap
import threading
import time
import unicodedata
from contextlib import contextmanager

try:
    import termios
except ImportError:  # pragma: no cover - not on macOS
    termios = None

from . import ui

# --------------------------------------------------------------------------- text and style

ANSI_ANY = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
POINTER, ON, OFF = "❯", "■", "□"


def strip(s) -> str:
    """Text without any escape sequence."""
    return ANSI_ANY.sub("", str(s))


def plain(s) -> str:
    """Text from outside (names, versions, command output) made safe to draw: no escapes, no control characters."""
    return _CONTROL.sub("", strip(s))


def clean(s) -> str:
    """plain(), without leading and trailing spaces."""
    return plain(s).strip()


def _cw(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(s) -> int:
    """Columns the text takes on screen."""
    return sum(_cw(c) for c in strip(s))


def fit(s, width: int) -> str:
    """Cut to `width` columns (escape sequences kept, '…' marks the cut), so a line never wraps."""
    s = str(s)
    if vlen(s) <= width:
        return s
    out, used, i = [], 0, 0
    limit = max(0, width - 1)
    while i < len(s):
        m = ANSI_ANY.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        w = _cw(s[i])
        if used + w > limit:
            break
        out.append(s[i])
        used += w
        i += 1
    return "".join(out) + ("…" if width > 0 else "") + ("\x1b[0m" if "\x1b[" in s else "")


def pad(s, width: int) -> str:
    return str(s) + " " * max(0, width - vlen(s))


def wrap(text: str, width: int) -> list:
    return textwrap.wrap(str(text), max(10, width)) or [""]


class _Style:
    """Colors for the toolkit: amber for focus and the primary action, dim for help, green and red for states.

    When the terminal does not say its theme (ui.known_theme(); macOS Terminal sets no COLORFGBG), the marks (pointer,
    bar, spinner) take the deep amber, 5.0:1 on white and 4.2:1 on black (index 130: 4.7:1 and 4.46:1), and the
    focused text and the chosen action stay bold in the terminal's own text color, which reads on its background."""

    def __init__(self):
        self.set()

    def set(self, color=None, truecolor=None, light=None):
        self.color = ui.COLOR if color is None else bool(color)
        self.true = (ui.TRUECOLOR if truecolor is None else bool(truecolor)) and self.color
        theme = ui.known_theme() if light is None else ("light" if light else "dark")
        self.light = theme == "light"
        deep = self.light or not theme
        if not self.color:
            self.amber = ""
        elif self.true:
            self.amber = "38;2;163;95;0" if deep else "38;2;255;179;74"
        else:
            self.amber = "38;5;130" if deep else "38;5;215"
        self.amber_text = self.amber if theme else ""

    def _c(self, code, s):
        return f"\x1b[{code}m{s}\x1b[0m" if self.color and code else str(s)


STYLE = _Style()


def set_color(color=None, truecolor=None, light=None):
    """Turn colors on or off (None: follow the terminal). Tests use set_color(False) for plain output."""
    STYLE.set(color, truecolor, light)


def accent(s):
    return STYLE._c(STYLE.amber, s)


def accent_bold(s):
    return STYLE._c("1;" + STYLE.amber_text if STYLE.amber_text else "1", s)


def bold(s):
    return STYLE._c("1", s)


def dim(s):
    return STYLE._c("2", s)


def ok(s):
    return STYLE._c("32", s)


def bad(s):
    return STYLE._c("31", s)


def warn(s):
    return STYLE._c("33", s)


def badge(spec) -> str:
    """('detected', 'ok') → green text; a plain string is dim."""
    if not spec:
        return ""
    text, tone = (spec, "dim") if isinstance(spec, str) else spec
    return {"ok": ok, "warn": warn, "bad": bad, "accent": accent, "bold": bold}.get(tone, dim)(text)


def step_header(n: int, total: int, title: str, width: int) -> list:
    """'Step 3 of 8 · Agents' and a thin progress bar under it."""
    bar_w = max(10, min(56, width - 4))
    full = round(bar_w * n / max(1, total))
    return [f"  {bold(f'Step {n} of {total}')} {dim('·')} {bold(title)}",
            "  " + accent("━" * full) + dim("─" * (bar_w - full))]


# --------------------------------------------------------------------------- keys

_CSI = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "shift-tab",
        "1~": "home", "7~": "home", "4~": "end", "8~": "end", "3~": "delete", "2~": "insert",
        "5~": "pageup", "6~": "pagedown"}
_KEYS = {b"\r": "enter", b"\n": "enter", b" ": "space", b"\t": "tab", b"\x7f": "backspace", b"\x08": "backspace",
         b"\x03": "ctrl-c", b"\x04": "ctrl-d", b"\x01": "ctrl-a", b"\x05": "ctrl-e", b"\x15": "ctrl-u",
         b"\x0b": "ctrl-k", b"\x17": "ctrl-w", b"\x10": "ctrl-p", b"\x0e": "ctrl-n", b"\x02": "ctrl-b",
         b"\x06": "ctrl-f", b"\x1a": "ctrl-z", b"\x0c": "ctrl-l"}
UP, DOWN = ("up", "ctrl-p", "k", "shift-tab"), ("down", "ctrl-n", "j", "tab")


class KeyReader:
    """Turns input bytes into key names: 'up', 'enter', 'space', 'ctrl-c', 'esc', or the typed character."""

    def __init__(self, read, ready):
        self._read, self._ready, self._back = read, ready, bytearray()

    @classmethod
    def from_bytes(cls, data: bytes) -> "KeyReader":
        buf = bytearray(data)

        def read(n):
            chunk = bytes(buf[:n])
            del buf[:n]
            return chunk
        return cls(read, lambda timeout=0: bool(buf))

    @classmethod
    def from_fd(cls, fd: int) -> "KeyReader":
        def ready(timeout=0):
            try:
                return bool(_select.select([fd], [], [], timeout)[0])
            except (OSError, ValueError):
                return False
        return cls(lambda n: os.read(fd, n), ready)

    def ready(self, timeout=0) -> bool:
        return bool(self._back) or self._ready(timeout)

    def read(self, n=1) -> bytes:
        if self._back:
            chunk = bytes(self._back[:n])
            del self._back[:n]
            return chunk
        return self._read(n)

    def unread(self, data: bytes):
        self._back[:0] = data

    def pending(self) -> bytes:
        """Whatever is already typed, without waiting (put it back with `unread`)."""
        out = bytearray()
        while self.ready(0):
            b = self.read(64)
            if not b:
                break
            out += b
        return bytes(out)

    def key(self) -> str:
        b = self.read(1)
        if not b:
            raise Cancelled("input closed")
        if b == b"\x1b":
            if not self.ready(0.05):
                return "esc"
            b2 = self.read(1)
            if b2 == b"O":
                return _CSI.get(self.read(1).decode("latin-1"), "unknown")
            if b2 == b"[":
                seq = ""
                while True:
                    c = self.read(1)
                    if not c:
                        break
                    seq += c.decode("latin-1")
                    if "@" <= seq[-1] <= "~":
                        break
                if seq.endswith("~"):
                    return _CSI.get(seq[:-1].split(";")[0] + "~", "unknown")
                return _CSI.get(seq[-1:], "unknown")
            self.unread(b2)  # Esc, then another key typed right after it
            return "esc"
        if b in _KEYS:
            return _KEYS[b]
        c = b[0]
        if c >= 0xC0:
            b += self.read(1 if c < 0xE0 else 2 if c < 0xF0 else 3)
            return b.decode("utf-8", "replace")
        if c < 0x20:
            return "ctrl-" + chr(c + 96)
        return b.decode("latin-1")


def decode(data: bytes) -> list:
    """All the keys in a byte string (for tests and scripted input)."""
    r, out = KeyReader.from_bytes(data), []
    while r.ready(0):
        out.append(r.key())
    return out


# --------------------------------------------------------------------------- prompts


class _Back:
    def __repr__(self):
        return "BACK"


BACK = _Back()


class Cancelled(Exception):
    """The user pressed Ctrl-C, or the input closed."""


class Option:
    """One choice: `value` is returned, `label` is shown, `note` is dim text beside it, `badge` a short state
    ('detected', or a (text, tone) pair), `desc` the one-line help under the focused item, `locked` cannot change."""

    def __init__(self, value, label=None, note="", badge="", desc="", locked=False):
        self.value, self.label = value, str(value) if label is None else label
        self.note, self.badge, self.desc, self.locked = note, badge, desc, locked


class Prompt:
    """Base: a title, a help line, Esc to go back (when `back`), Ctrl-C to cancel."""
    hint = ""

    def __init__(self, title: str, help: str = "", back: bool = True):
        self.title, self.help, self.back = title, help, back
        self.finished, self.result, self.error = False, None, ""

    def _finish(self, value):
        self.finished, self.result = True, value

    def handle(self, key: str) -> bool:
        if key == "ctrl-c":
            raise Cancelled()
        if key == "esc" and self.back:
            self._finish(BACK)
        else:
            self._key(key)
        return self.finished

    def _key(self, key):  # pragma: no cover - abstract
        raise NotImplementedError

    def head(self, width: int) -> list:
        out = ["  " + bold(self.title)]
        if self.help:
            out += ["  " + dim(ln) for ln in wrap(self.help, width - 4)]
        return out + [""]

    def keys_line(self, parts, width: int = 80) -> list:
        """The key hints, wrapped (not cut) on a narrow terminal."""
        parts, lines, cur = list(parts) + (["esc back"] if self.back else []), [], ""
        for part in parts:
            nxt = f"{cur} · {part}" if cur else part
            if cur and vlen(nxt) > width - 3:
                lines.append(cur)
                nxt = part
            cur = nxt
        return ["  " + dim(ln) for ln in lines + [cur]]

    def cursor(self):
        """(line index in lines(), column) for the text cursor, or None to hide it."""
        return None


class Select(Prompt):
    """One choice from a list: ↑↓ (or j/k, 1–9) move, Enter chooses."""

    def __init__(self, title, options, default=None, help="", back=True, enter="choose"):
        super().__init__(title, help, back)
        self.options = [o if isinstance(o, Option) else Option(o) for o in options]
        values = [o.value for o in self.options]
        self.i = values.index(default) if default in values else 0
        self.enter = enter

    @property
    def value(self):
        return self.options[self.i].value

    def _move(self, key) -> bool:
        n = len(self.options)
        if key in UP:
            self.i = (self.i - 1) % n
        elif key in DOWN:
            self.i = (self.i + 1) % n
        elif key in ("home", "pageup"):
            self.i = 0
        elif key in ("end", "pagedown"):
            self.i = n - 1
        elif len(key) == 1 and key.isdigit() and 1 <= int(key) <= n:
            self.i = int(key) - 1
        else:
            return False
        return True

    def _key(self, key):
        if not self._move(key) and key == "enter":
            self._finish(self.value)

    def _mark(self, o) -> str:
        return ""

    def rows(self, width: int) -> list:
        marks = [self._mark(o) for o in self.options]
        mw = max((vlen(m) for m in marks), default=0)
        lw = max(vlen(o.label) for o in self.options)
        nw = max(vlen(o.note) for o in self.options)
        bw = max(vlen(strip(badge(o.badge))) for o in self.options)
        lead = 4 + (mw + 1 if mw else 0)
        show_note = nw and lead + lw + 2 + nw + (2 + bw if bw else 0) <= width - 1
        out = []
        for idx, o in enumerate(self.options):
            focus = idx == self.i
            pointer = accent(POINTER) if focus else " "
            label = accent_bold(o.label) if focus else o.label
            row = f"  {pointer} " + (marks[idx] + " " if mw else "") + pad(label, lw)
            if show_note:
                row += "  " + dim(pad(o.note, nw))
            if o.badge:
                row += "  " + badge(o.badge)
            out.append(row.rstrip())
            if focus and o.desc:
                out.append(" " * lead + dim(o.desc))
        return out

    def lines(self, width: int) -> list:
        return self.head(width) + self.rows(width) + [""] + self.keys_line(["↑↓ move", f"enter {self.enter}"], width)


class MultiSelect(Select):
    """Several choices: ↑↓ move, Space toggles, `a` selects all or none, Enter confirms."""

    def __init__(self, title, options, selected=(), help="", back=True, min_count=0):
        super().__init__(title, options, None, help, back)
        values = [o.value for o in self.options]
        self.initial = [v for v in selected if v in values]
        self.on = set(self.initial) | {o.value for o in self.options if o.locked}
        self.min_count = min_count

    def _free(self):
        return [o.value for o in self.options if not o.locked]

    def _key(self, key):
        if self._move(key):
            self.error = ""
            return
        o = self.options[self.i]
        if key == "space":
            if not o.locked:
                self.on ^= {o.value}
            self.error = ""
        elif key in ("a", "A"):
            free = self._free()
            if all(v in self.on for v in free):
                self.on -= set(free)
            else:
                self.on |= set(free)
            self.error = ""
        elif key == "enter":
            if len(self.on) < self.min_count:
                self.error = "Choose at least one." if self.min_count == 1 else f"Choose at least {self.min_count}."
                return
            values = [o.value for o in self.options]
            keep = [v for v in self.initial if v in self.on]
            self._finish(keep + [v for v in values if v in self.on and v not in keep]
                         + [v for v in self.on if v not in values and v not in keep])

    def _mark(self, o) -> str:
        if o.value in self.on:
            return dim(ON) if o.locked else ok(ON)
        return dim(OFF)

    def lines(self, width: int) -> list:
        free = self._free()
        every = free and all(v in self.on for v in free)
        count = f"{len(self.on)} of {len(self.options)} selected"
        out = self.head(width) + self.rows(width) + [""]
        if self.error:
            out.append("  " + bad("✗ " + self.error))
        return out + self.keys_line([count, "space select", "a " + ("none" if every else "all"), "enter confirm"], width)


class Confirm(Prompt):
    """Yes or no: y/n answer at once, ←→ or Tab switch, Enter accepts the highlighted one (the default first)."""

    def __init__(self, title, default=True, help="", back=True, yes="Yes", no="No"):
        super().__init__(title, help, back)
        self.value, self.yes, self.no = bool(default), yes, no

    def _key(self, key):
        if key in ("y", "Y"):
            self._finish(True)
        elif key in ("n", "N"):
            self._finish(False)
        elif key in ("left", "right", "tab", "shift-tab", "h", "l", "up", "down"):
            self.value = not self.value
        elif key == "enter":
            self._finish(self.value)

    def choices(self) -> str:
        def one(text, on):
            return accent_bold(f"[ {text} ]") if on else dim(f"  {text}  ")
        return one(self.yes, self.value) + " " + one(self.no, not self.value)

    def lines(self, width: int) -> list:
        return self.head(width) + ["  " + self.choices(), ""] + \
            self.keys_line(["y yes", "n no", "←→ switch", "enter confirm"], width)

    def inline(self, width: int) -> str:
        if self.finished:
            return f"  {dim('›')} {dim(self.title)}  {self.yes if self.result else self.no}"
        return f"  {accent('?')} {bold(self.title)}  {self.choices()}  {dim('y/n')}"


class Text(Prompt):
    """A line of text with a default: typing replaces the default, Enter keeps it; `validate(value)` returns an
    error message (shown in red under the field) or None."""

    def __init__(self, title, default="", help="", validate=None, back=True):
        super().__init__(title, help, back)
        self.default = str(default)
        self.buf, self.pristine, self.validate = list(self.default), True, validate
        self.pos = len(self.buf)

    @property
    def text(self) -> str:
        return "".join(self.buf)

    def _edit(self):
        self.pristine, self.error = False, ""

    def _key(self, key):
        if key == "space":
            key = " "
        if len(key) == 1 and key.isprintable():
            if self.pristine:
                self.buf, self.pos = [], 0
            self._edit()
            self.buf.insert(self.pos, key)
            self.pos += 1
        elif key == "backspace":
            self._edit()
            if self.pos:
                del self.buf[self.pos - 1]
                self.pos -= 1
        elif key in ("delete", "ctrl-d"):
            self._edit()
            if self.pos < len(self.buf):
                del self.buf[self.pos]
        elif key in ("left", "ctrl-b"):
            self._edit()
            self.pos = max(0, self.pos - 1)
        elif key in ("right", "ctrl-f"):
            self._edit()
            self.pos = min(len(self.buf), self.pos + 1)
        elif key in ("home", "ctrl-a"):
            self._edit()
            self.pos = 0
        elif key in ("end", "ctrl-e"):
            self._edit()
            self.pos = len(self.buf)
        elif key == "ctrl-u":
            self._edit()
            del self.buf[:self.pos]
            self.pos = 0
        elif key == "ctrl-k":
            self._edit()
            del self.buf[self.pos:]
        elif key == "ctrl-w":
            self._edit()
            j = self.pos
            while j and self.buf[j - 1] == " ":
                j -= 1
            while j and self.buf[j - 1] != " ":
                j -= 1
            del self.buf[j:self.pos]
            self.pos = j
        elif key == "enter":
            value = self.text.strip() or self.default
            err = self.validate(value) if self.validate else None
            if err:
                self.error = err
            else:
                self._finish(value)

    def _window(self, width: int):
        room = max(8, width - 7)
        start = max(0, self.pos - room + 1) if self.pos >= room else 0
        return self.text[start:start + room], self.pos - start

    def lines(self, width: int) -> list:
        self._width = width
        shown, _ = self._window(width)
        field = dim(shown) if self.pristine else shown
        out = self.head(width) + [f"  {accent('›')} {field}"]
        out.append("  " + bad("✗ " + self.error) if self.error else "")
        keys = ["type to replace", f"enter keeps {self.default}"] if self.pristine and self.default else ["enter confirm"]
        return out + self.keys_line(keys, width)

    def cursor(self):
        width = getattr(self, "_width", 80)
        return len(self.head(width)), 4 + self._window(width)[1]


def run_keys(prompt: Prompt, data: bytes):
    """Feed bytes to a prompt until it finishes; returns its result (tests, scripted input)."""
    r = KeyReader.from_bytes(data)
    while not prompt.finished:
        prompt.handle(r.key())
    return prompt.result


# --------------------------------------------------------------------------- terminal


def available(fin=None, fout=None) -> bool:
    """True when both ends are a real terminal that understands cursor movement."""
    fin, fout = fin or sys.stdin, fout or sys.stdout
    if termios is None or os.environ.get("TERM", "dumb") in ("", "dumb"):
        return False
    try:
        return fin.isatty() and fout.isatty()
    except (AttributeError, ValueError):
        return False


class Terminal:
    """The real terminal: raw keys, the alternate screen, its size; restores every mode it changes."""

    def __init__(self, fin=None, fout=None, reader=None):
        self.fin, self.out = fin or sys.stdin, fout or sys.stdout
        try:
            self.fd = self.fin.fileno()
        except (AttributeError, ValueError, OSError):
            self.fd = None
        self.reader = reader or KeyReader.from_fd(self.fd)
        self.lock = threading.RLock()
        self._resized, self._raw_old, self._full, self._quiet_old = False, None, False, None

    # output
    def write(self, s: str):
        with self.lock:
            self.out.write(s)
            self.out.flush()

    def size(self):
        try:
            cols, rows = os.get_terminal_size(self.out.fileno())
        except (AttributeError, ValueError, OSError):
            cols, rows = 80, 24
        return max(20, cols or 80), max(8, rows or 24)

    # input
    def key(self) -> str:
        if self.fd is None:  # scripted input (tests)
            return self.reader.key()
        while True:
            if self._resized:
                self._resized = False
                return "resize"
            if self.reader.ready(0.2):
                return self.reader.key()

    def interrupted(self) -> bool:
        """Ctrl-C typed while something else runs (other typed keys are kept for later)."""
        if self.fd is None:
            return False
        data = self.reader.pending()
        if b"\x03" in data:
            return True
        self.reader.unread(data)
        return False

    # modes
    def _set_raw(self):
        if self.fd is None or termios is None:
            return
        self._raw_old = termios.tcgetattr(self.fd)
        new = termios.tcgetattr(self.fd)
        new[0] &= ~(termios.IXON | termios.ICRNL | termios.INLCR)
        new[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.fd, termios.TCSANOW, new)  # TCSANOW keeps what was typed ahead

    def _unset_raw(self):
        if self._raw_old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._raw_old)
            self._raw_old = None

    @contextmanager
    def raw(self):
        """Keys one at a time, no echo, Ctrl-C as a key (so it is handled, not a traceback)."""
        self._set_raw()
        try:
            yield self
        finally:
            self._unset_raw()
            self.write("\x1b[0m\x1b[?25h")

    @contextmanager
    def quiet(self):
        """While steps run: typed keys are not echoed over the spinner; Ctrl-C still interrupts."""
        old = None
        if self.fd is not None and termios is not None:
            try:
                old = termios.tcgetattr(self.fd)
                new = termios.tcgetattr(self.fd)
                new[3] &= ~(termios.ECHO | termios.ICANON)
                termios.tcsetattr(self.fd, termios.TCSANOW, new)
            except termios.error:
                old = None
        self._quiet_old = old
        try:
            yield self
        finally:
            self._quiet_old = None
            if old is not None:
                termios.tcflush(self.fd, termios.TCIFLUSH)
                termios.tcsetattr(self.fd, termios.TCSADRAIN, old)
            self.write("\x1b[0m\x1b[?25h")

    def _enter_full(self):
        self.write("\x1b[?1049h\x1b[H\x1b[2J\x1b[?25l")
        self._full = True

    def _leave_full(self):
        if self._full:
            self.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            self._full = False

    @contextmanager
    def session(self):
        """The interactive part: raw keys on the alternate screen, redrawn when the window changes size."""
        prev, hooked = None, threading.current_thread() is threading.main_thread()
        if hooked:
            prev = signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "_resized", True))
        self._set_raw()
        self._enter_full()
        try:
            yield self
        finally:
            self._leave_full()
            self._unset_raw()
            if hooked:
                signal.signal(signal.SIGWINCH, prev if prev is not None else signal.SIG_DFL)

    @contextmanager
    def suspended(self):
        """Back to the normal screen and line mode (for a command that talks to the user), then back again."""
        was_full, was_raw, quiet = self._full, self._raw_old is not None, None
        if was_full:
            self._leave_full()
        if was_raw:
            self._unset_raw()
        if self._quiet_old is not None:
            quiet = termios.tcgetattr(self.fd)
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._quiet_old)
        self.write("\x1b[?25h")
        try:
            yield self
        finally:
            if quiet is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, quiet)
            if was_raw:
                self._set_raw()
            if was_full:
                self._enter_full()


# --------------------------------------------------------------------------- drivers


class Screen:
    """Full-screen pages in the alternate buffer: static lines, then the active prompt, repainted on every key.

    When the page is taller than the window, lines under the top bar are dropped first, so the prompt stays in
    view; when the window is narrower than a line, the line is cut with '…'."""

    def __init__(self, term: Terminal):
        self.term = term

    def compose(self, static, prompt=None):
        """`static` may be a list of lines or a function returning them (called on every paint, so the page
        follows the window size)."""
        static = static() if callable(static) else static
        width, height = self.term.size()
        body = prompt.lines(width - 1) if prompt is not None else []
        lines, start = list(static) + body, len(static)
        over = len(lines) - (height - 1)
        if over > 0:
            cut = min(over, max(0, len(static) - 1))
            lines = lines[:1] + lines[1 + cut:]
            start -= cut
            if len(lines) > height - 1:
                drop = len(lines) - (height - 1)
                lines, start = lines[drop:], start - drop
        cur = prompt.cursor() if prompt is not None and not prompt.finished else None
        if cur is not None:
            cur = (start + cur[0], cur[1])
        return [fit(ln, width - 1) for ln in lines], cur

    def paint(self, static, prompt=None):
        lines, cur = self.compose(static, prompt)
        out = ["\x1b[?2026h\x1b[H"]
        out += [ln + "\x1b[0m\x1b[K\r\n" for ln in lines[:-1]] + ([lines[-1] + "\x1b[0m\x1b[K"] if lines else [])
        out.append("\x1b[J")
        if cur is not None and 0 <= cur[0] < len(lines):
            out.append(f"\x1b[{cur[0] + 1};{cur[1] + 1}H\x1b[?25h")
        else:
            out.append("\x1b[?25l")
        out.append("\x1b[?2026l")
        self.term.write("".join(out))

    def ask(self, prompt: Prompt, static=()):
        """Show the prompt under `static` and return its result (or BACK)."""
        while True:
            self.paint(static, prompt)
            if prompt.finished:
                return prompt.result
            key = self.term.key()
            if key != "resize":
                prompt.handle(key)

    def busy(self, static, label: str, fn):
        """Run `fn` while a spinner turns under `static`; Ctrl-C stops it (KeyboardInterrupt)."""
        stop, hit = threading.Event(), []

        def page(i):
            return list(static() if callable(static) else static) + [f"  {accent(FRAMES[i % len(FRAMES)])} {label}"]

        def spin():
            i = 0
            while not stop.wait(0.08):
                i += 1
                self.paint(page(i))
                if self.term.interrupted():
                    hit.append(True)
                    import _thread
                    _thread.interrupt_main()
                    return
        self.paint(page(0))
        t = threading.Thread(target=spin, daemon=True)
        t.start()
        try:
            return fn()
        finally:
            stop.set()
            t.join(1)


def ask_inline(term: Terminal, prompt: Confirm):
    """A one-line prompt in the normal screen (used between apply steps)."""
    with term.raw():
        term.write("\x1b[?25l")
        while True:
            width = term.size()[0]
            term.write("\r\x1b[2K" + fit(prompt.inline(width), width - 1))
            if prompt.finished:
                term.write("\r\n")
                return prompt.result
            key = term.key()
            if key != "resize":
                prompt.handle(key)


def elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"


class _Item:
    def __init__(self, label):
        self.label, self.state, self.detail, self.notes = label, "pending", "", []
        self.t0 = self.t1 = None


class Checklist:
    """Steps drawn in place: ○ waiting, a spinner while running, then ✓ done, ✗ failed, ! needs attention, – skipped.

    The finished steps above the running one are written once and stay; only the rest is redrawn. `pause()` hides
    the unfinished steps while a command talks to the user; `resume()` shows them again."""

    FINISHED = ("ok", "fail", "warn", "skip")
    MARKS = {"ok": lambda: ok("✓"), "fail": lambda: bad("✗"), "warn": lambda: warn("!"), "skip": lambda: dim("–"),
             "pending": lambda: dim("○")}

    def __init__(self, term: Terminal, labels, animate=True):
        self.term, self.items = term, [_Item(lb) for lb in labels]
        self.base, self.drawn, self.frame, self.current = 0, 0, 0, None
        self.animate, self.paused = animate, False
        self._stop, self._thread = threading.Event(), None

    def __enter__(self):
        self.term.write("\x1b[?25l")
        self.draw()
        if self.animate:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(1)
        with self.term.lock:
            if self.current is not None and self.items[self.current].state == "running":
                it = self.items[self.current]
                it.state, it.detail, it.t1 = "fail", "stopped", time.time()
            self.draw(final=True)
            self.term.write("\x1b[?25h")
        return False

    def _spin(self):
        while not self._stop.wait(0.1):
            with self.term.lock:
                if not self.paused and self.current is not None:
                    self.frame += 1
                    self.draw()

    def start(self, i: int, detail: str = ""):
        with self.term.lock:
            it = self.items[i]
            it.state, it.detail, it.t0 = "running", detail, time.time()
            self.current = i
            self.draw()

    def update(self, detail: str):
        with self.term.lock:
            if self.current is not None:
                self.items[self.current].detail = detail

    def finish(self, i: int, state: str = "ok", detail: str = "", notes=()):
        with self.term.lock:
            it = self.items[i]
            it.state, it.detail, it.notes, it.t1 = state, detail, list(notes), time.time()
            if it.t0 is None:
                it.t0 = it.t1
            if self.current == i:
                self.current = None
            self.draw()

    def pause(self):
        with self.term.lock:
            self.draw(final=True, pending=False)
            self.paused = True
            self.term.write("\x1b[?25h")

    def resume(self):
        with self.term.lock:
            self.paused = False
            self.term.write("\x1b[?25l")
            self.draw()

    def _line(self, it: _Item, lw: int, width: int) -> list:
        mark = accent(FRAMES[self.frame % len(FRAMES)]) if it.state == "running" else self.MARKS[it.state]()
        label = bold(it.label) if it.state == "running" else (dim(it.label) if it.state == "pending" else it.label)
        left = f"  {mark} {pad(label, lw)}"
        if it.detail:
            left += "  " + dim(it.detail)
        t = ""
        if it.t0 is not None and it.state != "pending":
            secs = (it.t1 or time.time()) - it.t0
            t = elapsed(secs) if secs >= 1 or it.state == "running" else ""
        room = width - 1 - vlen(t) - 2
        line = fit(left, room) if t else fit(left, width - 1)
        if t:
            line = pad(line, room) + "  " + dim(t)
        return [line] + [fit("      " + n, width - 1) for n in it.notes]

    def draw(self, final=False, pending=True):
        width = self.term.size()[0]
        items = self.items[self.base:]
        lw = min(34, max((vlen(it.label) for it in self.items), default=0))
        out, committed, still = [], 0, False
        for it in items:
            if not pending and it.state not in self.FINISHED:
                continue
            ls = self._line(it, lw, width)
            out += ls
            if it.state in self.FINISHED and not still:
                committed += len(ls)
                self.base += 1
            else:
                still = True
        up = f"\x1b[{self.drawn}A\r" if self.drawn else "\r"
        self.term.write(up + "".join(ln + "\x1b[0m\x1b[K\n" for ln in out) + "\x1b[J")
        self.drawn = 0 if (final or not pending) else len(out) - committed
