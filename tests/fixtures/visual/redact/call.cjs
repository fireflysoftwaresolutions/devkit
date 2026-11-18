// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Test harness for lib/devkit/visual/redact.cjs, used by tests/test_visual_redact.py: one node process runs a list of
// calls and prints their results, so the Python tests check the module's real behavior without a JavaScript test
// runner.
//
//   node call.cjs < {"calls": [{"fn": "redactUrl", "args": ["/x?token=abc"]}, ...]}
//   -> {"results": [{"ok": true, "value": ...} | {"ok": false, "code": "...", "message": "..."}, ...]}
//
// Any argument written as {"join": [parts]} is the parts joined, so test files never hold a whole token.
'use strict';

const fs = require('fs');
const path = require('path');

const redact = require(path.resolve(__dirname, '..', '..', '..', '..', 'lib', 'devkit', 'visual', 'redact.cjs'));

function resolveJoins(value) {
  if (Array.isArray(value)) return value.map(resolveJoins);
  if (value && typeof value === 'object') {
    const keys = Object.keys(value);
    if (keys.length === 1 && keys[0] === 'join' && Array.isArray(value.join)) return value.join.join('');
    const out = {};
    for (const key of keys) out[key] = resolveJoins(value[key]);
    return out;
  }
  return value;
}

const request = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = request.calls.map(({ fn, args }) => {
  if (typeof redact[fn] !== 'function') return { ok: false, code: 'no-such-function', message: `redact.cjs has no ${fn}` };
  try {
    return { ok: true, value: redact[fn](...resolveJoins(args)) };
  } catch (error) {
    return { ok: false, code: error.code || null, message: String(error.message) };
  }
});
process.stdout.write(JSON.stringify({ results }));
