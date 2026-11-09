# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev setup` from the person's side: the agent question, the review, the install steps, the sign-in panels, the
summary and continuing a setup later. Each test names the acceptance criterion it proves (INSTALL-AC-n of the
installer experience spec). The wizard runs in-process on a fake terminal with scripted keys; detection, commands and
sign-ins are stubbed by a fake Mac, and every file goes to a temporary folder, never this Mac's ~/.config or ~/.codex.
"""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, providers, setup, toolchain, tui, wizard  # noqa: E402

UP, DOWN, ENTER, ESC, CTRL_C = b"\x1b[A", b"\x1b[B", b"\r", b"\x1b", b"\x03"
LIB = str(Path(__file__).resolve().parents[1] / "lib")


def plain(s) -> str:
    return tui.strip(s)


def dev_args(argv) -> list:
    argv = [str(a) for a in argv]
    i = next((n for n, a in enumerate(argv) if a.endswith("/bin/dev")), None)
    return argv[i + 1:] if i is not None else []


class FakeTerm(tui.Terminal):
    """A terminal that reads scripted bytes and writes to a string (no termios, no TTY). When the keys run out, the
    next read is a closed input, which the wizard takes as Ctrl-C."""

    def __init__(self, data: bytes = b"", cols=100, rows=60):
        super().__init__(fin=io.StringIO(), fout=io.StringIO(), reader=tui.KeyReader.from_bytes(data))
        self._size = (cols, rows)

    def size(self):
        return self._size

    @property
    def text(self) -> str:
        return plain(self.out.getvalue())


def setUpModule():
    tui.set_color(False)


def tearDownModule():
    tui.set_color()


def rows(n_missing, n_installed=0):
    """package_status() rows: n Homebrew packages to install and n installed, in the core group."""
    return [("core", [(f"tool{i}", "brew", False) for i in range(n_missing)]
             + [(f"have{i}", "brew", True) for i in range(n_installed)])]


class Mac:
    """A fake Mac: what the survey finds, each agent's state ("missing", "installed", "signed-in"), the commands the
    wizard runs and how they end, and the sign-ins the person goes through."""

    def __init__(self, claude="signed-in", codex="signed-in", saved=None, stacks=None, docker="running",
                 packages=None, npm=("pyright",)):
        self.agent = {"claude": claude, "codex": codex}
        self.saved, self.stacks, self.docker = saved, stacks, docker
        self.packages = packages if packages is not None else []
        self.npm = list(npm)
        self.events = []        # ("exec", name) and ("ask", title), in order
        self.ran = []           # every command run with its output captured
        self.brewfiles = []     # the text of each Brewfile passed to brew bundle
        self.outcome = {}       # command name -> exit code, wizard._Exit, an exception, or a function(on_line)
        self.sign_ins = {"claude": [], "codex": []}  # scripted: "ok", "interrupted", "timeout", "ok-but-out",
        self.attached = []      # ("failed", "<its last error line>")
        self.bundle_check = 0   # the exit code of `brew bundle check`

    # what the wizard sees
    def survey(self):
        provs = {k: {"installed": v != "missing", "logged_in": v == "signed-in",
                     "plan": {"claude": "max", "codex": "business"}[k] if v == "signed-in" else None}
                 for k, v in self.agent.items()}
        return {"cpus": 8, "ram": 16, "macos": "26.2", "arch": "arm64", "brew": True,
                "gh": {"login": "ana-dev", "name": "Ana Dev", "id": 1}, "providers": provs, "config": self.saved,
                "seen": {"hosts": ["github"], "clouds": [], "evidence": {}},
                "stacks_found": {"counts": self.stacks if self.stacks is not None else {"python": 2}, "repos": 2},
                "docker": {"colima": self.docker != "missing", "running": self.docker == "running"}}

    def status(self, include_account=True):
        return self.survey()["providers"]

    def have(self, tool):
        if tool in self.agent:
            return self.agent[tool] != "missing"
        if tool == "colima":
            return self.docker != "missing"
        return True

    @staticmethod
    def name(argv) -> str:
        argv = [str(a) for a in argv]
        if dev_args(argv):
            return dev_args(argv)[0]
        if argv[:3] == ["brew", "bundle", "check"]:
            return "bundle-check"
        if argv[:2] == ["brew", "bundle"]:
            return "brew"
        if argv[:3] == ["brew", "install", "--cask"]:
            return "codex-install"
        if argv[:1] == ["npm"]:
            return "npm"
        if argv[:1] == ["/bin/bash"]:
            return "claude-install"
        return argv[0]

    def exec(self, argv, on_line=None, log=None, env=None, timeout=None):
        name = self.name(argv)
        self.ran.append([str(a) for a in argv])
        self.events.append(("exec", name))
        if log:  # as the real runner does: every command goes to the setup log first
            log.write(f"\n$ {' '.join(map(str, argv))}\n")
        bf = next((a.split("=", 1)[1] for a in map(str, argv) if a.startswith("--file=")), None)
        if bf and name == "brew":
            self.brewfiles.append(Path(bf).read_text())
        if name == "bundle-check":
            return wizard._Exit(self.bundle_check)
        out = self.outcome.get(name, 0)
        if isinstance(out, BaseException):
            raise out
        if callable(out):
            out = out(on_line)
        if out == 0:
            if name == "claude-install":
                self.agent["claude"] = "installed"
            elif name == "codex-install":
                self.agent["codex"] = "installed"
            elif name == "docker":
                self.docker = "running"
            elif name == "doctor" and on_line:
                on_line("  ✓ 58/58 checks passed")
        return out if isinstance(out, wizard._Exit) else wizard._Exit(out)

    def attach(self, argv, timeout=None, device=None, clock=None):
        k = "claude" if str(argv[0]) == "claude" else "codex"
        self.attached.append([str(a) for a in argv])
        self.events.append(("sign-in", k))
        got = self.sign_ins[k].pop(0) if self.sign_ins[k] else "ok"
        got, err = got if isinstance(got, tuple) else (got, "")
        if got == "ok":
            self.agent[k] = "signed-in"
        return wizard._Ended("ok" if got == "ok-but-out" else got, err)

    @contextlib.contextmanager
    def patched(self, tmp: Path, device=os.devnull):
        cfgf, state = tmp / "config.json", tmp / "state"
        if self.saved is not None and not cfgf.exists():
            cfgf.write_text(json.dumps(self.saved))
        mac = self
        real_ask, real_block = tui.Screen.ask, tui.ask_block

        def ask(screen, prompt, static=()):
            mac.events.append(("ask", prompt.title))
            mac.asked.append((prompt.title, [getattr(o, "label", o) for o in getattr(prompt, "options", [])],
                              getattr(prompt, "value", None), prompt))
            return real_ask(screen, prompt, static)

        def block(term, prompt, above=()):
            mac.events.append(("ask", prompt.title))
            mac.asked.append((prompt.title, [getattr(o, "label", o) for o in getattr(prompt, "options", [])],
                              getattr(prompt, "value", None), prompt))
            return real_block(term, prompt, above)
        self.asked = []
        with mock.patch.object(wizard, "CONFIG_FILE", cfgf), mock.patch.object(wizard, "STATE", state), \
                mock.patch.object(setup, "STATE", state), mock.patch.object(wizard, "survey", self.survey), \
                mock.patch.object(wizard, "_exec", self.exec), mock.patch.object(wizard, "_attached", self.attach), \
                mock.patch.object(wizard, "package_status", lambda groups: self.packages), \
                mock.patch.object(wizard, "_npm_globals", lambda groups: list(self.npm)), \
                mock.patch.object(wizard, "detect_docker", lambda: {"colima": self.docker != "missing",
                                                                    "running": self.docker == "running"}), \
                mock.patch.object(wizard, "terminal_device", lambda: device), \
                mock.patch.object(core, "have", self.have), mock.patch.object(providers, "status", self.status), \
                mock.patch.object(tui.Screen, "ask", ask), mock.patch.object(tui, "ask_block", block), \
                mock.patch.dict(os.environ, {"DEVKIT_INSTALLER": ""}), \
                mock.patch("subprocess.call", side_effect=AssertionError("every command goes through _exec")):
            yield cfgf, state

    def titles(self) -> list:
        return [t for t, *_ in self.asked]

    def prompt(self, title_part: str, nth: int = 0):
        found = [a for a in self.asked if title_part in a[0]]
        return found[nth] if len(found) > nth else None


class Result:
    def __init__(self, rc, text, cfg, run, raw=""):
        self.rc, self.text, self.cfg, self.run, self.raw = rc, text, cfg, run, raw

    def state(self, sid: str) -> str:
        return next((s["state"] for s in self.run["steps"] if s["id"] == sid), None)

    def step(self, sid: str) -> dict:
        return next((s for s in self.run["steps"] if s["id"] == sid), {})


def run_wizard(mac: Mac, keys: bytes, skip=False, resume=False, tmp=None, cols=100, rows=60, device=os.devnull):
    """Run `dev setup` (or `dev setup --resume`) in the interactive front end -> Result."""
    own = tmp is None
    td = tempfile.TemporaryDirectory() if own else None
    tmp = Path(td.name) if own else tmp
    try:
        term = FakeTerm(keys, cols, rows)
        with mac.patched(tmp, device) as (cfgf, state):
            rc = wizard.interactive(term, skip_packages=skip, resume=resume)
        cfg = json.loads(cfgf.read_text()) if cfgf.exists() else None
        run = core.read_json(state / "setup-run.json", None)
        return Result(rc, term.text, cfg, run, term.out.getvalue())
    finally:
        if own:
            td.cleanup()


# Keys for the recommended path on a first run where stacks were found: Recommended, the agent answer, Apply.
RECOMMENDED = ENTER + ENTER + ENTER
# The same, from the default "Claude Code only" (Claude Code signed in, Codex not) up to both agents.
BOTH_FROM_CLAUDE = ENTER + UP + ENTER + ENTER


# --------------------------------------------------------------------------- your choices


class ChoicesTests(unittest.TestCase):
    def test_ac1_the_recommended_path_asks_which_agents(self):
        """INSTALL-AC-1: Recommended setup on a first run goes to the agent question, with exactly three answers."""
        mac = Mac()
        run_wizard(mac, ENTER)
        title, labels, _, _ = mac.asked[1]
        self.assertEqual(title, "Which coding agents do you want to use?")
        self.assertEqual(labels, ["Claude Code and Codex", "Claude Code only", "Codex only"])

    def test_ac2_the_agent_default_follows_what_is_signed_in(self):
        """INSTALL-AC-2: Claude Code signed in and Codex not installed: "Claude Code only" is highlighted."""
        mac = Mac(claude="signed-in", codex="missing")
        run_wizard(mac, ENTER)
        self.assertEqual(mac.prompt("Which coding agents")[2], "claude")
        mac = Mac(claude="missing", codex="missing")
        run_wizard(mac, ENTER)
        self.assertEqual(mac.prompt("Which coding agents")[2], "both")

    def test_ac5_the_agent_question_shows_what_each_agent_needs(self):
        """INSTALL-AC-5: at 80 columns, what each agent needs is on screen whichever option the pointer is on."""
        mac = Mac()
        run_wizard(mac, ENTER)
        prompt = mac.prompt("Which coding agents")[3]
        for i in range(3):
            prompt.i = i
            text = "\n".join(plain(x) for x in prompt.lines(79))
            self.assertIn("Claude Code  a Claude Pro, Max, Team or Enterprise plan,", text)
            self.assertIn("Codex        a ChatGPT Plus, Pro, Business, Edu or Enterprise plan,", text)

    def test_ac6_esc_goes_back_one_question_and_keeps_the_answer(self):
        """INSTALL-AC-6: "Codex only" answered, Esc on the stacks question shows the agent question on "Codex only"."""
        mac = Mac(stacks={})
        run_wizard(mac, ENTER + DOWN + DOWN + ENTER + ESC)
        self.assertEqual(mac.titles()[1:4], ["Which coding agents do you want to use?", "Stacks you work in",
                                             "Which coding agents do you want to use?"])
        self.assertEqual(mac.prompt("Which coding agents", 1)[2], "codex")

    def test_ac7_a_number_on_the_review_opens_its_section(self):
        """INSTALL-AC-7: 2 on the review opens Agents; after its last question the review shows the new row."""
        mac = Mac()
        r = run_wizard(mac, ENTER + ENTER + b"2" + DOWN + ENTER + ENTER)
        self.assertEqual(mac.titles()[2:6], ["Apply this plan?", "Which coding agents do you want to use?",
                                             "Claude Code permissions", "Apply this plan?"])
        last = r.text.split("Step 3 of 3 · Review")[-1]
        self.assertRegex(last, r"2  Agents +Claude Code \(no prompts\)\r?\n")
        self.assertNotIn("devkit removes its rules", last)  # a first run set nothing up in Codex to remove

    def test_ac8_a_changed_row_shows_what_it_was(self):
        """INSTALL-AC-8: the changed Agents row is yellow, with "was …" under it."""
        mac = Mac()
        tui.set_color(True)
        try:
            r = run_wizard(mac, ENTER + ENTER + b"2" + DOWN + ENTER + ENTER)
        finally:
            tui.set_color(False)
        self.assertIsNotNone(mac.prompt("Apply this plan?", 1))
        first, last = r.raw.split("Apply this plan?")[0], r.raw.split("Apply this plan?")[-2]
        self.assertNotIn("\x1b[33mAgents", first)
        self.assertIn("\x1b[33mAgents", last)
        self.assertIn("was Claude Code (no prompts), Codex (no prompts)", plain(last))

    def test_ac9_quitting_at_the_review_changes_nothing(self):
        """INSTALL-AC-9: Quit without changes: no config file, no command, exit code 1."""
        mac = Mac()
        r = run_wizard(mac, ENTER + ENTER + DOWN + DOWN + ENTER)
        self.assertEqual((r.rc, r.cfg, mac.ran), (1, None, []))
        self.assertIn("Nothing changed. Run dev setup when you are ready.", r.text)

    def test_ac13_docker_start_is_answered_in_the_docker_section(self):
        """INSTALL-AC-13: No to "Start Docker when the setup ends?" in Customize: no Start Docker step, no question."""
        mac = Mac(docker="stopped")
        keys = (DOWN + ENTER                    # Customize
                + ENTER * 2                     # workspace
                + ENTER * 3                     # agents: both, and each one's permissions
                + ENTER                         # stacks
                + ENTER * 2                     # hosts, commit template
                + ENTER                         # clouds
                + ENTER * 4 + b"n"              # docker: CPUs, memory, disk, start at login, start at the end: no
                + ENTER * 4                     # automation
                + ENTER)                        # Apply
        r = run_wizard(mac, keys, skip=True)
        self.assertEqual(r.rc, 0, r.text)
        self.assertIn("Start Docker when the setup ends?", mac.titles())
        self.assertNotIn("docker", [n for e, n in mac.events if e == "exec"])
        after = mac.titles()[mac.titles().index("Apply this plan?") + 1:]
        self.assertFalse([t for t in after if "Docker" in t])
        self.assertNotIn("Start Docker", [s["title"] for s in r.run["steps"]])

    def test_ac27_ctrl_c_in_the_questions_asks_first(self):
        """INSTALL-AC-27: Ctrl+C on the stacks question asks to quit, Keep going first; Enter returns to the question."""
        mac = Mac(stacks={})
        r = run_wizard(mac, ENTER + ENTER + CTRL_C + ENTER + ENTER + ENTER)
        quit_prompt = mac.prompt("Quit the setup?")
        self.assertIsNotNone(quit_prompt)
        self.assertIn("Nothing has changed on this Mac.", r.text)
        self.assertEqual(quit_prompt[2], True)  # Keep going is highlighted
        self.assertEqual(mac.titles()[2:6], ["Stacks you work in", "Quit the setup?", "Stacks you work in",
                                             "Apply this plan?"])
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.cfg["stacks"], ["python", "node"])  # the same answers

    def test_ac27_a_second_ctrl_c_quits(self):
        """INSTALL-AC-27 (W-6): a second Ctrl+C quits, nothing changed, exit code 1."""
        mac = Mac()
        r = run_wizard(mac, ENTER + CTRL_C + CTRL_C)
        self.assertEqual((r.rc, r.cfg, mac.ran), (1, None, []))
        self.assertIn("Nothing changed. Run dev setup when you are ready.", r.text)

    def test_ac47_the_review_estimates_the_install_time(self):
        """INSTALL-AC-47: 36 packages to install: about 15 minutes (36 x 20 s, up to the next 5 minutes)."""
        mac = Mac(packages=rows(36, 4))
        r = run_wizard(mac, ENTER + ENTER)
        self.assertIn("about 15 minutes of installing that needs nothing from you", " ".join(r.text.split()))
        self.assertIn("36 to install · ✓ 4 already installed", r.text)
        mac = Mac(packages=rows(0, 4), npm=())
        r = run_wizard(mac, ENTER + ENTER)
        self.assertIn("about 2 minutes of setting up that needs nothing from you", " ".join(r.text.split()))


# --------------------------------------------------------------------------- the install steps


class InstallStepTests(unittest.TestCase):
    def test_ac3_claude_code_only_installs_no_codex(self):
        """INSTALL-AC-3: "Claude Code only" with Codex missing: no Codex install, no cask "codex", no Codex step."""
        mac = Mac(claude="signed-in", codex="missing", packages=rows(3))
        r = run_wizard(mac, ENTER + ENTER + ENTER)
        self.assertEqual(mac.prompt("Which coding agents")[2], "claude")
        self.assertEqual(r.rc, 0, r.text)
        self.assertTrue(mac.brewfiles)
        self.assertFalse([b for b in mac.brewfiles if 'cask "codex"' in b])
        self.assertNotIn("codex-install", [n for e, n in mac.events if e == "exec"])
        titles = [s["title"] for s in r.run["steps"]]
        self.assertNotIn("Install Codex", titles)
        self.assertNotIn("Sign in to Codex", titles)
        self.assertNotIn("Sign in to Codex", r.text)

    def test_ac3_codex_is_installed_in_its_own_step_when_chosen(self):
        """INSTALL-AC-3 (R2): Codex chosen and missing: its own step installs it; brew bundle still has no Codex."""
        mac = Mac(claude="signed-in", codex="missing", packages=rows(3))
        r = run_wizard(mac, ENTER + UP + ENTER + ENTER + DOWN + DOWN + ENTER)  # both; then skip Codex's sign-in
        self.assertIn(["brew", "install", "--cask", "codex"], mac.ran)
        self.assertFalse([b for b in mac.brewfiles if 'cask "codex"' in b])
        self.assertEqual(r.state("codex"), "done")

    def test_ac4_codex_only_installs_no_claude_code(self):
        """INSTALL-AC-4: "Codex only" with Claude Code missing: its installer does not run, no Claude Code step."""
        mac = Mac(claude="missing", codex="signed-in")
        r = run_wizard(mac, ENTER + ENTER + ENTER)
        self.assertEqual(mac.prompt("Which coding agents")[2], "codex")
        self.assertNotIn("claude-install", [n for e, n in mac.events if e == "exec"])
        self.assertFalse([s for s in r.run["steps"] if "Claude Code" in s["title"]])
        self.assertFalse([t for t in mac.titles() if "Claude Code" in t])

    def test_ac12_the_install_steps_ask_nothing(self):
        """INSTALL-AC-12: Docker to start and both agents to sign in: the first prompt after Apply is a sign-in panel,
        and every install step ran before it."""
        mac = Mac(claude="installed", codex="installed", docker="stopped")
        run_wizard(mac, RECOMMENDED + DOWN * 3 + ENTER + DOWN * 2 + ENTER)  # skip both sign-ins
        events = mac.events[[e for e in mac.events].index(("ask", "Apply this plan?")) + 1:]
        first_ask = next(i for i, e in enumerate(events) if e[0] == "ask")
        self.assertEqual(events[first_ask], ("ask", "Sign in to Claude Code"))
        self.assertIn(("exec", "docker"), events[:first_ask])
        self.assertIn(("exec", "install"), events[:first_ask])

    def test_ac24_ctrl_c_during_an_install_step_offers_three_ways_on(self):
        """INSTALL-AC-24: Ctrl+C in "Install Homebrew packages": brew stops and the menu offers three ways on."""
        mac = Mac(packages=rows(5))
        mac.outcome["brew"] = KeyboardInterrupt()
        run_wizard(mac, RECOMMENDED + ENTER)
        menu = mac.prompt("You stopped")
        self.assertEqual(menu[0], 'You stopped "Install Homebrew packages".')
        self.assertEqual(menu[1], ["Skip it and go on", "Try it again", "Stop the setup here"])

    def test_ac25_skip_goes_on_with_the_rest(self):
        """INSTALL-AC-25: Skip: the step reads skipped by you, Start Docker waits for it, the others run."""
        mac = Mac(packages=rows(5), docker="stopped")
        mac.outcome["brew"] = KeyboardInterrupt()
        r = run_wizard(mac, RECOMMENDED + ENTER)
        self.assertRegex(r.text, r"– Install Homebrew packages +skipped by you")
        self.assertRegex(r.text, r"– Start Docker +waits for Install Homebrew packages")
        names = [n for e, n in mac.events if e == "exec"]
        self.assertIn("npm", names)
        self.assertIn("install", names)
        self.assertNotIn("docker", names)
        self.assertEqual((r.state("brew"), r.step("brew")["reason"]), ("skipped", "interrupted"))

    def test_ac26_a_second_ctrl_c_stops_the_setup(self):
        """INSTALL-AC-26: Ctrl+C at the menu: "Setup paused.", exit 130, the step saved interrupted, the rest pending."""
        mac = Mac(packages=rows(5))
        mac.outcome["brew"] = KeyboardInterrupt()
        r = run_wizard(mac, RECOMMENDED + CTRL_C)
        self.assertEqual(r.rc, 130)
        self.assertIn("Setup paused.", r.text)
        self.assertEqual(r.step("brew")["reason"], "interrupted")
        later = [s for s in r.run["steps"][[s["id"] for s in r.run["steps"]].index("brew") + 1:]]
        self.assertTrue(later)
        self.assertEqual({s["state"] for s in later}, {"pending"})

    def test_ac51_a_required_step_has_no_skip(self):
        """INSTALL-AC-51: Ctrl+C in "Set up devkit": only Try it again and Stop, under the reason it cannot be skipped."""
        mac = Mac()
        mac.outcome["install"] = KeyboardInterrupt()
        r = run_wizard(mac, RECOMMENDED + DOWN + ENTER)
        menu = mac.prompt("You stopped")
        self.assertEqual(menu[1], ["Try it again", "Stop the setup here"])
        self.assertIn("devkit cannot be ready without this step.", r.text)
        self.assertEqual(r.rc, 130)
        self.assertEqual((r.state("configure"), r.step("configure")["reason"]), ("failed", "interrupted"))

    def test_ac29_every_failure_has_a_reason_a_meaning_and_a_fix(self):
        """INSTALL-AC-29: each step of the step table, failing, shows Reason, This means and Fix with its command."""
        table = {"brew": ("Install Homebrew packages", "Some tools are missing", "dev install --only tools"),
                 "npm": ("Install npm packages", "Language servers and tools from npm are missing",
                         "npm install -g pyright"),
                 "claude-install": ("Install Claude Code", "Claude Code is not installed, so it cannot sign in.",
                                    "curl -fsSL https://claude.ai/install.sh | bash"),
                 "codex-install": ("Install Codex", "Codex is not installed, so it cannot sign in.",
                                   "brew install --cask codex"),
                 "install": ("Set up devkit", "Claude Code and Codex do not have devkit's rules and safety guard yet.",
                             "dev setup --resume"),
                 "docker": ("Start Docker", "Docker is not running; projects that use containers wait until it is.",
                            "dev docker up")}
        for name, (title, means, fix) in table.items():
            with self.subTest(step=title):
                mac = Mac(claude="missing", codex="missing", packages=rows(2), docker="stopped")
                mac.outcome[name] = wizard._Exit(1, [f"boom in {name}"])
                r = run_wizard(mac, RECOMMENDED)
                block = "\n".join(r.text.split(f"✗ {title}")[1].splitlines()[:6])
                self.assertIn(f"Reason: boom in {name}", block)
                self.assertIn(f"This means: {means}", block)
                self.assertIn(f"Fix: {fix}", block)

    def test_ac30_a_silent_failure_still_says_something(self):
        """INSTALL-AC-30: exit code 3 and no output: "<command> stopped with exit code 3 and gave no reason." """
        mac = Mac(packages=rows(2))
        mac.outcome["brew"] = 3
        r = run_wizard(mac, RECOMMENDED)
        self.assertIn("Reason: brew bundle stopped with exit code 3 and gave no reason.", r.text)

    def test_ac31_a_missing_agent_is_not_signed_in(self):
        """INSTALL-AC-31: "Install Claude Code" failed: no Claude Code panel; its sign-in waits for the install."""
        mac = Mac(claude="missing", codex="signed-in")
        mac.outcome["claude-install"] = wizard._Exit(1, ["curl: (6) Could not resolve host: claude.ai"])
        r = run_wizard(mac, ENTER + UP + UP + ENTER + ENTER)  # both agents
        self.assertFalse([t for t in mac.titles() if t == "Sign in to Claude Code"])
        self.assertRegex(r.text, r"– Sign in to Claude Code +waits for Install Claude Code")
        self.assertEqual(r.rc, 1)  # a chosen agent's install is required

    def test_ac32_progress_is_saved_after_each_step(self):
        """INSTALL-AC-32: when "Install npm packages" finishes, setup-run.json says done, newer, mode 0600."""
        mac, seen = Mac(packages=rows(2)), {}

        def npm(on_line):
            seen["before"] = core.read_json(mac.state / "setup-run.json", {})["updated"]
            return 0

        def install(on_line):
            f = mac.state / "setup-run.json"
            seen["after"] = core.read_json(f, {})
            seen["mode"] = stat.S_IMODE(f.stat().st_mode)
            return 0
        mac.outcome.update(npm=npm, install=install)
        ticks = iter(range(1_800_000_000, 1_800_100_000))  # a clock that moves at every reading
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(time, "time", lambda: float(next(ticks))):
            mac.state = Path(tmp) / "state"
            run_wizard(mac, RECOMMENDED, tmp=Path(tmp))
        npm_step = next(s for s in seen["after"]["steps"] if s["id"] == "npm")
        self.assertEqual(npm_step["state"], "done")
        self.assertGreater(seen["after"]["updated"], seen["before"])
        self.assertEqual(seen["mode"], 0o600)


class FailureReasonTests(unittest.TestCase):
    """The reason line of a failed step comes from what the command wrote, never its banner (W-9)."""

    BANNER = ('print("       ██                 ██       ✦  ██  ")\nprint("  Every repo in its place.")\n'
              'print("  v26.11.9 · install  ·  © 2026 Example Inc. · Apache License 2.0")\n'
              'print("\\n▸ Pinned copy of devkit (what the hooks and the guard run)")\n')

    def test_ac28_the_reason_comes_from_stderr(self):
        """INSTALL-AC-28: dev install prints its banner, crashes with KeyError: 'version' and exits 70: the reason
        names the error, never the banner, and "Bug report saved:" names the newest crash file."""
        script = ("import os, sys\nsys.path.insert(0, os.environ['WIZ_LIB'])\nfrom devkit import bugs\n" + self.BANNER
                  + 'try:\n    {}["version"]\nexcept KeyError as e:\n'
                    '    sys.exit(bugs.handle_crash(["install", "--as-pinned"], e))\n')
        with tempfile.TemporaryDirectory() as tmp:
            kit, home = Path(tmp) / "devkit", Path(tmp) / "home"
            (kit / "bin").mkdir(parents=True)
            home.mkdir()
            (kit / "bin" / "dev").write_text(script)
            w = wizard._Wizard(FakeTerm())
            w.a = {"commit_template": True, "use_claude": True, "use_codex": True}
            st = wizard._Step("configure", "Set up devkit", True, w._configure, "dev setup --resume", "",
                              "Claude Code and Codex do not have devkit's rules and safety guard yet.")
            with mock.patch.object(wizard, "DEVKIT_HOME", kit), mock.patch.object(wizard, "STATE", Path(tmp)), \
                    mock.patch.dict(os.environ, {"HOME": str(home), "WIZ_LIB": LIB}), \
                    tui.Checklist(w.term, ["Set up devkit"], animate=False) as cl:
                run = wizard.SetupRun.start(w.a)
                run.plan([(st.sid, st.title, True, st.fix)])
                w._attempt(cl, 0, st, run)
            crashes = sorted((home / ".local" / "state" / "devkit" / "crashes").glob("*"))
        text = "\n".join(plain(n) for n in cl.items[0].notes)
        reason = next(n for n in text.splitlines() if "Reason:" in n)
        self.assertIn("KeyError: 'version'", reason)
        self.assertIn("devkit hit a bug while pinning its copy", reason)
        for banner in ("Every repo in its place", "©", "v26.11.9"):
            self.assertNotIn(banner, text)
        self.assertEqual(len(crashes), 1)
        self.assertIn(f"Bug report saved: ~/.local/state/devkit/crashes/{crashes[0].name}", text)
        self.assertEqual(run.state("configure"), "failed")

    def test_the_reason_skips_banners_versions_headers_and_progress(self):
        """W-9 rule 3: the last line of stderr, then stdout, that is not a banner, version, header or progress."""
        rc = wizard._Exit(1, ["v26.11.9 · install", "▸ Skills", "⠹", "████████░░ 80%"])
        self.assertEqual(wizard._reason(rc, ["fatal: not a tree object", "© 2026 Example"], "git"),
                         "fatal: not a tree object")
        self.assertEqual(wizard._reason(wizard._Exit(3), [], "brew bundle"),
                         "brew bundle stopped with exit code 3 and gave no reason.")
        self.assertEqual(wizard._reason(wizard._Exit(1), ["  ✗ Skills: clone failed"], "dev install"),
                         "Skills: clone failed")
        self.assertEqual(wizard._reason(wizard._Exit(1), ["  ✓ running from ~/.local/share/devkit"], "dev install"),
                         "dev install stopped with exit code 1 and gave no reason.")  # a success line is no reason


# --------------------------------------------------------------------------- signing in


class SignInTests(unittest.TestCase):
    def test_ac14_a_signed_in_agent_gets_no_panel(self):
        """INSTALL-AC-14: Claude Code signed in (Max), Codex not: one line for Claude Code, the first panel is Codex."""
        mac = Mac(claude="signed-in", codex="installed")
        r = run_wizard(mac, BOTH_FROM_CLAUDE + DOWN * 2 + ENTER)
        self.assertRegex(r.text, r"✓ Claude Code +signed in · plan Max")
        panels = [t for t in mac.titles() if t.startswith("Sign in to")]
        self.assertEqual(panels[0], "Sign in to Codex")

    def test_ac15_a_sign_in_panel_offers_named_choices(self):
        """INSTALL-AC-15: the Codex panel names its choices; there is no Yes or No."""
        mac = Mac(claude="signed-in", codex="installed")
        run_wizard(mac, BOTH_FROM_CLAUDE + DOWN * 2 + ENTER)
        self.assertEqual(mac.prompt("Sign in to Codex")[1],
                         ["Sign in with the browser", "Sign in with a code", "Skip for now"])
        mac = Mac(claude="installed", codex="signed-in")
        run_wizard(mac, ENTER + UP + UP + ENTER + ENTER + DOWN * 3 + ENTER)
        self.assertEqual(mac.prompt("Sign in to Claude Code")[1],
                         ["Sign in with the browser", "Sign in with your company's single sign-on",
                          "Sign in with an Anthropic Console account", "Skip for now"])

    def test_ac16_ctrl_c_during_a_sign_in_stops_only_that_sign_in(self):
        """INSTALL-AC-16: Ctrl+C in Codex's browser sign-in: the panel comes back with the reason; the setup goes on."""
        mac = Mac(claude="signed-in", codex="installed")
        mac.sign_ins["codex"] = ["interrupted"]
        r = run_wizard(mac, BOTH_FROM_CLAUDE + ENTER + DOWN * 2 + ENTER)
        self.assertIn("Codex is not signed in yet", r.text)
        self.assertIn("Reason: you stopped the sign-in.", r.text)
        self.assertEqual(mac.attached, [["codex", "login"]])
        self.assertEqual([n for e, n in mac.events if e == "exec"][-1], "doctor")
        self.assertNotEqual(r.rc, 130)

    def test_ac17_a_failed_sign_in_offers_the_other_way(self):
        """INSTALL-AC-17: Codex exited with an error: the reason quotes it; Try again, a code, Skip for now."""
        mac = Mac(claude="signed-in", codex="installed")
        mac.sign_ins["codex"] = [("failed", "Error: port 1455 is already in use")]
        r = run_wizard(mac, BOTH_FROM_CLAUDE + ENTER + DOWN * 2 + ENTER)
        self.assertIn("Reason: Codex stopped with an error: Error: port 1455 is already in use", r.text)
        again = mac.prompt("Codex is not signed in yet")
        self.assertEqual(again[1], ["Try again", "Sign in with a code", "Skip for now"])

    def test_ac17_a_code_after_a_failed_browser_sign_in(self):
        """W-14: the second choice runs Codex's sign-in with a code."""
        mac = Mac(claude="signed-in", codex="installed")
        mac.sign_ins["codex"] = [("failed", "Error: port 1455 is already in use"), "ok"]
        r = run_wizard(mac, BOTH_FROM_CLAUDE + ENTER + DOWN + ENTER)
        self.assertEqual(mac.attached, [["codex", "login"], ["codex", "login", "--device-auth"]])
        self.assertRegex(r.text, r"✓ Codex +signed in · plan Business")
        self.assertEqual(r.state("signin-codex"), "done")

    def test_ac18_esc_brings_back_the_previous_agents_panel(self):
        """INSTALL-AC-18: Claude Code skipped, Esc on the Codex panel shows the Claude Code panel again."""
        mac = Mac(claude="installed", codex="installed")
        run_wizard(mac, RECOMMENDED + DOWN * 3 + ENTER + ESC + DOWN * 3 + ENTER + DOWN * 2 + ENTER)
        panels = [t for t in mac.titles() if t.startswith("Sign in to")]
        self.assertEqual(panels[:3], ["Sign in to Claude Code", "Sign in to Codex", "Sign in to Claude Code"])

    def test_ac19_the_summary_offers_a_skipped_sign_in(self):
        """INSTALL-AC-19: Codex skipped: listed under Left for later, codex login under To finish, and in the menu."""
        mac = Mac(claude="signed-in", codex="installed")
        r = run_wizard(mac, BOTH_FROM_CLAUDE + DOWN * 2 + ENTER + ENTER)
        left = r.text.split("Left for later")[1]
        self.assertRegex(left, r"– Sign in to Codex +you chose Skip for now")
        self.assertIn("codex login", left.split("To finish")[1])
        self.assertIn("Sign in to Codex now", mac.prompt("What now?")[1])
        self.assertEqual(r.rc, 0)

    def test_ac20_signing_in_from_the_summary_updates_it(self):
        """INSTALL-AC-20: "Sign in to Codex now" that works: the summary is drawn again, ready, without the item."""
        mac = Mac(claude="signed-in", codex="installed")
        r = run_wizard(mac, BOTH_FROM_CLAUDE + DOWN * 2 + ENTER + DOWN + ENTER + ENTER)
        last = r.text.split("What now?")[-1]
        tail = r.text[r.text.rindex("devkit is ready"):]
        self.assertTrue(tail.startswith("devkit is ready.\n"), tail[:80])
        self.assertNotIn("Sign in to Codex", tail)
        self.assertIsNotNone(last)
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.state("signin-codex"), "done")

    def test_ac21_the_browser_sign_in_reports_its_time_limit(self):
        """INSTALL-AC-21 (panel): a sign-in stopped at its time limit says so on the panel."""
        mac = Mac(claude="signed-in", codex="installed")
        mac.sign_ins["codex"] = ["timeout"]
        r = run_wizard(mac, BOTH_FROM_CLAUDE + ENTER + DOWN * 2 + ENTER)
        self.assertIn("Reason: the sign-in page was not finished within 10 minutes.", r.text)

    def test_ac23_the_result_comes_from_the_agents_status(self):
        """INSTALL-AC-23: codex login exits 0, but its status says not signed in: the panel says so."""
        mac = Mac(claude="signed-in", codex="installed")
        mac.sign_ins["codex"] = ["ok-but-out"]
        r = run_wizard(mac, BOTH_FROM_CLAUDE + ENTER + DOWN * 2 + ENTER)
        self.assertIn("Reason: Codex finished, but it still reports that you are not signed in.", r.text)

    def test_ac48_a_remote_session_recommends_the_code(self):
        """INSTALL-AC-48: SSH_CONNECTION set: "Sign in with a code" first, recommended, with the remote line."""
        mac = Mac(claude="signed-in", codex="installed")
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "10.0.0.2 51234 10.0.0.9 22"}):
            r = run_wizard(mac, BOTH_FROM_CLAUDE + DOWN * 2 + ENTER)
        panel = mac.prompt("Sign in to Codex")
        self.assertEqual(panel[1][0], "Sign in with a code")
        self.assertEqual(panel[3].options[0].badge, "recommended")
        self.assertIn("You are connected to this Mac remotely, so its browser is not in front of you.", r.text)


