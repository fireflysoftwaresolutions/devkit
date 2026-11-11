# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev setup`: the terminal UI toolkit (keys, prompts, drivers), the interactive wizard, the line-prompt fallback,
`--yes` and the Playwright skill install. Nothing here installs packages or touches the real ~/.config, ~/.claude or
~/.codex: configs go to temporary folders, commands are stubbed, and the pseudo-terminal runs use a temporary HOME.
"""
import io
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sandbox  # noqa: E402

from devkit import core, providers, registry, setup, toolchain, tui, ui, wizard  # noqa: E402

LIB = str(Path(__file__).resolve().parents[1] / "lib")
UP, DOWN, ENTER, ESC, SPACE, CTRL_C, BS = b"\x1b[A", b"\x1b[B", b"\r", b"\x1b", b" ", b"\x03", b"\x7f"


def plain(s) -> str:
    return tui.strip(s)


def dev_args(argv) -> list:
    """The arguments of a `dev` call after its bin/dev path, whatever the interpreter flags before it."""
    argv = [str(a) for a in argv]
    i = next((n for n, a in enumerate(argv) if a.endswith("/bin/dev")), None)
    return argv[i + 1:] if i is not None else []


class FakeTerm(tui.Terminal):
    """A terminal that reads scripted bytes and writes to a string (no termios, no TTY)."""

    def __init__(self, data: bytes = b"", cols=100, rows=40):
        super().__init__(fin=io.StringIO(), fout=io.StringIO(), reader=tui.KeyReader.from_bytes(data))
        self._size = (cols, rows)

    def size(self):
        return self._size

    @property
    def text(self) -> str:
        return plain(self.out.getvalue())


class _Stdin(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def setUpModule():
    tui.set_color(False)


def tearDownModule():
    tui.set_color()


# --------------------------------------------------------------------------- keys and prompts


class KeyTests(unittest.TestCase):
    def test_decodes_arrows_space_enter_and_control_keys(self):
        self.assertEqual(tui.decode(UP + DOWN + SPACE + ENTER + b"a" + CTRL_C),
                         ["up", "down", "space", "enter", "a", "ctrl-c"])
        self.assertEqual(tui.decode(b"\x1bOA\x1bOB\x1b[3~\x1b[1;5C\x1b[Z\x7f\n\t"),
                         ["up", "down", "delete", "right", "shift-tab", "backspace", "enter", "tab"])
        self.assertEqual(tui.decode("é".encode() + b"\x15"), ["é", "ctrl-u"])

    def test_a_lone_escape_is_esc_even_when_another_key_follows(self):
        self.assertEqual(tui.decode(ESC), ["esc"])
        self.assertEqual(tui.decode(ESC + ENTER), ["esc", "enter"])

    def test_closed_input_cancels(self):
        with self.assertRaises(tui.Cancelled):
            tui.KeyReader.from_bytes(b"").key()


class SelectTests(unittest.TestCase):
    def options(self):
        return [tui.Option("a", "Alpha", desc="first"), tui.Option("b", "Beta"), tui.Option("c", "Gamma")]

    def test_arrows_move_and_enter_chooses(self):
        self.assertEqual(tui.run_keys(tui.Select("Pick", self.options()), DOWN + DOWN + ENTER), "c")
        self.assertEqual(tui.run_keys(tui.Select("Pick", self.options()), DOWN + UP + ENTER), "a")

    def test_moves_wrap_around_and_start_at_the_default(self):
        self.assertEqual(tui.run_keys(tui.Select("Pick", self.options()), UP + ENTER), "c")
        self.assertEqual(tui.run_keys(tui.Select("Pick", self.options(), default="b"), ENTER), "b")
        self.assertEqual(tui.run_keys(tui.Select("Pick", self.options()), b"3" + ENTER), "c")

    def test_esc_goes_back_and_ctrl_c_cancels(self):
        self.assertIs(tui.run_keys(tui.Select("Pick", self.options()), ESC), tui.BACK)
        self.assertEqual(tui.run_keys(tui.Select("Pick", self.options(), back=False), ESC + ENTER), "a")
        with self.assertRaises(tui.Cancelled):
            tui.run_keys(tui.Select("Pick", self.options()), DOWN + CTRL_C)

    def test_the_focused_item_shows_its_description(self):
        s = tui.Select("Pick", self.options())
        text = "\n".join(s.lines(80))
        self.assertIn("❯ Alpha", text)
        self.assertIn("first", text)
        s.handle("down")
        self.assertNotIn("first", "\n".join(s.lines(80)))


class MultiSelectTests(unittest.TestCase):
    def options(self):
        return [tui.Option("github", "GitHub", locked=True), tui.Option("gitlab", "GitLab", badge=("found", "ok")),
                tui.Option("bitbucket", "Bitbucket")]

    def test_space_toggles_the_focused_item(self):
        m = tui.MultiSelect("Hosts", self.options(), ["github"])
        self.assertEqual(tui.run_keys(m, DOWN + SPACE + DOWN + SPACE + UP + SPACE + ENTER), ["github", "bitbucket"])

    def test_a_selects_all_then_none_but_never_a_locked_item(self):
        self.assertEqual(tui.run_keys(tui.MultiSelect("Hosts", self.options(), ["github"]), b"a" + ENTER),
                         ["github", "gitlab", "bitbucket"])
        self.assertEqual(tui.run_keys(tui.MultiSelect("Hosts", self.options(), ["github"]), b"aa" + ENTER), ["github"])
        self.assertEqual(tui.run_keys(tui.MultiSelect("Hosts", self.options(), ["github"]), SPACE + ENTER), ["github"])

    def test_keeps_the_order_of_what_was_already_selected(self):
        m = tui.MultiSelect("Hosts", self.options(), ["bitbucket", "github"])
        self.assertEqual(tui.run_keys(m, DOWN + SPACE + ENTER), ["bitbucket", "github", "gitlab"])

    def test_minimum_count_shows_an_error_and_waits(self):
        m = tui.MultiSelect("Stacks", [tui.Option("java"), tui.Option("php")], [], min_count=1)
        m.handle("enter")
        self.assertFalse(m.finished)
        self.assertIn("Choose at least one.", plain("\n".join(m.lines(80))))
        self.assertEqual(tui.run_keys(m, SPACE + ENTER), ["java"])

    def test_draws_marks_badges_and_the_count(self):
        text = plain("\n".join(tui.MultiSelect("Hosts", self.options(), ["github"]).lines(80)))
        self.assertIn("■ GitHub", text)
        self.assertIn("□ GitLab", text)
        self.assertIn("found", text)
        self.assertIn("1 of 3 selected", text)


class ConfirmTests(unittest.TestCase):
    def test_y_and_n_answer_at_once(self):
        self.assertTrue(tui.run_keys(tui.Confirm("Go?", False), b"y"))
        self.assertFalse(tui.run_keys(tui.Confirm("Go?", True), b"n"))

    def test_enter_accepts_the_highlighted_default_and_arrows_switch(self):
        self.assertTrue(tui.run_keys(tui.Confirm("Go?", True), ENTER))
        self.assertFalse(tui.run_keys(tui.Confirm("Go?", True), b"\x1b[C" + ENTER))
        self.assertIs(tui.run_keys(tui.Confirm("Go?", True), ESC), tui.BACK)

    def test_the_default_is_highlighted(self):
        self.assertIn("[ No ]", plain("\n".join(tui.Confirm("Go?", False).lines(80))))
        self.assertIn("[ Yes ]", plain(tui.Confirm("Go?", True).inline(80)))


class TextTests(unittest.TestCase):
    def test_enter_keeps_the_default_and_typing_replaces_it(self):
        self.assertEqual(tui.run_keys(tui.Text("CPUs", "6"), ENTER), "6")
        self.assertEqual(tui.run_keys(tui.Text("CPUs", "6"), b"12" + ENTER), "12")

    def test_backspace_and_arrows_edit_the_default(self):
        self.assertEqual(tui.run_keys(tui.Text("Root", "~/Dev"), BS + b"x" + ENTER), "~/Dex")
        self.assertEqual(tui.run_keys(tui.Text("Root", "ab"), b"\x1b[D" + b"X" + ENTER), "aXb")
        self.assertEqual(tui.run_keys(tui.Text("Root", "abc"), b"\x15" + b"z" + ENTER), "z")

    def test_validation_shows_the_error_inline_until_the_value_is_fixed(self):
        t = tui.Text("CPUs", "6", validate=wizard._int_in(1, 8))
        for k in tui.decode(b"99" + ENTER):
            t.handle(k)
        self.assertFalse(t.finished)
        self.assertIn("✗ Enter a whole number from 1 to 8.", plain("\n".join(t.lines(80))))
        t.handle("backspace")
        self.assertNotIn("✗", plain("\n".join(t.lines(80))))
        self.assertEqual(tui.run_keys(t, BS + b"4" + ENTER), "4")

    def test_unicode_and_the_cursor_position(self):
        t = tui.Text("Name", "")
        for k in tui.decode("añ".encode()):
            t.handle(k)
        t.lines(80)
        self.assertEqual(t.cursor(), (2, 6))
        self.assertEqual(tui.run_keys(t, ENTER), "añ")


class LayoutTests(unittest.TestCase):
    def test_fit_cuts_to_the_width_and_keeps_escape_sequences(self):
        self.assertEqual(tui.fit("hello world", 8), "hello w…")
        cut = tui.fit("\x1b[1mhello world\x1b[0m", 8)
        self.assertEqual(tui.vlen(cut), 8)
        self.assertTrue(cut.startswith("\x1b[1m"))
        self.assertEqual(tui.vlen("日本"), 4)

    def test_clean_removes_escapes_and_control_characters_from_outside_text(self):
        self.assertEqual(tui.clean("Ada\x1b]0;evil\x07 \x1b[31mLovelace\x1b[0m\x07"), "Ada Lovelace")

    def test_step_header_and_progress_bar(self):
        head = tui.step_header(3, 8, "Agents", 80)
        self.assertEqual(plain(head[0]), "  Step 3 of 8 · Agents")
        self.assertEqual(plain(head[1]).count("━"), 21)

    def test_key_hints_wrap_on_a_narrow_terminal(self):
        lines = tui.MultiSelect("Hosts", [tui.Option("a")]).keys_line(["1 of 1 selected", "space select", "a all",
                                                                        "enter confirm"], 40)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(tui.vlen(ln) <= 39 for ln in lines))


class DriverTests(unittest.TestCase):
    def test_screen_returns_the_answer_and_never_draws_wider_than_the_window(self):
        term = FakeTerm(DOWN + ENTER, cols=30, rows=12)
        static = ["  " + "a very long static line that is wider than the terminal"]
        self.assertEqual(tui.Screen(term).ask(tui.Select("Pick one", ["first", "second"]), static), "second")
        frames = [f for f in term.out.getvalue().split("\x1b[H") if f]
        for line in plain(frames[-1]).replace("\r\n", "\n").split("\n"):
            self.assertLessEqual(tui.vlen(line), 29)

    def test_on_a_short_window_the_prompt_stays_in_view(self):
        term = FakeTerm(ENTER, cols=60, rows=10)
        static = ["top bar"] + [f"line {i}" for i in range(20)]
        tui.Screen(term).ask(tui.Select("Pick one", ["first", "second"]), static)
        last = plain(term.out.getvalue().split("\x1b[H")[-1])
        self.assertIn("top bar", last)
        self.assertIn("Pick one", last)
        self.assertNotIn("line 0", last)

    def test_terminal_modes_are_restored_after_an_error(self):
        import pty
        import termios
        master, slave = pty.openpty()
        # A terminal reads its output all the time (restoring the modes waits for that): `cat` plays that part, in
        # its own process, as Python 3.9 keeps the GIL inside tcsetattr.
        reader = subprocess.Popen(["cat"], stdin=master, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            term = tui.Terminal(os.fdopen(slave, "r", closefd=False), os.fdopen(slave, "w", closefd=False))
            before = termios.tcgetattr(slave)
            with self.assertRaises(RuntimeError):
                with term.session():
                    self.assertFalse(termios.tcgetattr(slave)[3] & termios.ICANON)
                    raise RuntimeError("boom")
            after = termios.tcgetattr(slave)
            pendin = getattr(termios, "PENDIN", 0)  # set by the kernel itself when a tty returns to line mode
            after[3] &= ~pendin
            before[3] &= ~pendin
            self.assertEqual(after, before)
        finally:
            os.close(slave)
            out = reader.communicate(timeout=10)[0]
            os.close(master)
        self.assertIn(b"\x1b[?1049h", out)
        self.assertIn(b"\x1b[?1049l", out)
        self.assertTrue(out.rstrip().endswith(b"\x1b[?1049l"))

    def test_checklist_shows_each_step_with_its_state_and_fix(self):
        term = FakeTerm()
        with tui.Checklist(term, ["Install", "Configure", "Check"], animate=False) as cl:
            cl.start(0, "brew")
            cl.finish(0, "ok", "3 installed")
            cl.start(1)
            cl.finish(1, "fail", "1 problem", ["Fix: dev install"])
        text = term.text
        self.assertIn("✓ Install", text)
        self.assertIn("✗ Configure", text)
        self.assertIn("Fix: dev install", text)
        self.assertIn("○ Check", text)  # never ran, and says so

    def test_available_only_on_a_real_terminal(self):
        tty = _Stdin(True)
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            self.assertTrue(tui.available(tty, tty))
            self.assertFalse(tui.available(_Stdin(False), tty))
            self.assertFalse(tui.available(tty, io.StringIO()))
        with mock.patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertFalse(tui.available(tty, tty))


# --------------------------------------------------------------------------- the wizard


# The config --yes writes on a first run where Claude Code is signed in and Codex is not: the agents signed in are the
# default answer (INSTALL-AC-2), so Codex is off.
SNAPSHOT_FIRST = {
    "dev_root": "~/Development", "github_user": "ada", "handoff_repo": "ada/handoff",
    "agents": {"claude": True, "codex": False}, "claude": {"default_mode": "bypassPermissions"},
    "codex": {"mode": "full", "approval_policy": "never", "sandbox_mode": "danger-full-access"},
    "stacks": ["python", "node"], "lock": False, "hosts": ["github", "gitlab"], "clouds": ["aws"],
    "docker": {"cpu": 6, "memory": 12, "disk": 80, "autostart": False}, "gc": {"auto": True},
    "sync": {"interval_minutes": 60}, "ai": {"enabled": False, "asked": True}, "frameworks": []}
SAVED ={"stacks": ["python"], "hosts": ["bitbucket"], "clouds": [], "docker": {"cpu": 4, "memory": 8},
         "editor": "zed", "agents": {"claude": True, "codex": False}, "lock": True}
SNAPSHOT_RERUN = {
    "stacks": ["python"], "hosts": ["github", "bitbucket"], "clouds": [],
    "docker": {"cpu": 4, "memory": 8, "disk": 80, "autostart": False}, "editor": "zed",
    "agents": {"claude": True, "codex": False}, "lock": True, "dev_root": "~/Development", "github_user": "ada",
    "handoff_repo": "ada/handoff", "claude": {"default_mode": "bypassPermissions"},
    "codex": {"mode": "full", "approval_policy": "never", "sandbox_mode": "danger-full-access"},
    "gc": {"auto": True}, "sync": {"interval_minutes": 60}, "ai": {"enabled": False, "asked": True}}
# The line prompts' questions and answers with --yes, as `dev setup` prints them for installs without a terminal.
GOLDEN_YES = """\
Good <time>, Ada. This wizard sets up devkit on this Mac. Press Enter to keep the value in [brackets]; nothing \
changes until you confirm.

   1 This machine
     macOS 26.1 · arm64 · 12 CPU · 36 GB RAM · Homebrew yes
     GitHub: ada
     Claude Code  signed in
     Codex        installed, not signed in

   2 Workspace
     Development root → ~/Development
     Private handoff repository → ada/handoff

   3 Agents
       1. both — Claude Code and Codex
     › 2. claude — Claude Code only
       3. codex — Codex only
     Which coding agents do you want to use? → claude
     › 1. bypassPermissions — no prompts; the devkit guard protects the workspace (recommended)
       2. acceptEdits — edits without prompts, commands ask
       3. default — ask before every tool
     Claude Code permission mode → bypassPermissions

   4 Stacks (toolchains, language servers, skills)
     □ 1. java — Java (JDK, Maven, Gradle, jdtls)
     ■ 2. python — Python (uv, pyright)
     □ 3. php — PHP (php, Composer, intelephense)
     ■ 4. node — Node / TypeScript (pnpm, tsc)
     Stacks you develop in (comma-separated numbers) → python, node

   5 Frameworks (skills, integrations, required runtimes)
     □ 1. angular — Angular
     □ 2. react — React
     □ 3. nextjs — Next.js
     □ 4. nestjs — NestJS
     □ 5. spring — Spring Boot
     □ 6. laravel — Laravel
     □ 7. symfony — Symfony
     Frameworks you develop in (numbers, or none) → none

   6 Git hosts and CI/CD (CLIs, sign-in checks, `dev ci status`)
     found on this Mac: gitlab (x); aws (y)
     ■ 1. github — GitHub · Actions
     ■ 2. gitlab — GitLab · GitLab CI
     □ 3. bitbucket — Bitbucket · Pipelines
     □ 4. azure-devops — Azure DevOps · Pipelines
     Where your repositories and pipelines live (comma-separated numbers) → github, gitlab
     Open new commit messages with devkit's template (commit.template in each repository that has none)? → yes

   7 Cloud and infrastructure (CLIs and sign-in checks)
     ■ 1. aws — AWS
     □ 2. azure — Microsoft Azure
     □ 3. gcp — Google Cloud
     □ 4. digitalocean — DigitalOcean
     □ 5. kubernetes — Kubernetes · Helm
     □ 6. terraform — OpenTofu / Terraform
     Clouds and infrastructure tools you use (numbers, or none) → aws

   8 Docker on Colima
     VM CPUs → 6
     VM memory (GB) → 12
     VM disk, grows as needed (GB) → 80
     Start Colima at login? → no
     Start Docker when the setup ends? → yes

   9 Automation
     Reclaim disk automatically (merged worktrees, idle build output, stale temp)? → yes
     Background sync interval (minutes) → 60
     Let background jobs ask a small model (Claude Haiku or Codex's gpt-6-luna, at most 20 calls a day), for example to merge duplicate lessons written in different languages? → no
     Lock Claude Code settings with a managed-settings file (needs your admin password)? → no

  10 Plan
     config       <CONFIG>
     root         ~/Development
     handoff      ada/handoff → ~/Development/.handoff
     Claude Code  bypassPermissions
     Codex        skip
     stacks       Python, Node / TypeScript
     frameworks   none
     git hosts    github, gitlab · commit template on
     clouds       aws
     packages     skip
     Docker VM    6 CPU · 12 GB · 80 GB
     automation   sync every 60 min · safe cleanup on · background AI off
     lock         off (drift is still blocked and repaired)
     Apply this plan? → yes

"""


def line_mode(saved=None, yes=True, typed=None, clouds=("aws",), state=None, call=None, resume=False, device=None,
              tools=("gh", "brew", "claude", "codex"), first=True):
    """Run `dev setup` through the line prompts with detection and commands stubbed: (rc, config, output, calls).
    `clouds`: the cloud and infrastructure tools this Mac shows. `state`: devkit's state folder (default: a temp one).
    `call(argv)`: what a started command returns (default 0). `device`: the terminal a sign-in would get (none).
    `resume`: then `dev setup --resume`; `first`: run plain `dev setup` before it (else only the resume runs)."""
    seen = {"hosts": ["github", "gitlab"], "clouds": list(clouds), "evidence": {"gitlab": "x", "aws": "y"}}
    with tempfile.TemporaryDirectory() as d:
        cfgf = Path(d) / "config.json"
        if saved is not None:
            cfgf.write_text(json.dumps(saved))
        calls, answers = [], iter(typed or []) if not callable(typed) else None

        def answer(prompt=""):
            print(prompt, end="")
            return typed(prompt) if answers is None else next(answers)

        def fake_exec(argv, on_line=None, log=None, env=None, timeout=None):
            calls.append([str(a) for a in argv])
            rc = call(argv) if call else 0
            if rc == 0 and dev_args(argv)[-1:] == ["doctor"] and on_line:
                on_line("  ✓ 1/1 checks passed")
            return wizard._Exit(rc)
        with mock.patch.object(wizard, "CONFIG_FILE", cfgf), mock.patch.object(setup, "STATE", state or Path(d)), \
                mock.patch.object(wizard, "STATE", state or Path(d)), \
                mock.patch.object(wizard, "terminal_device", lambda: device), \
                mock.patch.object(wizard, "_hw", lambda: (12, 36, "26.1", "arm64")), \
                mock.patch.object(core, "gh_user", lambda: {"login": "ada", "name": "Ada Lovelace", "id": 1}), \
                mock.patch.object(core, "have", lambda t: t in tools), \
                mock.patch.object(providers, "status", lambda include_account=True: {
                    "claude": {"installed": True, "logged_in": True}, "codex": {"installed": True, "logged_in": False}}), \
                mock.patch.object(toolchain, "detect", lambda: seen), mock.patch.object(wizard, "_exec", fake_exec), \
                mock.patch("subprocess.call", side_effect=AssertionError("every command goes through _exec")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no command may really run here")), \
                mock.patch("builtins.input", answer), \
                mock.patch("sys.stdin", _Stdin(not yes)), mock.patch("sys.stdout", io.StringIO()) as out:
            rc = wizard.run(assume_yes=yes, skip_packages=True) if first else None
            if resume:
                rc = wizard.run(assume_yes=yes, resume=True)
            run = core.read_json((state or Path(d)) / "setup-run.json", None)
        text = ui.ANSI_RE.sub("", out.getvalue()).replace(str(cfgf), "<CONFIG>")
        text = re.sub(r"Good (morning|afternoon|evening)", "Good <time>", text)
        line_mode.run = run
        return rc, (json.loads(cfgf.read_text()) if cfgf.exists() else None), text[max(text.find("Good <time>"), 0):], \
            calls


class LineModeTests(unittest.TestCase):
    def test_yes_writes_the_defaults_with_the_signed_in_agent(self):
        rc, cfg, out, calls = line_mode()
        self.assertEqual(rc, 0)
        self.assertEqual(cfg, SNAPSHOT_FIRST)
        self.assertEqual(list(cfg), list(SNAPSHOT_FIRST))  # same key order in the file
        self.assertEqual([dev_args(c)[0] for c in calls], ["install", "doctor"])
        self.assertEqual(dev_args(calls[0])[1:], ["--commit-template", "yes", "--skip-packages"])

    def test_yes_on_a_configured_mac_keeps_the_saved_values(self):
        rc, cfg, _, _ = line_mode(saved=SAVED)
        self.assertEqual(cfg, SNAPSHOT_RERUN)

    def test_yes_prints_the_line_prompts_unchanged(self):
        out = line_mode()[2]
        self.assertEqual(out[:out.index("  Applying your plan")], GOLDEN_YES)
        self.assertIn("✓ devkit is ready.", out)

    def test_typed_answers_in_the_line_prompts(self):
        # root, handoff, Codex only, its autonomy, stacks, hosts, no template, clouds, Docker (CPUs 8, start at login,
        # do not start it at the end), automation (no cleanup, 30 min, AI yes), then Apply
        typed = ["~/Code", "", "3", "2", "1,3", "", "", "n", "", "8", "", "", "y", "n", "n", "30", "y", ""]
        with tempfile.TemporaryDirectory() as state:
            rc, cfg, out, calls = line_mode(yes=False, typed=typed, state=Path(state))
            # the wizard itself writes no devkit state but the record of its apply steps (dev setup --resume) and the
            # log of its commands' output
            self.assertEqual(sorted(p.name for p in Path(state).iterdir()), ["logs", "setup-run.json"])
        self.assertEqual(dev_args(calls[0]), ["install", "--commit-template", "no", "--skip-packages"])
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["dev_root"], "~/Code")
        self.assertEqual(cfg["agents"], {"claude": False, "codex": True})
        self.assertEqual(cfg["codex"], {"mode": "workspace", "approval_policy": "on-request",
                                        "sandbox_mode": "workspace-write"})
        self.assertEqual(cfg["stacks"], ["java", "php"])
        self.assertEqual(cfg["docker"], {"cpu": 8, "memory": 12, "disk": 80, "autostart": True})
        self.assertEqual((cfg["gc"], cfg["sync"], cfg["lock"]), ({"auto": False}, {"interval_minutes": 30}, False))
        self.assertEqual(cfg["ai"], {"enabled": True, "asked": True})  # asked once, and answered yes
        self.assertIn("[Y/n]", out)

    def test_ctrl_c_in_an_optional_step_goes_on_with_the_rest(self):
        def call(argv):
            if dev_args(argv) == ["docker", "up"]:
                raise KeyboardInterrupt
            return 0
        def typed(prompt):  # every default, and yes to starting Docker
            return "y" if "Start Docker" in prompt else ""
        with mock.patch.object(core, "sh", lambda args, **kw: (1, "", "stopped")):  # colima status: stopped
            rc, _, out, calls = line_mode(yes=False, typed=typed, call=call,
                                          tools=("gh", "brew", "colima", "claude", "codex"))
        self.assertIn(["docker", "up"], [dev_args(c) for c in calls])
        self.assertEqual(dev_args(calls[-1]), ["doctor"])  # the step after it still ran
        self.assertRegex(out, r"– Start Docker +skipped by you")
        self.assertIn("dev docker up", out.split("To finish")[1])
        self.assertEqual({s["id"]: s["state"] for s in line_mode.run["steps"]}["docker"], "skipped")
        self.assertEqual(rc, 0)

    def test_a_failed_install_is_not_ready_and_resume_runs_it_again_with_the_saved_answers(self):
        tries = []

        def call(argv):
            if dev_args(argv)[:1] == ["install"]:
                tries.append(argv)
                return 70 if len(tries) == 1 else 0
            return 0
        rc, _, out, calls = line_mode(call=call, resume=True)
        self.assertIn("devkit is not ready yet. 1 step failed.", out)
        self.assertIn("Reason: dev install stopped with exit code 70 and gave no reason.", out)
        self.assertIn("dev setup --resume", out.split("To finish")[1])
        self.assertEqual([dev_args(c) for c in calls], [["install", "--commit-template", "yes", "--skip-packages"], ["doctor"],
                                                        ["install", "--commit-template", "yes", "--skip-packages"], ["doctor"]])
        self.assertEqual({s["id"]: s["state"] for s in line_mode.run["steps"]}["configure"], "done")
        self.assertEqual(rc, 0)  # the resumed run finished

    def test_resume_continues_saved_choices_whose_install_never_finished(self):
        """dev setup --resume in the line prompts on a Mac with saved choices but no finished install and no setup run
        (an install that failed right after Save your choices): it continues the setup with the saved choices, as
        plain dev setup and the full-screen wizard do, never "no setup is saved"."""
        rc, cfg, out, calls = line_mode(saved=SAVED, resume=True, first=False)
        self.assertNotIn("Nothing to continue", out)
        self.assertEqual([dev_args(c)[0] for c in calls if dev_args(c)], ["install", "doctor"])
        self.assertEqual(cfg, SNAPSHOT_RERUN)
        self.assertIn("✓ devkit is ready.", out)
        self.assertEqual(rc, 0)

    def test_falls_back_to_line_prompts_without_a_terminal_or_with_yes(self):
        with mock.patch.object(wizard, "_run_lines", return_value=7) as lines, \
                mock.patch.object(wizard, "_Wizard") as interactive:
            with mock.patch("sys.stdin", _Stdin(False)), mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(wizard.run(), 7)
            with mock.patch.object(tui, "available", return_value=True):
                self.assertEqual(wizard.run(assume_yes=True), 7)
            with mock.patch.dict(os.environ, {"TERM": "dumb"}), \
                    mock.patch("sys.stdin", _Stdin(True)), mock.patch("sys.stdout", _Stdin(True)):
                self.assertEqual(wizard.run(), 7)
            interactive.assert_not_called()
            self.assertEqual(lines.call_count, 3)


TERRAFORM_SKILLS = "Add the agent skills for OpenTofu / Terraform (terraform-style-guide, terraform-test)?"


class OptionalSkillGroupTests(unittest.TestCase):
    # b1993f1: skills.optional groups (iac for terraform) are offered for the tools picked, never installed by default
    def test_terraform_is_offered_its_skill_group_and_yes_adds_it_to_the_default_groups(self):
        rc, cfg, out, _ = line_mode(clouds=("aws", "terraform"))
        self.assertEqual(rc, 0)
        self.assertIn(f"{TERRAFORM_SKILLS} → yes", out)
        self.assertEqual(cfg["skills"], {"groups": core.DEFAULTS["skills"]["groups"] + ["iac"]})
        self.assertIn("aws, terraform · agent skills: iac", out)  # the plan says it

    def test_a_declined_offer_stays_declined_on_a_rerun(self):
        saved = dict(SAVED, clouds=["terraform"], skills={"groups": ["postgres"]})
        rc, cfg, out, _ = line_mode(saved=saved, clouds=("terraform",))
        self.assertIn(f"{TERRAFORM_SKILLS} → no", out)
        self.assertEqual(cfg["skills"], {"groups": ["postgres"]})
        rc, cfg, out, _ = line_mode(saved=dict(saved, clouds=[], skills={"groups": ["postgres", "iac"]}), clouds=())
        self.assertNotIn("agent skills for", out)  # terraform not picked: not asked, and the saved groups stay
        self.assertEqual(cfg["skills"], {"groups": ["postgres", "iac"]})


def fake_survey(saved=None, stacks=None, clouds=()):
    return {"cpus": 8, "ram": 16, "macos": "26.1", "arch": "arm64", "brew": True,
            "gh": {"login": "ada", "name": "\x1b[31mAda Lovelace", "id": 1},
            "providers": {"claude": {"installed": True, "logged_in": True}, "codex": {"installed": True, "logged_in": True}},
            "config": saved, "seen": {"hosts": ["github", "gitlab"], "clouds": list(clouds), "evidence": {"gitlab": "glab"}},
            "stacks_found": {"counts": stacks if stacks is not None else {"java": 2, "node": 1}, "repos": 3},
            "docker": {"colima": True, "running": True}}


class InteractiveTests(unittest.TestCase):
    """The interactive wizard driven in-process with scripted keys (detection and commands stubbed)."""

    def run_wizard(self, keys: bytes, saved=None, stacks=None, skip=True, clouds=()):
        with tempfile.TemporaryDirectory() as d:
            cfgf, state = Path(d) / "config.json", Path(d) / "state"
            if saved is not None:
                cfgf.write_text(json.dumps(saved))
            ran = []

            def fake_exec(argv, on_line=None, log=None, env=None, timeout=None):
                ran.append([str(a) for a in argv])
                if argv[-1] == "doctor" and on_line:
                    on_line("  ✓ 1/1 checks passed")
                return 0
            term = FakeTerm(keys)
            # a saved config here is a rerun: devkit was installed with it (an unfinished one is test_setup_review's)
            with mock.patch.object(wizard, "CONFIG_FILE", cfgf), mock.patch.object(wizard, "STATE", state), \
                    mock.patch.object(wizard, "installed_before", lambda: saved is not None), \
                    mock.patch.object(setup, "STATE", state), \
                    mock.patch.object(wizard, "survey", lambda: fake_survey(saved, stacks, clouds)), \
                    mock.patch.object(wizard, "_exec", fake_exec), \
                    mock.patch.object(wizard, "package_status", lambda groups: []), \
                    mock.patch.object(providers, "status", lambda include_account=True: fake_survey()["providers"]), \
                    mock.patch("subprocess.call", side_effect=AssertionError("no attached commands expected")):
                rc = wizard._Wizard(term, skip_packages=skip).run()
            cfg = json.loads(cfgf.read_text()) if cfgf.exists() else None
            # the commit template answer goes to `dev install`, which applies it; the wizard writes no state for it
            install = next((r for r in ran if dev_args(r)[:1] == ["install"]), [])
            self.template_answer = install[install.index("--commit-template") + 1] \
                if "--commit-template" in install else None
            self.assertFalse((state / "git").exists())
            return rc, cfg, term.text, ran

    def test_recommended_setup_accepts_what_was_detected(self):
        rc, cfg, text, ran = self.run_wizard(ENTER + ENTER + ENTER)  # Recommended, both agents, Apply
        self.assertEqual(rc, 0)
        self.assertIn("Step 1 of 2 · Agents", text)  # the recommended path asks which agents
        self.assertEqual(cfg["agents"], {"claude": True, "codex": True})
        self.assertEqual(cfg["stacks"], ["java", "node"])  # from the repositories, not every stack
        self.assertEqual(cfg["hosts"], ["github", "gitlab"])
        self.assertEqual(cfg["docker"]["cpu"], 6)
        self.assertEqual([dev_args(r)[0] for r in ran], ["install", "doctor"])
        self.assertEqual(self.template_answer, "yes")  # the recommended setup uses the commit template
        self.assertIn("devkit is ready.", text)
        self.assertRegex(text, r"Good (morning|afternoon|evening), Ada\.")
        self.assertNotIn("[31m", text)  # the escape sequence in the GitHub name was removed

    def test_a_first_run_with_no_repositories_asks_the_stacks_with_python_and_node_preselected(self):
        rc, cfg, text, ran = self.run_wizard(ENTER + ENTER + ENTER + ENTER, stacks={})  # … agents, stacks, Apply
        self.assertEqual(rc, 0)
        self.assertIn("Step 2 of 3 · Stacks", text)  # asked even on the recommended path
        self.assertEqual(cfg["stacks"], ["python", "node"])  # Return keeps the light preselection

    def test_customize_then_change_a_section_from_the_review(self):
        keys = (DOWN + ENTER                      # Customize
                + ENTER + b"\x15team/handoff" + ENTER  # workspace
                + DOWN + ENTER + ENTER            # agents: Claude Code only, its mode kept
                + b"a" + ENTER                    # stacks: all
                + ENTER                           # frameworks: none
                + ENTER + b"n" + ENTER            # hosts, no commit template, clouds
                + b"99" + ENTER + BS + BS + b"4" + ENTER + ENTER + ENTER + b"n"  # docker, with a fixed typo
                + b"n" + b"30" + ENTER + ENTER + ENTER  # automation: background AI stays off
                + DOWN + ENTER + b"7" + ENTER     # review → change a section → Docker
                + ENTER + ENTER + ENTER + b"y"    # … start at login after all
                + ENTER)                          # apply
        rc, cfg, text, _ = self.run_wizard(keys)
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["handoff_repo"], "team/handoff")
        self.assertEqual(cfg["agents"], {"claude": True, "codex": False})
        self.assertEqual(cfg["stacks"], ["java", "node", "python", "php"])
        self.assertEqual(cfg["docker"], {"cpu": 4, "memory": 12, "disk": 80, "autostart": True})
        self.assertEqual((cfg["gc"], cfg["sync"], cfg["lock"]), ({"auto": False}, {"interval_minutes": 30}, False))
        self.assertIn("Enter a whole number from 1 to 8.", text)
        self.assertIn("Step 7 of 9 · Docker", text)
        self.assertEqual(self.template_answer, "no")
        self.assertIn("commit template off", text)  # the review says it

    def test_esc_on_the_first_question_returns_to_the_welcome(self):
        rc, cfg, text, _ = self.run_wizard(DOWN + ENTER + ESC + UP + ENTER + ENTER + ENTER)  # focus back on Customize
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(text.count("How do you want to set up devkit?"), 2)
        self.assertIn("Which coding agents do you want to use?", text.split("How do you want to set up devkit?")[-1])
        self.assertEqual(cfg["stacks"], ["java", "node"])

    def test_quit_and_ctrl_c_change_nothing(self):
        rc, cfg, text, ran = self.run_wizard(ENTER + ENTER + DOWN + DOWN + ENTER)
        self.assertEqual((rc, cfg, ran), (1, None, []))
        self.assertIn("Nothing changed.", text)
        rc, cfg, text, ran = self.run_wizard(DOWN + ENTER + CTRL_C + CTRL_C)  # Ctrl-C asks first; a second one quits
        self.assertEqual((rc, cfg, ran), (1, None, []))
        self.assertIn("Quit the setup?", text)
        self.assertIn("Nothing changed. Run dev setup when you are ready.", text)

    def test_a_rerun_keeps_saved_selections_despite_detected_tools(self):
        saved = dict(SNAPSHOT_FIRST, hosts=["github"], clouds=[], stacks=["php"])
        rc, cfg, text, _ = self.run_wizard(ENTER + ENTER, saved=saved)
        self.assertIn("devkit is already set up here.", text)
        self.assertIn("Keep current setup", text)
        self.assertIn("No changes from your saved setup", text)
        self.assertEqual(cfg["stacks"], ["php"])  # saved stacks win over detection
        self.assertEqual(cfg["hosts"], ["github"])

    def test_a_rerun_without_changes_says_so(self):
        saved = dict(SNAPSHOT_FIRST, hosts=["github", "gitlab"], clouds=[], stacks=["java"])
        rc, cfg, text, _ = self.run_wizard(ENTER + DOWN + DOWN + ENTER, saved=saved)
        self.assertIn("No changes from your saved setup", text)
        self.assertEqual(rc, 1)

    def test_terraform_found_gets_its_skill_group_offered_in_the_clouds_section(self):
        # Recommended: what was found, the offer accepted, and the review says so
        rc, cfg, text, _ = self.run_wizard(ENTER + ENTER + ENTER, clouds=("terraform",))
        self.assertEqual(cfg["skills"]["groups"], core.DEFAULTS["skills"]["groups"] + ["iac"])
        self.assertIn("OpenTofu / Terraform · agent skills: iac", text)
        # Customize: the question follows the clouds, and No keeps the default groups
        keys = (DOWN + ENTER + ENTER * 2 + ENTER * 3 + ENTER * 2 + ENTER * 2 + ENTER  # to clouds, frameworks kept
                + b"n" + ENTER * 3 + b"n" + b"n" + ENTER + ENTER + b"n"   # no skills; docker; automation
                + ENTER)                                                  # apply
        rc, cfg, text, _ = self.run_wizard(keys, clouds=("terraform",))
        self.assertIn(TERRAFORM_SKILLS, text)
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["skills"]["groups"], core.DEFAULTS["skills"]["groups"])


class DoctorToolsTests(unittest.TestCase):
    def test_every_doctor_tool_comes_from_a_package_of_its_group(self):
        # `dev doctor` requires a group's tools only when that group is installed, and names the package as the fix:
        # both are wrong when the tool table drifts from the Brewfile or npm-globals.txt, or misses a stack.
        repo = Path(__file__).resolve().parents[1]
        self.assertEqual(set(wizard.DOCTOR_TOOLS) - set(wizard.BASE_GROUPS),
                         set(wizard.STACKS) | set(wizard.agent_groups({})) | {"angular"})
        for group, tools in wizard.DOCTOR_TOOLS.items():
            listed = {m.groups() for m in map(wizard._BREW_RE.match, wizard.filtered(repo / "Brewfile", {group})) if m}
            listed |= {("npm", n) for n in wizard.filtered(repo / "config" / "npm-globals.txt", {group})}
            for tool, (kind, package) in tools.items():
                if kind != "script":
                    self.assertIn((kind, package), listed, f"{tool} ({group})")


class DefaultsAndDetectionTests(unittest.TestCase):
    def test_stack_defaults(self):
        self.assertEqual(wizard.tui_defaults(fake_survey())["stacks"], ["java", "node"])
        self.assertEqual(wizard.tui_defaults(fake_survey(stacks={}))["stacks"], ["python", "node"])  # light first run
        self.assertEqual(wizard.tui_defaults(fake_survey({"stacks": ["php"]}))["stacks"], ["php"])
        self.assertEqual(wizard.tui_defaults(fake_survey({"stacks": []}))["stacks"], [])

    def test_docker_defaults_never_exceed_the_machine(self):
        d = dict(fake_survey(), cpus=4, ram=8)
        a = wizard.tui_defaults(d)
        self.assertEqual((a["cpu"], a["mem"]), (4, 8))

    def test_detect_stacks_from_the_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = Path(tmp) / "Development"
            for owner, repo, files in (("acme", "api", {"pom.xml": ""}), ("acme", "web", {"package.json": "{}"}),
                                       ("acme", "shop", {"composer.json": "{}", "artisan": ""}),
                                       ("ada", "lab", {"pyproject.toml": "", "package.json": "{}"})):
                (dev / owner / repo / ".git").mkdir(parents=True)
                for n, t in files.items():
                    (dev / owner / repo / n).write_text(t)
            with mock.patch.object(registry, "DEV", dev), mock.patch.object(registry, "LOCAL", dev / "_local"), \
                    mock.patch.object(registry, "WT_ROOT", dev / ".worktrees"):
                found = wizard.detect_stacks()
        self.assertEqual(found, {"counts": {"java": 1, "node": 2, "php": 1, "python": 1},
                                 "frameworks": {"laravel": 1}, "repos": 4})

    def test_package_status_marks_what_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, npm = Path(tmp) / "brew", Path(tmp) / "npm"
            for p in (prefix / "Cellar" / "uv", prefix / "opt" / "python", prefix / "Caskroom" / "codex", npm / "pyright"):
                p.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": str(prefix)}), \
                    mock.patch.object(wizard, "_NPM_ROOT", [npm]):
                status = dict(wizard.package_status(["core", "codex", "python"]))
        self.assertIn(("python", "brew", True), status["core"])
        self.assertIn(("git", "brew", False), status["core"])
        self.assertEqual(status["codex"], [("codex", "cask", True)])
        self.assertEqual(status["python"], [("uv", "brew", True), ("pyright", "npm", True)])

    def test_exec_streams_lines_to_the_log_and_returns_the_exit_code(self):
        class Log:
            text = ""

            def write(self, s):
                Log.text += s
        lines = []
        code = "import sys; print('one'); print('50%\\r100%'); sys.stdout.buffer.write(b'\\xff\\n'); sys.exit(3)"
        self.assertEqual(wizard._exec([sys.executable, "-u", "-c", code], lines.append, Log()), 3)
        self.assertEqual(lines, ["one", "100%", "�"])
        self.assertIn("one\n", Log.text)
        self.assertEqual(wizard._exec(["/nonexistent/devkit-test-binary"], lines.append), 127)

    def test_exec_returns_when_the_command_exits_though_a_process_it_started_keeps_its_output_open(self):
        code = "import subprocess, sys; subprocess.Popen(['sleep', '30']); print('started'); sys.exit(0)"
        t0, lines = time.monotonic(), []
        self.assertEqual(wizard._exec([sys.executable, "-u", "-c", code], lines.append), 0)
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(lines, ["started"])

    def test_validators(self):
        self.assertIsNone(wizard._int_in(1, 8)("8"))
        self.assertTrue(wizard._int_in(1, 8)("9"))
        self.assertTrue(wizard._int_in(1, 8)("x"))
        self.assertIsNone(wizard._valid_repo("ada")("team/handoff"))
        self.assertTrue(wizard._valid_repo("ada")("--help/x"))
        self.assertTrue(wizard._valid_repo("ada")("no-slash"))
        self.assertIsNone(wizard._valid_root("~/Development"))
        self.assertTrue(wizard._valid_root("relative/dir"))
        self.assertTrue(wizard._valid_root("~/bad\x1b[2Jpath"))


class PlaywrightSkillTests(unittest.TestCase):
    def run_install(self, rc=0):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root, share = Path(tmp) / "Development" / ".tmp", Path(tmp) / "share" / "skills"
            (share / "playwright-cli").mkdir(parents=True)
            (share / "playwright-cli" / "old.md").write_text("old")
            seen = []

            def fake_call(argv, **kw):
                seen.append((list(argv), kw.get("cwd")))
                if argv[:2] == ["playwright-cli", "install"] and rc == 0:
                    skill = Path(kw["cwd"]) / ".claude" / "skills" / "playwright-cli"
                    skill.mkdir(parents=True)
                    (skill / "SKILL.md").write_text("---\nname: playwright-cli\n---\n")
                    (Path(kw["cwd"]) / ".gitignore").write_text(".claude/\n")
                return rc if argv[0] == "playwright-cli" else 0
            with mock.patch.object(core, "TMP_ROOT", tmp_root), mock.patch.object(setup, "SKILLS_SHARE", share), \
                    mock.patch.object(core, "have", lambda t: True), mock.patch("subprocess.call", fake_call), \
                    mock.patch.object(toolchain, "postinstall", lambda keys: []), \
                    mock.patch("sys.stdout", io.StringIO()):
                wizard.install_packages(["node"], with_claude=False)
            runs = [cwd for argv, cwd in seen if argv[0] == "playwright-cli"]
            return runs, tmp_root, share, [p.name for p in (share / "playwright-cli").iterdir()]

    def test_runs_in_a_temp_folder_and_keeps_only_the_skill(self):
        runs, tmp_root, share, files = self.run_install()
        self.assertEqual(len(runs), 1)
        cwd = Path(runs[0])
        self.assertTrue(cwd.is_absolute())
        self.assertTrue(core.is_under(cwd, tmp_root), cwd)
        self.assertNotEqual(core.real(cwd), core.real(os.getcwd()))
        self.assertTrue(cwd.name.startswith("playwright-skill-"))
        self.assertEqual(files, ["SKILL.md"])  # the old copy was replaced
        self.assertFalse(cwd.exists())  # and the temp folder removed

    def test_a_failed_install_leaves_the_old_copy_and_no_temp_folder(self):
        runs, tmp_root, share, files = self.run_install(rc=1)
        self.assertEqual(files, ["old.md"])
        self.assertFalse(Path(runs[0]).exists())


class HomebrewAskTests(unittest.TestCase):
    """Homebrew asks "Do you want to proceed with the installation? [y/n]" by default; HOMEBREW_NO_ASK, the switch it
    documents, turns that off for every brew the setup runs."""

    def test_the_packages_step_of_the_wizard_runs_brew_without_asking(self):
        envs = {}

        def fake_exec(argv, on_line=None, log=None, env=None, timeout=None):
            envs[str(argv[0])] = env or {}
            return 0
        w = wizard._Wizard(FakeTerm())
        w._pk = {(): []}
        w.a = {"stacks": [], "hosts": [], "clouds": []}
        with mock.patch.object(wizard, "_exec", fake_exec), mock.patch.object(toolchain, "postinstall", lambda k: []), \
                tui.Checklist(w.term, ["Install Homebrew packages"], animate=False) as cl:
            w._brew(cl, 0, {"brew": ['brew "jq"'], "post": []})
        self.assertEqual(envs["brew"].get("HOMEBREW_NO_ASK"), "1")

    def test_dev_install_runs_brew_bundle_without_asking(self):
        envs = []

        def fake_call(argv, **kw):
            if list(argv[:2]) == ["brew", "bundle"]:
                envs.append(kw.get("env") or {})
            return 0
        with mock.patch("subprocess.call", fake_call), mock.patch.object(core, "have", lambda t: False), \
                mock.patch.object(toolchain, "postinstall", lambda keys: []), mock.patch("sys.stdout", io.StringIO()):
            wizard.install_packages([], with_claude=False)
        self.assertEqual([e.get("HOMEBREW_NO_ASK") for e in envs], ["1"])


# --------------------------------------------------------------------------- a failed step says why

# Stand-ins for devkit's bin/dev, run by the wizard's Configure step as it runs `dev install`: with the interpreter
# flags of bin/dev's shebang (-I -S), output captured, no terminal.
BANNER = ('print("\\n  devkit\\n\\n  Your development kit\\n  v26.11.8 · install  ·  © 2026 Firefly Software Solutions '
          'Inc.\\n")\nprint("▸ Pinned copy of devkit (what the hooks and the guard run)")\n')
FAILS_ON_STDERR = "import sys\n" + BANNER + (
    'sys.stderr.write("git archive 1a2b3c4 failed: fatal: not a tree object\\n")\nsys.exit(1)\n')
CRASHES = "import os, sys\nsys.path.insert(0, os.environ['WIZ_LIB'])\nfrom devkit import bugs\n" + BANNER + (
    'try:\n    {}["path"]\nexcept KeyError as e:\n    sys.exit(bugs.handle_crash(["install", "--as-pinned"], e))\n')
KILLED = "import os, signal\n" + BANNER + (
    'print("  ✓ running from ~/.local/share/devkit/releases/26.11.8-1a2b3c4")\n'
    'print("▸ Codex (CLI + ChatGPT app): settings, hooks, plugins, MCP")\nos.kill(os.getpid(), signal.SIGKILL)\n')


class FailedStepTests(unittest.TestCase):
    """The Set up devkit step runs `dev install` as a child. When it fails, the step says why in one line (its error
    line, its crash, or the signal that stopped it), never the banner it printed first, and the log keeps every line
    it printed."""

    def configure(self, script: str):
        class Log:
            text = ""

            def write(self, s):
                self.text += s
        with tempfile.TemporaryDirectory() as tmp:
            kit, home = Path(tmp) / "devkit", Path(tmp) / "home"
            (kit / "bin").mkdir(parents=True)
            home.mkdir()
            (kit / "bin" / "dev").write_text(script)
            w = wizard._Wizard(FakeTerm())
            w.a, w.log = {"commit_template": True}, Log()
            st = wizard._Step("configure", "Set up devkit", True, w._configure, "dev setup --resume", "",
                              "devkit's rules are not set up yet.")
            # the crash notice is written by devkit's own handler, whose files go to this temporary HOME
            with mock.patch.object(wizard, "DEVKIT_HOME", kit), mock.patch.object(wizard, "STATE", Path(tmp)), \
                    mock.patch.dict(os.environ, {"HOME": str(home), "WIZ_LIB": LIB}), \
                    tui.Checklist(w.term, ["Set up devkit"], animate=False) as cl:
                run = wizard.SetupRun.start(w.a)
                run.plan([(st.sid, st.title, True, st.fix)])
                w._attempt(cl, 0, st, run)
            it = cl.items[0]
            crashes = sorted(p.name for p in (home / ".local" / "state" / "devkit" / "crashes").glob("*"))
            notes = [plain(n) for n in it.notes]
            reason = next((n for n in notes if n.startswith("Reason: ")), "")
            return it.state, reason, notes, crashes, w.log.text

    def test_an_error_on_stderr_is_the_reason_and_never_the_banner(self):
        state, reason, notes, _, _ = self.configure(FAILS_ON_STDERR)
        self.assertEqual(state, "fail")
        self.assertEqual(reason, "Reason: git archive 1a2b3c4 failed: fatal: not a tree object")
        for banner in ("Your development kit", "© 2026", "v26.11.8 · install"):
            self.assertNotIn(banner, "\n".join(notes))

    def test_a_crash_names_the_exception_and_the_crash_file(self):
        state, reason, notes, crashes, _ = self.configure(CRASHES)
        self.assertEqual(state, "fail")
        self.assertEqual(reason, "Reason: devkit hit a bug while pinning its copy: KeyError: 'path'")
        self.assertEqual(len(crashes), 1)
        self.assertIn(f"Bug report saved: ~/.local/state/devkit/crashes/{crashes[0]}", "\n".join(notes))

    def test_a_signal_is_the_reason_and_the_lines_printed_before_it_are_kept_in_the_log(self):
        state, reason, notes, _, log = self.configure(KILLED)
        self.assertEqual(state, "fail")
        self.assertEqual(reason, "Reason: dev install was stopped by SIGKILL and gave no reason.")
        self.assertIn("running from ~/.local/share/devkit/releases/26.11.8-1a2b3c4", log)
        self.assertIn("▸ Codex (CLI + ChatGPT app): settings, hooks, plugins, MCP", log)


# --------------------------------------------------------------------------- interruptions, failures and resume

class SignInLimitTests(unittest.TestCase):
    """The sign-in screens, Ctrl-C and resume are in test_setup_flow.py; this one runs a real command."""

    def test_a_sign_in_past_its_time_limit_is_stopped(self):
        t0 = time.monotonic()
        result = wizard._attached([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5,
                                  device=os.devnull)
        self.assertEqual(result, "timeout")
        self.assertLess(time.monotonic() - t0, 10)


class SignInTerminalTests(unittest.TestCase):
    """A sign-in gets this Mac's terminal by its own device. Claude Code's runtime watches its input with kqueue, which
    macOS refuses on /dev/tty (EINVAL): the installer run through a pipe gave the wizard /dev/tty, and the sign-in
    crashed with "EINVAL: invalid argument, kqueue". The stand-in for `claude` fails the same way."""

    CLAUDE = textwrap.dedent('''
        import select, sys
        try:
            select.kqueue().control([select.kevent(0, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0, 0)
        except OSError as e:
            sys.stderr.write("EINVAL: invalid argument, kqueue (%s)\\n" % e.strerror)
            sys.exit(1)
    ''')
    DRIVER = textwrap.dedent('''
        import os, sys
        sys.path.insert(0, sys.argv[1])
        if sys.argv[3] == "dev-tty":  # what the installer gave the wizard: the terminal opened as /dev/tty
            os.dup2(os.open("/dev/tty", os.O_RDWR), 0)
        from devkit import wizard
        print("result:", wizard._attached([sys.executable, sys.argv[2]], timeout=30))
    ''')

    def run_driver(self, stdin: str, terminal=True) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            claude, driver = Path(tmp) / "claude.py", Path(tmp) / "driver.py"
            claude.write_text(self.CLAUDE)
            driver.write_text(self.DRIVER)
            argv = [sys.executable, str(driver), LIB, str(claude), stdin]
            if not terminal:
                p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                                   start_new_session=True)
                return p.stdout + p.stderr
            import pty
            master, slave = pty.openpty()
            p = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                 preexec_fn=_take_terminal)
            os.close(slave)
            out = b""
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            p.wait(30)
            os.close(master)
            return out.decode("utf-8", "replace")

    @unittest.skipUnless(sys.platform == "darwin", "kqueue and its refusal of /dev/tty are macOS's")
    def test_a_sign_in_gets_the_terminal_device_kqueue_accepts_when_stdin_is_dev_tty(self):
        out = self.run_driver("dev-tty")
        self.assertIn("result: ok", out)
        self.assertNotIn("EINVAL", out)

    def test_without_a_terminal_nothing_is_started(self):
        out = self.run_driver("as-is", terminal=False)
        self.assertIn("result: no-terminal", out)


def _take_terminal():
    """In the child, after setsid: make the pseudo-terminal on stdin its controlling terminal."""
    import fcntl
    import termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


# --------------------------------------------------------------------------- the whole flow in a pseudo-terminal

DRIVER = textwrap.dedent('''
    import json, os, subprocess, sys
    sys.path.insert(0, os.environ["WIZ_LIB"])
    from devkit import core, providers, toolchain, wizard

    def record(kind, argv, **kw):
        with open(os.environ["WIZ_CALLS"], "a") as f:
            f.write(json.dumps({"kind": kind, "argv": [str(a) for a in argv], "cwd": kw.get("cwd")}) + "\\n")

    wizard._hw = lambda: (8, 16, "26.1", "arm64")
    core.gh_user = lambda: {"login": "ada", "name": "Ada Lovelace", "id": 1}
    core.have = lambda t: t in ("gh", "brew", "colima", "claude", "codex", "npm")
    providers.status = lambda include_account=True: {"claude": {"installed": True, "logged_in": True},
                                                     "codex": {"installed": True, "logged_in": True}}
    toolchain.detect = lambda: {"hosts": ["github"], "clouds": [], "evidence": {}}
    toolchain.postinstall = lambda keys: []
    wizard.detect_docker = lambda: {"colima": True, "running": True}
    wizard._NPM_ROOT[:] = [None]
    core.sh = lambda args, **kw: (1, "", "stubbed")

    def fake_exec(argv, on_line=None, log=None, env=None, timeout=None):
        record("exec", argv)
        if str(argv[-1]) == "doctor" and on_line:
            on_line("  ✓ 1/1 checks passed")
        return 0

    def fake_call(argv, **kw):
        record("call", argv, **kw)
        return 0

    def refuse(*a, **k):
        raise RuntimeError("unexpected process: %r" % (a,))

    wizard._exec = fake_exec
    subprocess.call = fake_call
    subprocess.Popen = subprocess.run = refuse
    sys.exit(wizard.run(skip_packages="--skip-packages" in sys.argv))
''')


class Pty:
    """The wizard in a pseudo-terminal with a temporary HOME; send keys, wait for text on the screen."""

    def __init__(self, home: Path, term="xterm-256color", cols=100, rows=40):
        import fcntl
        import pty
        import struct
        import termios
        self.home = home
        (home / "driver.py").write_text(DRIVER)
        dev = home / "Development" / "acme" / "api"
        (dev / ".git").mkdir(parents=True)
        (dev / "pom.xml").write_text("<project/>")
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TERM": term, "LANG": "en_US.UTF-8", "WIZ_LIB": LIB,
               "WIZ_CALLS": str(home / "calls.jsonl"), "HOMEBREW_PREFIX": str(home / "brew")}
        env = _sandbox.env(env)  # its devkit keeps the run's stand-ins first, never this Mac's real tools
        self.master, slave = pty.openpty()
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.proc = subprocess.Popen([sys.executable, str(home / "driver.py")], stdin=slave, stdout=slave,
                                     stderr=slave, env=env, cwd=str(home), close_fds=True)
        os.close(slave)
        self.out, self.mark = b"", 0

    def _read(self, timeout):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                chunk = b""
            self.out += chunk
            return bool(chunk)
        return True

    def text(self, start=0) -> str:
        return plain(self.out[start:].decode("utf-8", "replace"))

    def expect(self, s: str, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if s in self.text(self.mark):
                self.mark = len(self.out)
                return
            if not self._read(0.1) and self.proc.poll() is not None:
                break
        raise AssertionError(f"{s!r} not shown; screen:\n{self.text()[-2000:]}")

    def send(self, data: bytes):
        os.write(self.master, data)
        time.sleep(0.05)

    def wait(self, timeout=20) -> int:
        end = time.time() + timeout
        while time.time() < end and self.proc.poll() is None:
            self._read(0.1)
        while self._read(0.05) and select.select([self.master], [], [], 0)[0]:
            pass
        os.close(self.master)
        return self.proc.wait(5)

    def calls(self) -> list:
        f = self.home / "calls.jsonl"
        return [json.loads(ln) for ln in f.read_text().splitlines()] if f.exists() else []

    def config(self):
        f = self.home / ".config" / "devkit" / "config.json"
        return json.loads(f.read_text()) if f.exists() else None


@unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "needs a POSIX pseudo-terminal")
class PseudoTerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow_with_scripted_keys(self):
        t = Pty(self.home)
        t.expect("How do you want to set up devkit?")
        t.send(DOWN + ENTER)
        t.expect("Development folder")
        t.send(ENTER)
        t.expect("Handoff repository")
        t.send(b"\x15team/handoff" + ENTER)
        t.expect("Which coding agents do you want to use?")
        t.send(DOWN + ENTER)
        t.expect("Claude Code permissions")
        t.send(DOWN + ENTER)
        t.expect("Stacks you work in")
        t.send(b"a" + ENTER)
        t.expect("Frameworks you work in")
        t.send(ENTER)
        t.expect("Where your code and pipelines live")
        t.send(ENTER)
        t.expect("Open new commit messages with devkit's template?")
        t.send(ENTER)
        t.expect("Clouds and infrastructure tools")
        t.send(ENTER)
        t.expect("VM CPUs")
        t.send(b"99" + ENTER)
        t.expect("Enter a whole number from 1 to 8.")
        t.send(BS + BS + b"4" + ENTER)
        t.expect("VM memory (GB)")
        t.send(ENTER)
        t.expect("VM disk (GB)")
        t.send(ESC)
        t.expect("VM memory (GB)")
        t.send(b"6" + ENTER)
        t.expect("VM disk (GB)")
        t.send(ENTER)
        t.expect("Start Colima at login?")
        t.send(b"y")
        t.expect("Reclaim disk space automatically?")
        t.send(b"n")
        t.expect("Background sync interval (minutes)")
        t.send(b"30" + ENTER)
        t.expect("Let background jobs ask a small model")
        t.send(ENTER)
        t.expect("Lock Claude Code settings?")
        t.send(ENTER)
        t.expect("Apply this plan?")
        t.send(ENTER)
        t.expect("devkit is ready.")
        self.assertEqual(t.wait(), 0)
        self.assertEqual(t.config(), {
            "dev_root": "~/Development", "github_user": "ada", "handoff_repo": "team/handoff",
            "agents": {"claude": True, "codex": False}, "claude": {"default_mode": "acceptEdits"},
            "codex": {"mode": "full", "approval_policy": "never", "sandbox_mode": "danger-full-access"},
            "stacks": ["java", "python", "php", "node"], "lock": False, "hosts": ["github"], "clouds": [],
            "docker": {"cpu": 4, "memory": 6, "disk": 80, "autostart": True}, "gc": {"auto": False},
            "sync": {"interval_minutes": 30}, "ai": {"enabled": False, "asked": True}, "frameworks": []})
        ran = [c["argv"][0] if c["argv"][0] in ("brew", "npm") else dev_args(c["argv"])[0] for c in t.calls()]
        self.assertEqual(ran, ["brew", "npm", "install", "doctor"])
        self.assertIn(["install", "--commit-template", "yes"], [dev_args(c["argv"]) for c in t.calls()])
        log = self.home / ".local" / "state" / "devkit" / "logs" / "setup.log"
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        raw = t.out.decode("utf-8", "replace")
        self.assertIn("\x1b[?1049h", raw)
        self.assertLess(raw.rindex("\x1b[?1049l"), raw.index("Applying your plan"))

    def test_ctrl_c_asks_then_restores_the_terminal_and_changes_nothing(self):
        t = Pty(self.home)
        t.expect("How do you want to set up devkit?")
        t.send(CTRL_C)
        t.expect("Quit the setup?")
        t.send(CTRL_C)
        self.assertEqual(t.wait(), 1)
        raw = t.out.decode("utf-8", "replace")
        self.assertIn("Nothing changed. Run dev setup when you are ready.", plain(raw))
        self.assertTrue(raw.rindex("\x1b[?1049l") < raw.rindex("Nothing changed"))
        self.assertIn("\x1b[?25h", raw)
        self.assertIsNone(t.config())
        self.assertEqual(t.calls(), [])

    def test_term_dumb_uses_the_line_prompts(self):
        t = Pty(self.home, term="dumb")
        t.expect("Development root")
        t.send(b"\n" * 24)
        t.expect("devkit is ready.")
        self.assertEqual(t.wait(), 0)
        raw = t.out.decode("utf-8", "replace")
        self.assertNotIn("\x1b[?1049h", raw)
        self.assertIn("1 This machine", raw)
        self.assertEqual(t.config()["stacks"], ["python", "node"])  # the light first-run default


if __name__ == "__main__":
    unittest.main()
