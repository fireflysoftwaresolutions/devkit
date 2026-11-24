# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Which runner takes a JS/TS spec (testx.detect, owner, build_command): Playwright's browser specs and vitest's unit
tests side by side. Hermetic: tiny git repositories in temporary folders (test_testx.Env).
Run: python3 -m unittest tests.test_testx_routing -v"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import testx  # noqa: E402
from test_testx import Env  # noqa: E402

LICENSE = "/*\nCopyright 2026 Example.\nSPDX-License-Identifier: Apache-2.0\n*/\n"

# firefly-weave's Studio: an Angular app with vitest unit tests in tests/ and Playwright browser specs in
# tests/browser/ (the config's testDir), a second Playwright config for acceptance runs, and both tools in
# package.json's scripts
STUDIO = {
    "studio/package.json": json.dumps({"scripts": {"test": "vitest run", "test:browser": "playwright test"},
                                       "devDependencies": {"@playwright/test": "1.55.1", "vitest": "4.1.12"}}),
    "studio/angular.json": json.dumps({"projects": {"studio": {"root": "", "sourceRoot": "src"}}}),
    "studio/vitest.config.ts": "import { defineConfig } from 'vitest/config';\n"
                               "export default defineConfig({ test: { include: ['tests/**/*.test.ts'] } });\n",
    "studio/playwright.config.ts": LICENSE + "import { defineConfig } from \"@playwright/test\";\n\n"
                                   "const port = 4200;\n\nexport default defineConfig({\n"
                                   "  testDir: \"tests/browser\",\n  fullyParallel: false,\n"
                                   "  use: { baseURL: `http://127.0.0.1:${port}` },\n});\n",
    "studio/playwright.acceptance.config.ts": "import config from './playwright.config';\nexport default config;\n",
    "studio/tests/browser/studio.spec.ts": "import { expect, test } from \"@playwright/test\";\n\n"
                                           "test(\"opens\", async ({ page }) => {\n  await page.goto('/');\n});\n",
    "studio/tests/loop-frame.test.ts": "import { expect, it } from 'vitest';\n\nit('adds', () => {\n"
                                       "  expect(1 + 1).toBe(2);\n});\n",
    "studio/src/app.ts": "export const app = 1;\n",
}


class StudioLayoutTests(unittest.TestCase):
    # a browser spec under the config's testDir runs with Playwright in dev test run and prove (both build their
    # command from the spec's owner); a unit test beside that folder stays vitest's
    def test_studio_browser_specs_run_with_playwright(self):
        with Env() as e:
            top = str(e.repo(STUDIO, branch=None))
            runners = testx.detect(top)
            spec, unit = "studio/tests/browser/studio.spec.ts", "studio/tests/loop-frame.test.ts"
            self.assertEqual(("playwright", "vitest"), (testx.owner(runners, spec)["name"],
                                                        testx.owner(runners, unit)["name"]))
            cmd = testx.build_command(top, testx.owner(runners, spec), files=[spec], report="R")
            self.assertEqual(["npx", "--no-install", "playwright", "test", "--reporter=list,junit",
                              "tests/browser/studio.spec.ts"], cmd["argv"])
            self.assertTrue(cmd["cwd"].endswith("/studio"), cmd["cwd"])


# a Playwright config without a testDir: Playwright runs the specs of its folder that use it, vitest the others
NO_TEST_DIR = {
    "package.json": json.dumps({"devDependencies": {"vitest": "^5", "@playwright/test": "1"}}),
    "playwright.config.ts": "export default { use: { headless: true } };\n",
    "tests/page-types.ts": "import type { Page } from '@playwright/test';\n"
                           "export const title = (page: Page) => page.title();\n",
    "tests/named-types.ts": "import { type Locator, type Page } from '@playwright/test';\n"
                            "export type Both = [Locator, Page];\n",
    "tests/reexports.ts": "export type { Page } from '@playwright/test';\n",
    "tests/title.spec.ts": "import { it } from 'vitest';\nimport { title } from './page-types';\n",
    "tests/named.spec.ts": "import type { Page } from '@playwright/test';\nimport { it } from 'vitest';\n"
                           "import type { Both } from './named-types';\nimport './reexports';\n",
    "tests/real.spec.ts": "import { test, type Page } from '@playwright/test';\n",
}


class TypeOnlyImportTests(unittest.TestCase):
    # a type from @playwright/test, imported directly or by a helper (import type, { type X }, export type … from),
    # never makes a vitest test Playwright's; a value import does
    def test_a_type_only_import_of_playwright_leaves_a_unit_test_with_vitest(self):
        with Env() as e:
            top = str(e.repo(NO_TEST_DIR, branch=None))
            runners = testx.detect(top)
            got = {f: testx.owner(runners, f)["name"] for f in ("tests/title.spec.ts", "tests/named.spec.ts",
                                                               "tests/real.spec.ts")}
            self.assertEqual({"tests/title.spec.ts": "vitest", "tests/named.spec.ts": "vitest",
                              "tests/real.spec.ts": "playwright"}, got)


if __name__ == "__main__":
    unittest.main()