class AttachedTests(unittest.TestCase):
    """A sign-in runs on the person's terminal, with a time limit."""

    def test_ac21_the_browser_sign_in_has_a_time_limit(self):
        """INSTALL-AC-21: a command that never exits is ended when the (fake) clock passes 10 minutes."""
        ticks = iter([0.0, 1.0, 601.0, 601.0, 601.0, 601.0])
        t0 = time.monotonic()
        got = wizard._attached([sys.executable, "-c", "import time; time.sleep(60)"], timeout=wizard.SIGN_IN_TIMEOUT,
                               device=os.devnull, clock=lambda: next(ticks, 601.0))
        self.assertEqual(got, "timeout")
        self.assertLess(time.monotonic() - t0, 15)

    def test_ac22_sign_in_commands_get_the_terminal(self):
        """INSTALL-AC-22: stdin and stdout are the terminal device itself, stderr is a terminal too, never the
        installer's pipe or /dev/null; and the command's last error line comes back with how it ended."""
        import pty
        master, slave = pty.openpty()
        device = os.ttyname(slave)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "fds.json"
            probe = ("import json, os, sys\n"
                     "json.dump({fd: os.ttyname(fd) if os.isatty(fd) else None for fd in (0, 1, 2)},"
                     " open(sys.argv[1], 'w'))\nsys.stderr.write('Error: port 1455 is already in use\\n')\n"
                     "sys.exit(1)\n")
            try:
                with mock.patch("sys.stdin", io.StringIO()):
                    got = wizard._attached([sys.executable, "-c", probe, str(out)], timeout=30, device=device)
            finally:
                os.close(slave)
                os.close(master)
            fds = json.loads(out.read_text())
        self.assertEqual((fds["0"], fds["1"]), (device, device))
        self.assertIsNotNone(fds["2"])
        self.assertEqual(got, "failed")
        self.assertEqual(got.error, "Error: port 1455 is already in use")


