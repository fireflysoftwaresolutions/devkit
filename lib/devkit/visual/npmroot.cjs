// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// npm's global folder (`npm root -g`), where the pinned @playwright/cli and axe-core live (config/npm-globals.txt).
// The capture driver (capture.cjs, whose playwrightCore the crawl uses too) and axe (axe.cjs) read it from here, so
// one process asks npm once: a good answer (exit 0 and one absolute folder) is kept for the process. On a busy Mac
// npm can fail, time out, or print nothing; such an answer is asked again once, and when it is still no folder the
// caller hears what npm did, so it never reads an empty answer as a relative path and reports a missing install.
// npmroot.py keeps the same rule for devkit's Python (the comparison, the marks, `dev visual`, the packages step and
// `dev doctor`): change both together.
//
// Interface:
//
//   globalRoot() -> {root}   the absolute folder npm named
//                 | {why}    what npm did on its last try: "it printed nothing", "it failed with exit 1", …
'use strict';

const path = require('path');
const { spawnSync } = require('child_process');

const TRIES = 2;
const TIMEOUT_MS = 60000; // one try

let kept = null; // the good answer, for the rest of the process

function ask() {
  const npm = spawnSync('npm', ['root', '-g'], { encoding: 'utf8', timeout: TIMEOUT_MS });
  if (npm.error && npm.error.code === 'ETIMEDOUT') return { why: `it did not answer in ${TIMEOUT_MS / 1000} s` };
  if (npm.error) return { why: `it could not start (${npm.error.code || 'error'})` };
  if (npm.signal) return { why: `it was stopped by ${npm.signal}` };
  if (npm.status !== 0) return { why: `it failed with exit ${npm.status}` };
  const out = String(npm.stdout || '').trim();
  if (!out) return { why: 'it printed nothing' };
  if (out.includes('\n') || !path.isAbsolute(out)) return { why: 'it printed no absolute folder' };
  return { root: out };
}

function globalRoot() {
  if (kept) return { root: kept };
  let answer = null;
  for (let i = 0; i < TRIES && !(answer && answer.root); i += 1) answer = ask();
  if (answer.root) kept = answer.root;
  return answer;
}

module.exports = { globalRoot };
