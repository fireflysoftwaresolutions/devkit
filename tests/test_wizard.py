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

from devkit import core, providers, registry, setup, toolchain, tui, ui, wizard  # noqa: E402

LIB = str(Path(__file__).resolve().parents[1] / "lib")
UP, DOWN, ENTER, ESC, SPACE, CTRL_C, BS = b"\x1b[A", b"\x1b[B", b"\r", b"\x1b", b" ", b"\x03", b"\x7f"


def plain(s) -> str:
    return tui.strip(s)


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


SNAPSHOT_FIRST = {
    "dev_root": "~/Development", "github_user": "ada", "handoff_repo": "ada/handoff",
    "agents": {"claude": True, "codex": True}, "claude": {"default_mode": "bypassPermissions"},
    "codex": {"mode": "full", "approval_policy": "never", "sandbox_mode": "danger-full-access"},
    "stacks": ["python", "node"], "lock": False, "hosts": ["github", "gitlab"], "clouds": ["aws"],
    "docker": {"cpu": 6, "memory": 12, "disk": 80, "autostart": False}, "gc": {"auto": True},
    "sync": {"interval_minutes": 60}}
SAVED = {"stacks": ["python"], "hosts": ["bitbucket"], "clouds": [], "docker": {"cpu": 4, "memory": 8},
         "editor": "zed", "agents": {"claude": True, "codex": False}, "lock": True}
SNAPSHOT_RERUN = {
    "stacks": ["python"], "hosts": ["github", "bitbucket", "gitlab"], "clouds": ["aws"],
    "docker": {"cpu": 4, "memory": 8, "disk": 80, "autostart": False}, "editor": "zed",
    "agents": {"claude": True, "codex": True}, "lock": True, "dev_root": "~/Development", "github_user": "ada",
    "handoff_repo": "ada/handoff", "claude": {"default_mode": "bypassPermissions"},
    "codex": {"mode": "full", "approval_policy": "never", "sandbox_mode": "danger-full-access"},
    "gc": {"auto": True}, "sync": {"interval_minutes": 60}}
# The line prompts' output with --yes, as `dev setup` printed it before the interactive wizard existed.
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
     Configure Claude Code (CLI + Desktop)? → yes
     Configure Codex (CLI + ChatGPT app)? → yes
     › 1. bypassPermissions — no prompts; the devkit guard protects the workspace (recommended)
       2. acceptEdits — edits without prompts, commands ask
       3. default — ask before every tool
     Claude Code permission mode → bypassPermissions
     › 1. full — no prompts, full access; the devkit guard protects (recommended)
       2. workspace — sandboxed to the project; asks when it needs more
     Codex autonomy → full

   4 Stacks (toolchains, language servers, skills)
     □ 1. java — Java / Spring (JDK, Maven, Gradle, jdtls)
     ■ 2. python — Python (uv, pyright)
     □ 3. php — PHP (php, Composer, intelephense)
     ■ 4. node — Node / TypeScript / Angular / React (pnpm, tsc, ng)
     Stacks you develop in (comma-separated numbers) → python, node

   5 Git hosts and CI/CD (CLIs, sign-in checks, `dev ci status`)
     found on this Mac: gitlab (x); aws (y)
     ■ 1. github — GitHub · Actions
     ■ 2. gitlab — GitLab · GitLab CI
     □ 3. bitbucket — Bitbucket · Pipelines
     □ 4. azure-devops — Azure DevOps · Pipelines
     Where your repositories and pipelines live (comma-separated numbers) → github, gitlab

   6 Cloud and infrastructure (CLIs and sign-in checks)
     ■ 1. aws — AWS
     □ 2. azure — Microsoft Azure
     □ 3. gcp — Google Cloud
     □ 4. digitalocean — DigitalOcean
     □ 5. kubernetes — Kubernetes · Helm
     □ 6. terraform — OpenTofu / Terraform
     Clouds and infrastructure tools you use (numbers, or empty for none) → aws

   7 Docker on Colima
     VM CPUs → 6
     VM memory (GB) → 12
     VM disk, grows as needed (GB) → 80
     Start Colima at login? → no

   8 Automation
     Reclaim disk automatically (merged worktrees, idle build output, stale temp)? → yes
     Background sync interval (minutes) → 60
     Lock Claude Code settings with a managed-settings file (needs your admin password)? → no

   9 Plan
     config       <CONFIG>
     root         ~/Development
     handoff      ada/handoff → ~/Development/.handoff
     Claude Code  bypassPermissions
     Codex        full (never / danger-full-access)
     stacks       python, node
     git hosts    github, gitlab
     clouds       aws
     packages     skip
     Docker VM    6 CPU · 12 GB · 80 GB
     automation   sync every 60 min · safe cleanup on
     lock         off (drift is still blocked and repaired)
     Apply this plan? → yes

  ✓ saved <CONFIG>
     Codex is not signed in. Sign in now (opens the browser)? → yes

  → Verifying …

  Next: open a new terminal (or `exec zsh`), then `dev` — and in any project, start Claude Code or Codex as \