# --------------------------------------------------------------------------- the summary and exit codes


class SummaryTests(unittest.TestCase):
    def test_ac37_the_summary_of_a_ready_setup(self):
        """INSTALL-AC-37: ready: the first line, "Open a new Terminal window" before any command, the log last."""
        mac = Mac()
        r = run_wizard(mac, RECOMMENDED)
        summary = r.text[r.text.rindex("✓ devkit is ready."):]
        lines = [x for x in summary.splitlines() if x.strip()]
        self.assertEqual(lines[0].strip(), "✓ devkit is ready.")
        self.assertLess(summary.index("1. Open a new Terminal window"), summary.index("dev new my-app"))
        last = " ".join(x.strip() for x in lines[-2:])  # a long log path wraps under its label
        self.assertIn("The full log of this setup:", last)
        self.assertTrue(last.endswith("logs/setup.log"), last)
        self.assertIn("all 58 checks pass", summary)
        self.assertEqual(r.rc, 0)

    def test_ac38_failures_come_first(self):
        """INSTALL-AC-38: "Set up devkit" failed: the not-ready headline, Needs attention above Done."""
        mac = Mac()
        mac.outcome["install"] = wizard._Exit(1, ["fatal: not a tree object"])
        r = run_wizard(mac, RECOMMENDED + DOWN + ENTER)  # Finish
        summary = r.text[r.text.rindex("devkit is not ready yet"):]
        self.assertTrue(summary.startswith("devkit is not ready yet. 1 step failed."), summary[:60])
        self.assertLess(summary.index("Needs attention"), summary.index("Done"))
        self.assertEqual(mac.prompt("What now?")[1][:2], ["Try Set up devkit again", "Finish"])
        self.assertEqual(r.rc, 1)

    def test_a_refused_pin_is_not_ready_and_says_why(self):
        """An unpinned install stays visible: `dev install` refuses to pin (its ✗ line, exit 1), and the summary says
        devkit is not ready, with the refusal as the reason and dev setup --resume as the fix. (The installer's own
        summary line about the pinned copy is gone: the wizard's summary is the only one, R25.)"""
        refusal = ("4f2c1ab is not verified: it is not a release, and its tests have not passed here. The hooks and "
                   "the guard run only a verified copy.")

        def install(on_line):
            on_line("▸ Pinned copy of devkit (what the hooks and the guard run)")
            on_line(f"  ✗ {refusal}")
            return wizard._Exit(1)
        mac = Mac()
        mac.outcome["install"] = install
        r = run_wizard(mac, RECOMMENDED + DOWN + ENTER, cols=200)  # Finish
        summary = r.text[r.text.rindex("devkit is not ready yet"):]
        self.assertRegex(summary, r"✗ Set up devkit +4f2c1ab is not verified: it is not a release")
        self.assertIn("dev setup --resume", summary.split("To finish")[1])
        self.assertEqual((r.rc, r.state("configure")), (1, "failed"))

    def test_try_again_from_the_summary_runs_the_failed_step_and_redraws_it(self):
        """W-16: "Try Start Docker again" runs the failed step again (its output still goes to the setup log), and
        the summary drawn after it says devkit is ready."""
        tries = []

        def docker(on_line):
            tries.append(1)
            return wizard._Exit(1, ["Colima did not start: the VM disk is locked"]) if len(tries) == 1 else 0
        mac = Mac(docker="stopped")
        mac.outcome["docker"] = docker
        r = run_wizard(mac, RECOMMENDED + DOWN + ENTER)  # What now? → Try Start Docker again
        self.assertEqual(mac.prompt("What now?")[1], ["Finish", "Try Start Docker again"])
        self.assertEqual(len(tries), 2)
        self.assertNotIn("closed file", r.text)
        self.assertTrue(r.text[r.text.rindex("devkit is ready"):].startswith("devkit is ready.\n"))
        self.assertEqual((r.rc, r.state("docker")), (0, "done"))

    def test_ac45_exit_codes_match_the_outcome(self):
        """INSTALL-AC-45: ready 0, a skipped sign-in 0, a failed Set up devkit 1, quit at the review 1, paused 130."""
        self.assertEqual(run_wizard(Mac(), RECOMMENDED).rc, 0)
        self.assertEqual(run_wizard(Mac(codex="installed"), BOTH_FROM_CLAUDE + DOWN * 2 + ENTER + ENTER).rc, 0)
        failed = Mac()
        failed.outcome["install"] = 1
        self.assertEqual(run_wizard(failed, RECOMMENDED + DOWN + ENTER).rc, 1)
        self.assertEqual(run_wizard(Mac(), ENTER + ENTER + DOWN + DOWN + ENTER).rc, 1)
        paused = Mac(packages=rows(2))
        paused.outcome["brew"] = KeyboardInterrupt()
        self.assertEqual(run_wizard(paused, RECOMMENDED + DOWN * 2 + ENTER).rc, 130)

    def test_ac46_screens_read_without_color_at_60_columns(self):
        """INSTALL-AC-46: NO_COLOR at 60 columns: the agent question, a sign-in panel, a failed step and the summary
        fit in 59 columns, no word is cut but a reason line, and every state has its mark."""
        screens = wizard.preview(width=60, color=False)
        reasons = ("devkit hit a bug",)  # a reason row of the summary may be cut, as a Reason: line may
        for name in ("W-2", "W-3", "W-12", "W-9", "W-16 ready", "W-16 ready with items left", "W-16 not ready"):
            with self.subTest(screen=name):
                text = screens[name]
                for line in text.splitlines():
                    self.assertLessEqual(len(line), 59, line)
                    if line.rstrip().endswith("…") and "©" not in line:  # the banner's copyright line aside
                        self.assertTrue("Reason:" in line or any(r in line for r in reasons), line)
        marks = "".join(screens.values())
        for mark in "✓✗!–○":
            self.assertIn(mark, marks)


