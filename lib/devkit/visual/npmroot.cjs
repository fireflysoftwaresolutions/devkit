// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// npm's global folder (`npm root -g`), where the pinned @playwright/cli and axe-core live (config/npm-globals.txt).
// The capture driver (capture.cjs, whose playwrightCore the crawl uses too) and axe (axe.cjs) read it from here, so
// one process asks npm once: a good answer (exit 0 and one absolute folder) is kept for the process. On a busy Mac
// npm can fail, time out, or print nothing; such an answer is asked again once, and when it is still no folder the
// caller hears what npm did, so it never reads an empty answer as a relative path and reports a missing install.
// Each try waits TIMEOUT_MS, or the limit its caller gives; the capture asks before its own bound starts, so two tries
// fit the time store.CAPTURE_TIMEOUT leaves beyond BOUNDS.captureSeconds. An npm missing from PATH is not asked again:
// the answer names the command that installs it.
// npmroot.py keeps the same rule for devkit's Python (the comparison, the marks, `dev visual`, the packages step and
// `dev doctor`): change both together.
//
// Interface:
//
//   globalRoot(timeoutMs = TIMEOUT_MS) -> {root}   the absolute folder npm named
//                                       | {why}    what npm did on its last try and ", twice": "it printed nothing,
//                                                  twice", "it did not answer in 25 s, twice", …; or NOT_INSTALLED
//   TRIES, TIMEOUT_MS                              the tries, and the milliseconds one waits by default
'use strict';

const path = require('path');
const { spawnSync } = require('child_process');

const TRIES = 2;
const TIMEOUT_MS = 25000; // one try, unless the caller gives its own limit
const NOT_INSTALLED = 'npm is not installed: run dev install --only tools';

let kept = null; // the good answer, for the rest of the process

function ask(timeoutMs) {
  const npm = spawnSync('npm', ['root', '-g'], { encoding: 'utf8', timeout: timeoutMs });
  if (npm.error && npm.error.code === 'ETIMEDOUT') return { why: `it did not answer in ${timeoutMs / 1000} s` };
  if (npm.error && npm.error.code === 'ENOENT') return { why: NOT_INSTALLED };
  if (npm.error) return { why: `it could not start (${npm.error.code || 'error'})` };
  if (npm.signal) return { why: `it was stopped by ${npm.signal}` };
  if (npm.status !== 0) return { why: `it failed with exit ${npm.status}` };
  const out = String(npm.stdout || '').trim();
  if (!out) return { why: 'it printed nothing' };
  if (out.includes('\n') || !path.isAbsolute(out)) return { why: 'it printed no absolute folder' };
  return { root: out };
}

function globalRoot(timeoutMs = TIMEOUT_MS) {
  if (kept) return { root: kept };
  const limit = timeoutMs > 0 ? timeoutMs : TIMEOUT_MS; // spawnSync waits with no limit at all on 0, as npmroot.py does not
  let answer = null;
  for (let i = 0; i < TRIES; i += 1) {
    answer = ask(limit);
    if (answer.root) {
      kept = answer.root;
      return answer;
    }
    if (answer.why === NOT_INSTALLED) return answer; // a second ask cannot find it either
  }
  return { why: `${answer.why}, twice` };
}

module.exports = { globalRoot, TRIES, TIMEOUT_MS };