usual. `dev agents test` runs a live check of both agents.

"""


def line_mode(saved=None, yes=True, typed=None):
    """Run `dev setup` through the line prompts with detection and commands stubbed: (rc, config, output, calls)."""
    with tempfile.TemporaryDirectory() as d:
        cfgf = Path(d) / "config.json"
        if saved is not None:
            cfgf.write_text(json.dumps(saved))
        calls, answers = [], iter(typed or [])
        with mock.patch.object(wizard, "CONFIG_FILE", cfgf), \
                mock.patch.object(wizard, "_hw", lambda: (12, 36, "26.1", "arm64")), \
                mock.patch.object(core, "gh_user", lambda: {"login": "ada", "name": "Ada Lovelace", "id": 1}), \
                mock.patch.object(core, "have", lambda t: t in ("gh", "brew")), \
                mock.patch.object(providers, "status", lambda include_account=True: {
                    "claude": {"installed": True, "logged_in": True}, "codex": {"installed": True, "logged_in": False}}), \
                mock.patch.object(toolchain, "detect", lambda: {"hosts": ["github", "gitlab"], "clouds": ["aws"],
                                                                "evidence": {"gitlab": "x", "aws": "y"}}), \
                mock.patch("subprocess.call", lambda argv, **kw: calls.append(list(argv)) or 0), \
                mock.patch("builtins.input", lambda prompt="": (print(prompt, end=""), next(answers))[1]), \
                mock.patch("sys.stdin", _Stdin(not yes)), mock.patch("sys.stdout", io.StringIO()) as out:
            rc = wizard.run(assume_yes=yes, skip_packages=True)
        text = ui.ANSI_RE.sub("", out.getvalue()).replace(str(cfgf), "<CONFIG>")
        text = re.sub(r"Good (morning|afternoon|evening)", "Good <time>", text)
        return rc, (json.loads(cfgf.read_text()) if cfgf.exists() else None), text[text.index("Good <time>"):], calls


class LineModeTests(unittest.TestCase):
    def test_yes_writes_the_same_config_as_before(self):
        rc, cfg, out, calls = line_mode()
        self.assertEqual(rc, 0)
        self.assertEqual(cfg, SNAPSHOT_FIRST)
        self.assertEqual(list(cfg), list(SNAPSHOT_FIRST))  # same key order in the file
        self.assertEqual([c[-1] for c in calls], ["install", "doctor"])

    def test_yes_on_a_configured_mac_keeps_the_saved_values(self):
        rc, cfg, _, _ = line_mode(saved=SAVED)
        self.assertEqual(cfg, SNAPSHOT_RERUN)

    def test_yes_prints_the_line_prompts_unchanged(self):
        self.assertEqual(line_mode()[2], GOLDEN_YES)

    def test_typed_answers_in_the_line_prompts(self):
        typed = ["~/Code", "", "n", "y", "2", "1,3", "", "", "8", "", "", "y", "n", "30", "", "y"]
        rc, cfg, out, _ = line_mode(yes=False, typed=typed)
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["dev_root"], "~/Code")
        self.assertEqual(cfg["agents"], {"claude": False, "codex": True})
        self.assertEqual(cfg["codex"], {"mode": "workspace", "approval_policy": "on-request",
                                        "sandbox_mode": "workspace-write"})
        self.assertEqual(cfg["stacks"], ["java", "php"])
        self.assertEqual(cfg["docker"], {"cpu": 8, "memory": 12, "disk": 80, "autostart": True})
        self.assertEqual((cfg["gc"], cfg["sync"], cfg["lock"]), ({"auto": False}, {"interval_minutes": 30}, False))
        self.assertIn("[Y/n]", out)

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


def fake_survey(saved=None, stacks=None):
    return {"cpus": 8, "ram": 16, "macos": "26.1", "arch": "arm64", "brew": True,
            "gh": {"login": "ada", "name": "\x1b[31mAda Lovelace", "id": 1},
            "providers": {"claude": {"installed": True, "logged_in": True}, "codex": {"installed": True, "logged_in": True}},
            "config": saved, "seen": {"hosts": ["github", "gitlab"], "clouds": [], "evidence": {"gitlab": "glab"}},
            "stacks_found": {"counts": stacks if stacks is not None else {"java": 2, "node": 1}, "repos": 3},
            "docker": {"colima": True, "running": True}}


class InteractiveTests(unittest.TestCase):
    """The interactive wizard driven in-process with scripted keys (detection and commands stubbed)."""

    def run_wizard(self, keys: bytes, saved=None, stacks=None, skip=True):
        with tempfile.TemporaryDirectory() as d:
            cfgf, state = Path(d) / "config.json", Path(d) / "state"
            if saved is not None:
                cfgf.write_text(json.dumps(saved))
            ran = []

            def fake_exec(argv, on_line=None, log=None, env=None):
                ran.append([str(a) for a in argv])
                if argv[-1] == "doctor" and on_line:
                    on_line("  ✓ 1/1 checks passed")
                return 0
            term = FakeTerm(keys)
            with mock.patch.object(wizard, "CONFIG_FILE", cfgf), mock.patch.object(wizard, "STATE", state), \
                    mock.patch.object(wizard, "survey", lambda: fake_survey(saved, stacks)), \
                    mock.patch.object(wizard, "_exec", fake_exec), \
                    mock.patch.object(wizard, "package_status", lambda groups: []), \
                    mock.patch.object(providers, "status", lambda include_account=True: fake_survey()["providers"]), \
                    mock.patch("subprocess.call", side_effect=AssertionError("no attached commands expected")):
                rc = wizard._Wizard(term, skip_packages=skip).run()
            cfg = json.loads(cfgf.read_text()) if cfgf.exists() else None
            return rc, cfg, term.text, ran

    def test_recommended_setup_accepts_what_was_detected(self):
        rc, cfg, text, ran = self.run_wizard(ENTER + ENTER)
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["stacks"], ["java", "node"])  # from the repositories, not every stack
        self.assertEqual(cfg["hosts"], ["github", "gitlab"])
        self.assertEqual(cfg["docker"]["cpu"], 6)
        self.assertEqual([r[-1] for r in ran], ["install", "doctor"])
        self.assertIn("devkit is ready.", text)
        self.assertRegex(text, r"Good (morning|afternoon|evening), Ada\.")
        self.assertNotIn("[31m", text)  # the escape sequence in the GitHub name was removed

    def test_a_first_run_with_no_repositories_asks_the_stacks_with_python_and_node_preselected(self):
        rc, cfg, text, ran = self.run_wizard(ENTER + ENTER + ENTER, stacks={})
        self.assertEqual(rc, 0)
        self.assertIn("Stacks", text)  # asked even on the recommended path
        self.assertEqual(cfg["stacks"], ["python", "node"])  # Return keeps the light preselection

    def test_customize_then_change_a_section_from_the_review(self):
        keys = (DOWN + ENTER                      # Customize
                + ENTER + b"\x15team/handoff" + ENTER  # workspace
                + DOWN + SPACE + ENTER + ENTER    # agents: Codex off, Claude Code mode kept
                + b"a" + ENTER                    # stacks: all
                + ENTER + ENTER                   # hosts, clouds
                + b"99" + ENTER + BS + BS + b"4" + ENTER + ENTER + ENTER + b"n"  # docker, with a fixed typo
                + b"n" + b"30" + ENTER + ENTER    # automation
                + DOWN + ENTER + b"6" + ENTER     # review → change a section → Docker
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
        self.assertIn("Step 6 of 8 · Docker", text)

    def test_esc_on_the_first_question_returns_to_the_welcome(self):
        rc, cfg, text, _ = self.run_wizard(DOWN + ENTER + ESC + UP + ENTER + ENTER)  # focus comes back on Customize
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(text.count("How do you want to set up devkit?"), 2)
        self.assertEqual(cfg["stacks"], ["java", "node"])

    def test_quit_and_ctrl_c_change_nothing(self):
        rc, cfg, text, ran = self.run_wizard(ENTER + DOWN + DOWN + ENTER)
        self.assertEqual((rc, cfg, ran), (1, None, []))
        self.assertIn("Nothing changed.", text)
        rc, cfg, text, ran = self.run_wizard(DOWN + ENTER + CTRL_C)
        self.assertEqual((rc, cfg, ran), (130, None, []))
        self.assertIn("Setup cancelled. Nothing changed.", text)

    def test_a_rerun_starts_from_the_saved_config_and_shows_the_changes(self):
        saved = dict(SNAPSHOT_FIRST, hosts=["github"], clouds=[], stacks=["php"])
        rc, cfg, text, _ = self.run_wizard(ENTER + ENTER, saved=saved)
        self.assertIn("devkit is already set up here.", text)
        self.assertIn("Keep current setup", text)
        self.assertIn("1 change from your saved setup", text)
        self.assertIn("was GitHub", text)
        self.assertEqual(cfg["stacks"], ["php"])  # saved stacks win over detection
        self.assertEqual(cfg["hosts"], ["github", "gitlab"])

    def test_a_rerun_without_changes_says_so(self):
        saved = dict(SNAPSHOT_FIRST, hosts=["github", "gitlab"], clouds=[], stacks=["java"])
        rc, cfg, text, _ = self.run_wizard(ENTER + DOWN + DOWN + ENTER, saved=saved)
        self.assertIn("No changes from your saved setup", text)
        self.assertEqual(rc, 1)


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
        self.assertEqual(found, {"counts": {"java": 1, "node": 2, "php": 1, "python": 1}, "repos": 4})

    def test_package_status_marks_what_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, npm = Path(tmp) / "brew", Path(tmp) / "npm"
            for p in (prefix / "Cellar" / "uv", prefix / "opt" / "python", prefix / "Caskroom" / "codex", npm / "pyright"):
                p.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": str(prefix)}), \
                    mock.patch.object(wizard, "_NPM_ROOT", [npm]):
                status = dict(wizard.package_status(["core", "agents", "python"]))
        self.assertIn(("python", "brew", True), status["core"])
        self.assertIn(("git", "brew", False), status["core"])
        self.assertEqual(status["agents"], [("codex", "cask", True)])
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

    def fake_exec(argv, on_line=None, log=None, env=None):
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
        t.expect("Agents to configure")
        t.send(DOWN + SPACE + ENTER)
        t.expect("Claude Code permissions")
        t.send(DOWN + ENTER)
        t.expect("Stacks you work in")
        t.send(b"a" + ENTER)
        t.expect("Where your code and pipelines live")
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
            "sync": {"interval_minutes": 30}})
        ran = [c["argv"][0] if c["argv"][0] in ("brew", "npm") else c["argv"][-1] for c in t.calls()]
        self.assertEqual(ran, ["brew", "npm", "install", "doctor"])
        log = self.home / ".local" / "state" / "devkit" / "logs" / "setup.log"
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        raw = t.out.decode("utf-8", "replace")
        self.assertIn("\x1b[?1049h", raw)
        self.assertLess(raw.rindex("\x1b[?1049l"), raw.index("Applying your plan"))

    def test_ctrl_c_restores_the_terminal_and_changes_nothing(self):
        t = Pty(self.home)
        t.expect("How do you want to set up devkit?")
        t.send(CTRL_C)
        self.assertEqual(t.wait(), 130)
        raw = t.out.decode("utf-8", "replace")
        self.assertIn("Setup cancelled. Nothing changed.", plain(raw))
        self.assertTrue(raw.rindex("\x1b[?1049l") < raw.rindex("Setup cancelled"))
        self.assertIn("\x1b[?25h", raw)
        self.assertIsNone(t.config())
        self.assertEqual(t.calls(), [])

    def test_term_dumb_uses_the_line_prompts(self):
        t = Pty(self.home, term="dumb")
        t.expect("Development root")
        t.send(b"\n" * 24)
        t.expect("Verifying")
        self.assertEqual(t.wait(), 0)
        raw = t.out.decode("utf-8", "replace")
        self.assertNotIn("\x1b[?1049h", raw)
        self.assertIn("1 This machine", raw)
        self.assertEqual(t.config()["stacks"], ["python", "node"])  # the light first-run default


if __name__ == "__main__":
    unittest.main()