# --------------------------------------------------------------------------- continuing a setup later


def saved_run(tmp: Path, states: dict, reasons=None, answers=None, finished=None):
    """Write a setup-run.json with these step states (in this order) and the saved answers of a first run."""
    titles = {"save": "Save your choices", "brew": "Install Homebrew packages", "npm": "Install npm packages",
              "claude": "Install Claude Code", "configure": "Set up devkit", "signin-claude": "Sign in to Claude Code",
              "signin-codex": "Sign in to Codex", "docker": "Start Docker", "doctor": "Check the setup"}
    d = Mac().survey()
    a = dict(answers or wizard.tui_defaults(d))
    now = finished or time.time()
    steps = [{"id": k, "title": titles[k], "required": k in ("save", "claude", "configure"), "state": v,
              "reason": (reasons or {}).get(k, ""), "fix": ""} for k, v in states.items()]
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    (tmp / "state" / "setup-run.json").write_text(json.dumps(
        {"version": 1, "started": now - 60, "updated": now, "answers": a, "skip_packages": False, "steps": steps}))


PAUSED = {"save": "done", "brew": "done", "npm": "pending", "configure": "pending", "doctor": "pending"}


class ResumeTests(unittest.TestCase):
    def test_ac33_every_entry_point_offers_to_continue(self):
        """INSTALL-AC-33: a saved run with steps left: dev setup and dev setup --resume both show the resume screen
        (install.sh runs dev setup, so it does too)."""
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as tmp:
                saved_run(Path(tmp), PAUSED)
                mac = Mac(saved={"stacks": ["python"]})
                r = run_wizard(mac, b"", resume=resume, tmp=Path(tmp))
                self.assertEqual(mac.titles()[0], "Pick up where you left off?")
                self.assertRegex(r.text, r"✓ Install Homebrew packages")
                self.assertRegex(r.text, r"○ Install npm packages")

    def test_ac34_continue_does_not_repeat_finished_steps(self):
        """INSTALL-AC-34: Homebrew packages done and still installed: only brew bundle check runs, then npm."""
        with tempfile.TemporaryDirectory() as tmp:
            saved_run(Path(tmp), PAUSED)
            mac = Mac(saved={"stacks": ["python"]}, packages=rows(0, 3))
            r = run_wizard(mac, ENTER, resume=True, tmp=Path(tmp))
        names = [n for e, n in mac.events if e == "exec"]
        self.assertNotIn("brew", names)
        self.assertIn("bundle-check", names)
        self.assertEqual(names[names.index("bundle-check") + 1], "npm")
        self.assertEqual(r.state("npm"), "done")
        self.assertEqual(names[-1], "doctor")  # the setup is checked again at the end

    def test_continue_uses_the_saved_answers(self):
        """W-17 Continue: the answers saved with the run, not today's defaults (here: no commit template)."""
        with tempfile.TemporaryDirectory() as tmp:
            answers = dict(wizard.tui_defaults(Mac().survey()), commit_template=False)
            saved_run(Path(tmp), PAUSED, answers=answers)
            mac = Mac(saved={"stacks": ["python"]})
            run_wizard(mac, ENTER, resume=True, tmp=Path(tmp))
        install = next(dev_args(c) for c in mac.ran if dev_args(c)[:1] == ["install"])
        self.assertEqual(install, ["install", "--commit-template", "no"])

    def test_ac35_a_finished_step_that_broke_since_runs_again(self):
        """INSTALL-AC-35: "Install Claude Code" done, but claude is gone from the PATH: Continue installs it again."""
        with tempfile.TemporaryDirectory() as tmp:
            saved_run(Path(tmp), {"save": "done", "claude": "done", "configure": "pending", "doctor": "pending"})
            mac = Mac(saved={"stacks": ["python"]}, claude="missing")
            run_wizard(mac, ENTER, resume=True, tmp=Path(tmp))
        self.assertIn("claude-install", [n for e, n in mac.events if e == "exec"])

    def test_ac50_a_step_skipped_with_ctrl_c_runs_on_continue(self):
        """INSTALL-AC-50: "Install npm packages" skipped (interrupted), the run stopped: Continue runs it."""
        with tempfile.TemporaryDirectory() as tmp:
            saved_run(Path(tmp), {"save": "done", "brew": "done", "npm": "skipped", "configure": "pending",
                                  "doctor": "pending"}, reasons={"npm": "interrupted"})
            mac = Mac(saved={"stacks": ["python"]})
            r = run_wizard(mac, ENTER, resume=True, tmp=Path(tmp))
        self.assertIn("npm", [n for e, n in mac.events if e == "exec"])
        self.assertEqual(r.state("npm"), "done")

    def test_ac36_nothing_to_continue(self):
        """INSTALL-AC-36: every step done: "Nothing to continue: the last setup finished on <date>", exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            finished = time.mktime((2026, 11, 9, 11, 5, 0, 0, 0, -1))
            saved_run(Path(tmp), {k: "done" for k in PAUSED}, finished=finished)
            mac = Mac(saved={"stacks": ["python"]})
            r = run_wizard(mac, b"", resume=True, tmp=Path(tmp))
        self.assertIn("Nothing to continue: the last setup finished on November 9 at 11:05.", r.text)
        self.assertIn("To change your setup, run dev setup.", r.text)
        self.assertEqual((r.rc, mac.ran), (0, []))


# --------------------------------------------------------------------------- turning an agent off


DEVKIT_HOOK = {"type": "command", "command": "'/home/x/.local/share/devkit/releases/26.11.9/bin/dev' hook stop"}
OWN_HOOK = {"type": "command", "command": "/usr/local/bin/my-notifier"}
CATALOG_MCP = {"chrome-devtools": {"command": "npx", "args": ["chrome-devtools-mcp"], "env": {"X": "1"}},  # devkit's
               "angular-cli": {"command": "npx", "args": ["-y", "@angular/cli@22.2.6", "mcp"]}}  # catalog, in tests
# the person's own server under a name devkit also uses, set up as Angular's docs show
OWN_ANGULAR = {"command": "npx", "args": ["-y", "@angular/cli", "mcp"]}


class AgentOffTests(unittest.TestCase):
    """INSTALL-AC-39 (R21): `dev install` with an agent turned off removes what devkit wrote for it, and only that."""

    def homes(self, tmp: Path):
        claude, codex, agents, share = tmp / ".claude", tmp / ".codex", tmp / ".agents", tmp / "share" / "skills"
        for d in (claude / "skills", codex / "agents", agents / "skills", share / "tdd", agents / "skills" / "mine"):
            d.mkdir(parents=True)
        mark = setup.GENERATED_MARK + " 26.11.9 -->\n# rules\n"
        (codex / "AGENTS.md").write_text(mark)
        (claude / "CLAUDE.md").write_text(mark)
        (codex / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [DEVKIT_HOOK, OWN_HOOK]}],
                                                               "SessionStart": [{"hooks": [DEVKIT_HOOK]}]}}))
        (codex / "config.toml").write_text(
            'model = "gpt-6"\napproval_policy = "on-request"\n\n[projects."/Users/ana/work"]\ntrust_level = "trusted"\n'
            '\n[mcp_servers.chrome-devtools]\ncommand = "npx"\nargs = ["chrome-devtools-mcp"]\n\n'
            '[mcp_servers.chrome-devtools.env]\nX = "1"\n\n[mcp_servers.mine]\ncommand = "my-mcp"\n\n'
            '[mcp_servers.angular-cli]\ncommand = "npx"\nargs = ["-y", "@angular/cli", "mcp"]\n')
        (tmp / ".claude.json").write_text(json.dumps({"numStartups": 3, "mcpServers": {
            "chrome-devtools": {"type": "stdio", **CATALOG_MCP["chrome-devtools"]},
            "angular-cli": {"type": "stdio", **OWN_ANGULAR, "env": {}}}}))
        (codex / "fast.config.toml").write_text("# Managed by devkit (config/codex/config.json → profiles).\n")
        (codex / "work.config.toml").write_text("model = 'mine'\n")
        (codex / "agents" / "neo.toml").write_text(setup.CODEX_ROLE_MARK + " (config/crew.json).\nname = 'neo'\n")
        (codex / "agents" / "helper.toml").write_text("name = 'helper'\n")
        (agents / "skills" / "tdd").symlink_to(share / "tdd")
        (claude / "skills" / "tdd").symlink_to(share / "tdd")
        (claude / "settings.json").write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [DEVKIT_HOOK]}]}, "theme": "dark",
            "statusLine": {"type": "command", "command": "'/x/bin/dev' hook statusline --agent claude"},
            "enabledPlugins": {"devkit@firefly-devkit": True, "superpowers@claude-plugins-official": True}}))
        return claude, codex, agents, share

    def patched(self, tmp: Path, claude, codex, agents, share, person=True, audits=None):
        """The removal's world in `tmp`: the agents' folders, devkit's state, no agent CLIs, and the caller (`person`:
        the developer in a terminal, or a process an agent started). Audit records go to `audits`, never this Mac's."""
        stack = contextlib.ExitStack()
        for p in (mock.patch.object(setup, "CLAUDE_HOME", claude), mock.patch.object(setup, "CODEX_HOME", codex),
                  mock.patch.object(setup, "AGENTS_HOME", agents), mock.patch.object(setup, "SKILLS_SHARE", share),
                  mock.patch.object(setup, "STATE", tmp / "state"), mock.patch.object(setup, "HOME", tmp),
                  mock.patch.object(core, "older_copy", lambda: None), mock.patch.object(core, "have", lambda t: False),
                  mock.patch.object(core, "set_by_developer", lambda *a, **k: person),
                  mock.patch.object(setup, "_audit", lambda *a, **k: (audits if audits is not None else []).append(a)),
                  mock.patch("devkit.ext.effective_mcp", lambda a: dict(CATALOG_MCP))):
            stack.enter_context(p)
        return stack

    def remove(self, tmp: Path, agent: str, person=True, audits=None):
        claude, codex, agents, share = self.homes(tmp)
        with self.patched(tmp, claude, codex, agents, share, person, audits):
            gone = setup.remove_agent(agent)
        return gone, claude, codex, agents

    def test_an_agent_cannot_take_devkits_hooks_out_by_turning_an_agent_off(self):
        """An agent may run `dev config set agents.claude false` and `dev setup -y` (the guard allows both): the
        removal then leaves the guard's hooks, the rules and the skills in place and says the developer runs it in a
        terminal. The developer's own run (the allowed twin) removes them."""
        for person in (False, True):
            with self.subTest(person=person), tempfile.TemporaryDirectory() as tmp:
                audits = []
                tmp = Path(tmp)
                claude, codex, agents, share = self.homes(tmp)
                before = (claude / "settings.json").read_text()
                with self.patched(tmp, claude, codex, agents, share, person, audits):
                    if person:
                        setup.remove_agent("claude")
                    else:
                        with self.assertRaises(setup.RemovalLeft) as cm:
                            setup.remove_agent("claude")
                        self.assertIn("dev setup", str(cm.exception))
                        self.assertIn("terminal", str(cm.exception))
                after = (claude / "settings.json").read_text()
                self.assertEqual(after == before, not person)
                self.assertEqual((claude / "CLAUDE.md").exists(), not person)
                self.assertEqual((claude / "skills" / "tdd").is_symlink(), not person)
                self.assertEqual([a[0] for a in audits], ["agent.remove"])
                self.assertEqual(audits[0][3] if len(audits[0]) > 3 else "ok", "ok" if person else "refused")

    def test_an_agents_run_of_dev_install_leaves_devkit_in_a_turned_off_agent_and_says_why(self):
        """run_install with Claude Code off, run by an agent: a note says the hooks stay and who removes them; the
        install goes on."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            claude, codex, agents, share = self.homes(tmp)
            with self.patched(tmp, claude, codex, agents, share, person=False), \
                    mock.patch.dict(setup.CFG, {"agents": {"claude": False, "codex": True}}), \
                    mock.patch.object(setup, "_choose_copy", lambda *a, **k: setup.DEVKIT_HOME), \
                    mock.patch.object(core, "is_current_pin", lambda: False), \
                    mock.patch("sys.stdout", io.StringIO()) as out:
                self.assertEqual(setup.run_install(only={"claude"}, as_pinned=True), 0)
            self.assertIn("hook", json.loads((claude / "settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]
                          ["command"])
        self.assertIn("Run dev setup in your terminal", out.getvalue())
        self.assertNotIn("removed devkit's", out.getvalue())

    def test_ac39_turning_codex_off_removes_devkit_from_codex_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            gone, claude, codex, agents = self.remove(Path(tmp), "codex")
            self.assertTrue(gone)
            self.assertFalse((codex / "AGENTS.md").exists())
            hooks = json.loads((codex / "hooks.json").read_text())["hooks"]
            self.assertEqual(hooks, {"Stop": [{"hooks": [OWN_HOOK]}]})
            toml = (codex / "config.toml").read_text()
            self.assertNotIn("chrome-devtools", toml)
            for kept in ('model = "gpt-6"', 'approval_policy = "on-request"', '[projects."/Users/ana/work"]',
                         "[mcp_servers.mine]", 'command = "my-mcp"'):
                self.assertIn(kept, toml)
            self.assertFalse((agents / "skills" / "tdd").exists())
            self.assertTrue((agents / "skills" / "mine").is_dir())
            self.assertFalse((codex / "fast.config.toml").exists())
            self.assertTrue((codex / "work.config.toml").exists())
            self.assertFalse((codex / "agents" / "neo.toml").exists())
            self.assertTrue((codex / "agents" / "helper.toml").exists())
            # Claude Code is untouched
            self.assertTrue((claude / "CLAUDE.md").exists())
            self.assertTrue((claude / "skills" / "tdd").is_symlink())

    def test_ac39_turning_claude_code_off_removes_devkit_from_claude_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            gone, claude, codex, agents = self.remove(Path(tmp), "claude")
            settings = json.loads((claude / "settings.json").read_text())
            self.assertEqual(settings, {"theme": "dark",
                                        "enabledPlugins": {"superpowers@claude-plugins-official": True}})
            self.assertFalse((claude / "CLAUDE.md").exists())
            self.assertFalse((claude / "skills" / "tdd").exists())
            self.assertTrue((codex / "AGENTS.md").exists())

    def test_turning_codex_off_keeps_the_persons_own_mcp_server_under_a_name_devkit_uses(self):
        """An MCP server is devkit's only when it is the one devkit's catalog writes (same command, arguments, URL and
        environment): the person's angular-cli, set up as Angular's docs show, stays even on a first Claude-Code-only
        install where devkit never wrote to Codex, and the message names only the servers that went."""
        with tempfile.TemporaryDirectory() as tmp:
            gone, claude, codex, agents = self.remove(Path(tmp), "codex")
            toml = (codex / "config.toml").read_text()
        self.assertNotIn("chrome-devtools", toml)
        self.assertIn('[mcp_servers.angular-cli]\ncommand = "npx"\nargs = ["-y", "@angular/cli", "mcp"]', toml)
        self.assertEqual([g for g in gone if g.startswith("MCP")], ["MCP servers in config.toml (chrome-devtools)"])

    def test_turning_claude_code_off_removes_only_devkits_mcp_servers_after_a_backup(self):
        """Claude Code keeps its MCP servers in ~/.claude.json: devkit backs it up, then asks `claude mcp remove`
        only for the server that is devkit's catalog entry; the person's angular-cli is never named."""
        calls = []

        def sh(argv, **k):
            calls.append(list(argv))
            return 0, "", ""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            claude, codex, agents, share = self.homes(tmp)
            with self.patched(tmp, claude, codex, agents, share), mock.patch.object(setup, "sh", sh), \
                    mock.patch.object(core, "have", lambda t: t == "claude"):
                gone = setup.remove_agent("claude")
            saved = [p.read_text() for p in (tmp / "state" / "backups").glob(".claude.json.*")]
            original = (tmp / ".claude.json").read_text()
        removes = [c for c in calls if c[:3] == ["claude", "mcp", "remove"]]
        self.assertEqual(removes, [["claude", "mcp", "remove", "-s", "user", "chrome-devtools"]])
        self.assertEqual(saved, [original])
        self.assertIn("MCP servers (chrome-devtools)", gone)

    def test_removing_twice_changes_nothing_more(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude, codex, agents, share = self.homes(Path(tmp))
            with self.patched(Path(tmp), claude, codex, agents, share):
                self.assertTrue(setup.remove_agent("codex"))
                self.assertEqual(setup.remove_agent("codex"), [])

    def test_dev_install_removes_devkit_from_a_turned_off_agent_and_writes_it_no_rules(self):
        """R21: `dev install` with Codex off runs the removal instead of skipping Codex, and writes only CLAUDE.md."""
        removed = []
        with tempfile.TemporaryDirectory() as tmp:
            claude, codex = Path(tmp) / ".claude", Path(tmp) / ".codex"
            with mock.patch.dict(setup.CFG, {"agents": {"claude": True, "codex": False}}), \
                    mock.patch.object(setup, "CLAUDE_HOME", claude), mock.patch.object(setup, "CODEX_HOME", codex), \
                    mock.patch.object(setup, "STATE", Path(tmp) / "state"), \
                    mock.patch.object(core, "older_copy", lambda: None), \
                    mock.patch.object(setup, "_choose_copy", lambda *a, **k: setup.DEVKIT_HOME), \
                    mock.patch.object(core, "is_current_pin", lambda: False), \
                    mock.patch.object(setup, "remove_agent", lambda a: removed.append(a) or ["hooks"]), \
                    mock.patch.object(core, "set_by_developer", lambda *a, **k: True), \
                    mock.patch.object(setup, "setup_codex", side_effect=AssertionError("Codex is off")), \
                    mock.patch("sys.stdout", io.StringIO()) as out:
                self.assertEqual(setup.run_install(only={"instructions", "codex"}, as_pinned=True), 0)
                self.assertTrue((claude / "CLAUDE.md").exists())
                self.assertFalse((codex / "AGENTS.md").exists())
        self.assertEqual(removed, ["codex"])
        self.assertIn("removed devkit's hooks; Codex and your own settings stay", out.getvalue())

    def test_the_rules_step_takes_devkits_rules_out_of_a_turned_off_agent_and_keeps_hand_written_ones(self):
        """R21 on the repair path too: writing the rules with Codex off removes the AGENTS.md devkit generated; a
        hand-written one (no devkit mark) stays as the person wrote it. Only the developer's own run takes them out: the
        repair a hook runs for an agent leaves devkit's rules where they are."""
        for generated, person in ((True, True), (False, True), (True, False)):
            with self.subTest(generated=generated, person=person), tempfile.TemporaryDirectory() as tmp:
                claude, codex = Path(tmp) / ".claude", Path(tmp) / ".codex"
                codex.mkdir()
                text = (setup.GENERATED_MARK + " 26.11.8 -->\n# old rules\n") if generated else "# my own rules\n"
                (codex / "AGENTS.md").write_text(text)
                with mock.patch.dict(setup.CFG, {"agents": {"claude": True, "codex": False}}), \
                        mock.patch.object(setup, "CLAUDE_HOME", claude), mock.patch.object(setup, "CODEX_HOME", codex), \
                        mock.patch.object(setup, "STATE", Path(tmp) / "state"), \
                        mock.patch.object(core, "set_by_developer", lambda *a, **k: person), \
                        mock.patch.object(core, "older_copy", lambda: None), mock.patch("sys.stdout", io.StringIO()):
                    setup.write_agent_instructions(quiet=True)
                self.assertIn(setup.GENERATED_MARK, (claude / "CLAUDE.md").read_text()[:200])
                self.assertEqual((codex / "AGENTS.md").exists(), not (generated and person))
                if (codex / "AGENTS.md").exists():
                    self.assertEqual((codex / "AGENTS.md").read_text(), text)

    def test_a_turned_off_agents_rules_are_no_drift(self):
        """R21 on the repair path: with Codex off, a missing or hand-written AGENTS.md is not drift for the
        ConfigChange repair, while Claude Code's edited CLAUDE.md still is."""
        from devkit import policy
        with tempfile.TemporaryDirectory() as tmp:
            claude, codex = Path(tmp) / ".claude", Path(tmp) / ".codex"
            claude.mkdir()
            (claude / "CLAUDE.md").write_text("# edited by hand\n")
            with mock.patch.dict(core.CFG, {"agents": {"claude": True, "codex": False}}), \
                    mock.patch.object(policy, "CLAUDE_HOME", claude), mock.patch.object(policy, "CODEX_HOME", codex):
                drifts = [d.key for d in policy.diff_instructions()]
        self.assertEqual(drifts, [str(core.tilde(claude / "CLAUDE.md"))])

    def test_ac39_the_review_says_what_turning_codex_off_removes(self):
        """INSTALL-AC-39: on a rerun, Agents changed to Claude Code only: the review says so before Apply."""
        saved = {"agents": {"claude": True, "codex": True}, "stacks": ["python"]}
        mac = Mac(saved=saved)
        r = run_wizard(mac, ENTER + b"2" + DOWN + ENTER + ENTER)
        last = " ".join(r.text.split("· Review")[-1].split())
        self.assertIn("devkit removes its rules, hooks and skills from Codex. Codex and your own settings stay.",
                      last)
        self.assertIn("To remove the app: brew uninstall --cask codex", last)

    def test_the_tools_step_installs_codex_only_when_it_is_on(self):
        """R2 in `dev install --only tools`: the Codex cask is in the packages only when Codex is on."""
        for on in (True, False):
            seen = {}
            with self.subTest(codex=on), \
                    mock.patch.dict(setup.CFG, {"agents": {"claude": True, "codex": on}, "stacks": []}), \
                    mock.patch.object(core, "have", lambda t: True), \
                    mock.patch.object(wizard, "missing_packages", lambda groups: seen.update(groups=set(groups)) or
                                      ["x"]), \
                    mock.patch.object(wizard, "install_packages", lambda *a, **k: seen.update(k) or 0), \
                    mock.patch("devkit.toolchain.postinstall", lambda keys: []), \
                    mock.patch("devkit.toolchain.groups", lambda *a: []), mock.patch("sys.stdout", io.StringIO()):
                setup.setup_tools()
            self.assertEqual("codex" in seen["groups"], on)
            self.assertEqual(seen["with_codex"], on)


