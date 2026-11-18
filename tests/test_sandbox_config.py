# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The shared test sandboxes (TempState in tests/test_progress.py, its subclass in tests/test_crew.py, and the
# caller-identity variant) keep devkit's configuration file in their own temporary folder. A probe or a test that
# saves a setting inside one of them (dev config set work.mode guided, a job's schedule, an extension overlay) must
# never write the configuration the process would otherwise use: outside the suite's isolation that is the
# developer's real ~/.config/devkit/config.json. The writer and the file lock are replaced by a recorder, so the
# check itself never writes anywhere. Tests carry comments, not docstrings: `dev test prove` leaves out tests with a
# docstring. Run: python3 -m unittest tests.test_sandbox_config
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _hermetic  # noqa: E402

_hermetic.activate()

sys.path.insert(0, str(HERE.parent / "lib"))
from devkit import cli, core, setup, wizard  # noqa: E402,F401  (setup and wizard bind CONFIG_FILE at import)
import test_crew  # noqa: E402
import test_progress  # noqa: E402

SANDBOXES = {"test_progress.TempState": lambda: test_progress.TempState(),
             "test_progress.TempState(real_caller=True)": lambda: test_progress.TempState(real_caller=True),
             "test_crew.TempState": lambda: test_crew.TempState()}


def below(path, root) -> bool:
    p, r = os.path.realpath(str(path)), os.path.realpath(str(root))
    return p == r or p.startswith(r + os.sep)


@contextlib.contextmanager
def recorded_config_writes():
    # every target devkit's configuration writer would lock and replace, recorded instead of written
    targets = []

    @contextlib.contextmanager
    def lock(path, wait=30.0):
        targets.append(os.path.realpath(str(path)))
        yield True

    def write(path, candidate):
        targets.append(os.path.realpath(str(path)))

    with mock.patch.object(core, "file_lock", lock), mock.patch.object(core, "_write_config", write):
        yield targets


class SandboxConfigTests(unittest.TestCase):
    def setUp(self):
        self.outside = os.path.realpath(str(core.CONFIG_FILE))  # what a write would hit without the sandbox

    # `dev config set work.mode guided` inside a sandbox saves into the sandbox's folder, never into the
    # configuration the process had before it entered (the developer's own, outside the suite's isolation)
    def test_dev_config_set_inside_a_sandbox_writes_only_into_the_sandbox(self):
        for name, make in SANDBOXES.items():
            with self.subTest(name), make() as state, recorded_config_writes() as targets, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = cli.main(["config", "set", "work.mode", "guided"])
                self.assertEqual(0, rc)
                self.assertTrue(targets, "the configuration writer was never reached")
                self.assertNotIn(self.outside, targets)
                for target in targets:
                    self.assertTrue(below(target, state.tmp.name), f"{name} wrote {target}")

    # every devkit module that bound CONFIG_FILE at import (core, setup, wizard) sees the sandbox's file while the
    # sandbox lasts, so a writer that uses its own binding (setup's first configuration, the wizard's save) stays
    # inside too; and gets its own file back when it ends, so no later test writes into a deleted temporary folder
    def test_every_module_binding_of_the_config_file_lives_in_the_sandbox_while_it_lasts(self):
        def bindings():
            return {m: getattr(mod, "CONFIG_FILE") for m, mod in list(sys.modules.items())
                    if m.startswith("devkit") and isinstance(getattr(mod, "CONFIG_FILE", None), Path)}
        for name, make in SANDBOXES.items():
            with self.subTest(name):
                with make() as state:
                    bound = bindings()
                    self.assertTrue({"devkit.core", "devkit.setup", "devkit.wizard"} <= set(bound), sorted(bound))
                    outside = {m: str(p) for m, p in bound.items() if not below(p, state.tmp.name)}
                    self.assertEqual({}, outside)
                self.assertEqual({m: Path(self.outside) for m in bound},
                                 {m: Path(os.path.realpath(str(p))) for m, p in bindings().items() if m in bound})

    # a `dev` process a test starts inside a sandbox resolves the sandbox's configuration (DEVKIT_CONFIG), not the
    # one of the home it inherits; after the sandbox, the variable is what it was
    def test_a_child_process_resolves_the_sandbox_config(self):
        env = os.environ.get("DEVKIT_CONFIG")
        for name, make in SANDBOXES.items():
            with self.subTest(name):
                with make() as state:
                    self.assertTrue(below(core.config_file(), state.tmp.name), str(core.config_file()))
                self.assertEqual(env, os.environ.get("DEVKIT_CONFIG"))


class SandboxConfigCopyTests(unittest.TestCase):
    # the real writer keeps a copy of the previous file before each change (core._keep_config_copy, in
    # STATE/backups): inside a sandbox that copy lands in the sandbox, so a test's configs never enter the real
    # backups folder, push out its newest-20 copies, or become the copy the restore hint names
    def test_the_real_writer_keeps_its_config_copies_inside_the_sandbox(self):
        outside = os.path.realpath(str(core.STATE / "backups"))
        before = sorted(os.listdir(outside)) if os.path.isdir(outside) else []
        for label, make in SANDBOXES.items():
            with self.subTest(label), make() as box:
                core.config_set("work.mode", "guided")
                core.config_set("work.mode", "auto")
                copies = core.config_copies()
                self.assertTrue(copies, "the second write kept no copy of the first")
                for copy in copies:
                    self.assertTrue(below(copy, box.tmp.name), f"{copy} is outside the sandbox")
        after = sorted(os.listdir(outside)) if os.path.isdir(outside) else []
        self.assertEqual(after, before, "a sandbox's config copy reached the backups folder outside it")


if __name__ == "__main__":
    unittest.main()