# --------------------------------------------------------------------------- without a terminal, and --yes


class LineFrontEndTests(unittest.TestCase):
    def run_lines(self, mac, yes=True, tty=False, typed=()):
        answers = iter(typed)
        asked = []

        def answer(prompt=""):
            asked.append(prompt)
            print(prompt)
            return next(answers, "")

        class Stdin(io.StringIO):
            def isatty(self):
                return tty
        with tempfile.TemporaryDirectory() as tmp, mac.patched(Path(tmp), os.devnull if tty else None) as (cfgf, st), \
                mock.patch("builtins.input", answer), mock.patch("sys.stdin", Stdin()), \
                mock.patch("sys.stdout", io.StringIO()) as out, \
                mock.patch.object(core, "gh_user", lambda: {"login": "ana-dev", "name": "Ana Dev", "id": 1}), \
                mock.patch.object(wizard, "detect", mac.survey), \
                mock.patch.object(toolchain, "detect", lambda: {"hosts": ["github"], "clouds": [], "evidence": {}}):
            rc = wizard.run(assume_yes=yes, skip_packages=True)
        return rc, plain(out.getvalue()), asked

    def test_ac43_no_terminal_skips_the_sign_ins_and_lists_them(self):
        """INSTALL-AC-43: no terminal, both agents installed and not signed in: no sign-in starts, the commands are
        listed, exit code 0."""
        mac = Mac(claude="installed", codex="installed")
        rc, out, _ = self.run_lines(mac, yes=False, tty=False)
        self.assertEqual(mac.attached, [])
        self.assertRegex(out, r"– Sign in to Claude Code +no terminal to sign in from")
        self.assertIn("claude auth login", out.split("To finish")[1])
        self.assertIn("codex login", out.split("To finish")[1])
        self.assertEqual(rc, 0)

    def test_ac44_yes_still_shows_the_sign_in_panels(self):
        """INSTALL-AC-44: --yes in a terminal: the Codex sign-in is asked and waits for a choice."""
        mac = Mac(claude="installed", codex="installed")
        rc, out, asked = self.run_lines(mac, yes=True, tty=True, typed=["4", "3"])
        self.assertIn("Sign in to Codex: 1 browser, 2 code, 3 later [1]", " ".join(asked))
        self.assertEqual(mac.attached, [])


# --------------------------------------------------------------------------- the screens, from --preview


GOLDEN_W3_80 = """\
  ✦ devkit  setup                                       esc back · ctrl-c quits

  Step 1 of 3 · Agents
  ━━━━━━━━━━━━━━━━━━━─────────────────────────────────────

  Which coding agents do you want to use?
  devkit gives each one the same rules, safety guard and skills.

  ❯ Claude Code and Codex                                           recommended
      Use either one in any project. You sign in to each at the end.
    Claude Code only
    Codex only

  What each one needs
    Claude Code  a Claude Pro, Max, Team or Enterprise plan,
                 or an Anthropic Console account
    Codex        a ChatGPT Plus, Pro, Business, Edu or Enterprise plan,
                 or an OpenAI API key

  You can change this later with dev setup.
  ↑↓ move · enter choose · esc back"""

GOLDEN_W3_60 = """\
  ✦ devkit  setup                   esc back · ctrl-c quits

  Step 1 of 3 · Agents
  ━━━━━━━━━━━━━━━━━━━─────────────────────────────────────

  Which coding agents do you want to use?
  devkit gives each one the same rules, safety guard and
  skills.

  ❯ Claude Code and Codex                       recommended
      Use either one in any project. You sign in to each at
      the end.
    Claude Code only
    Codex only

  What each one needs
    Claude Code  a Claude Pro, Max, Team or Enterprise
                 plan, or an Anthropic Console account
    Codex        a ChatGPT Plus, Pro, Business, Edu or
                 Enterprise plan, or an OpenAI API key

  You can change this later with dev setup.
  ↑↓ move · enter choose · esc back"""

GOLDEN_W12_80 = """\
  Sign in to Codex                                                       2 of 2
  Codex needs your ChatGPT account (a Plus, Pro, Business, Edu or Enterprise
  plan) or an OpenAI API key.

  ❯ Sign in with the browser                                        recommended
      Your browser opens a ChatGPT page. Sign in there, then come back here.
    Sign in with a code
    Skip for now

  ↑↓ move · enter choose · esc back to Claude Code"""

GOLDEN_W12_60 = """\
  Sign in to Codex                                   2 of 2
  Codex needs your ChatGPT account (a Plus, Pro,
  Business, Edu or Enterprise plan) or an OpenAI API key.

  ❯ Sign in with the browser                    recommended
      Your browser opens a ChatGPT page. Sign in there,
      then come back here.
    Sign in with a code
    Skip for now

  ↑↓ move · enter choose · esc back to Claude Code"""

GOLDEN_W16_LEFT_80 = """\
  ✓ devkit is ready. 2 things are left for later.

  Done
  ✓ Saved your choices             ~/.config/devkit/config.json
  ✓ Installed 42 packages          Homebrew and npm
  ✓ Installed Claude Code and Codex
  ✓ Set up devkit                  rules, guard, hooks and skills
  ✓ Signed in to Claude Code       plan Max
  ✓ Checked the setup              56 of 58 checks pass

  Left for later
  – Sign in to Codex               you chose Skip for now
  ✗ Start Docker                   Colima did not start: the VM disk is locked

  To finish, open a new Terminal window, then run:
    codex login                    signs you in to Codex
    dev docker up                  starts Docker

  The full log of this setup: ~/.local/state/devkit/logs/setup.log"""

GOLDEN_W16_LEFT_60 = """\
  ✓ devkit is ready. 2 things are left for later.

  Done
  ✓ Saved your choices
      ~/.config/devkit/config.json
  ✓ Installed 42 packages
      Homebrew and npm
  ✓ Installed Claude Code and Codex
  ✓ Set up devkit
      rules, guard, hooks and skills
  ✓ Signed in to Claude Code
      plan Max
  ✓ Checked the setup
      56 of 58 checks pass

  Left for later
  – Sign in to Codex
      you chose Skip for now
  ✗ Start Docker
      Colima did not start: the VM disk is locked

  To finish, open a new Terminal window, then run:
    codex login        signs you in to Codex
    dev docker up      starts Docker

  The full log of this setup:
  ~/.local/state/devkit/logs/setup.log"""


class PreviewTests(unittest.TestCase):
    def test_preview_draws_every_screen_and_changes_nothing(self):
        """`dev setup --preview` draws each screen with sample data and writes no file and runs nothing."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(wizard, "CONFIG_FILE", Path(tmp) / "config.json"), \
                mock.patch.object(wizard, "STATE", Path(tmp) / "state"), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("preview runs nothing")), \
                mock.patch.object(wizard, "_exec", side_effect=AssertionError("preview runs nothing")):
            screens = wizard.preview(width=80, color=False)
            self.assertEqual(list(Path(tmp).iterdir()), [])
        for name in ("W-2", "W-3", "W-6", "W-7", "W-8", "W-9", "W-10", "W-11", "W-12", "W-13", "W-14", "W-15",
                     "W-16 ready", "W-16 ready with items left", "W-16 not ready", "W-16 paused", "W-17", "W-18"):
            self.assertIn(name, screens)
            self.assertTrue(screens[name].strip(), name)

    def test_dev_setup_preview_prints_the_screens_and_starts_no_setup(self):
        from devkit import cli
        with mock.patch.object(wizard, "run", side_effect=AssertionError("the preview starts no setup")), \
                mock.patch.dict(os.environ, {"COLUMNS": "60"}), mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(cli.main(["setup", "--preview"]), 0)  # as wide as the terminal says (COLUMNS here)
        text = plain(out.getvalue())
        self.assertIn("── W-3 ", text)
        self.assertIn("Which coding agents do you want to use?", text)
        self.assertIn("devkit is ready. 2 things are left for later.", text)
        self.assertEqual([x for x in text.splitlines() if len(x) > 59], [])

    def test_the_review_cuts_no_row_and_names_the_lock_only_when_it_is_on(self):
        """W-7: rows fit at 80 columns; a long value wraps under its column instead of being cut."""
        for width in (80, 60):
            text = wizard.preview(width=width, color=False)["W-7"]
            self.assertFalse([x for x in text.splitlines() if x.rstrip().endswith("…")], text)
        self.assertIn("7  Automation           sync every 60 min · disk cleanup on · AI off\n",
                      wizard.preview(width=80, color=False)["W-7"])

    def test_golden_agent_question(self):
        """W-3 at 80 and 60 columns."""
        self.assertEqual(wizard.preview(width=80, color=False)["W-3"], GOLDEN_W3_80)
        self.assertEqual(wizard.preview(width=60, color=False)["W-3"], GOLDEN_W3_60)

    def test_golden_codex_sign_in_panel(self):
        """W-12 at 80 and 60 columns."""
        self.assertEqual(wizard.preview(width=80, color=False)["W-12"], GOLDEN_W12_80)
        self.assertEqual(wizard.preview(width=60, color=False)["W-12"], GOLDEN_W12_60)

    def test_golden_summary_with_items_left(self):
        """W-16, ready with items left, at 80 and 60 columns (the menu under it is the W-16 menu screen)."""
        self.assertEqual(wizard.preview(width=80, color=False)["W-16 ready with items left"], GOLDEN_W16_LEFT_80)
        self.assertEqual(wizard.preview(width=60, color=False)["W-16 ready with items left"], GOLDEN_W16_LEFT_60)


if __name__ == "__main__":
    unittest.main()
