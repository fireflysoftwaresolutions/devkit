// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// A stand-in for `dev impeccable detect`, used by tests/test_visual_capture.py: the capture driver runs it as its
// detector with `detect --cdp <port> --target <text> --json`, while the captured page is still open. It records what
// it saw into the file named by DETECTOR_LOG (the port it was given, the addresses that port listens on, the pages
// the port lists, and the first line of DevToolsActivePort under TMPDIR), then answers as impeccable does: one
// low-contrast finding on #low, with the scanned page's URL in `file`, and exit code 2 (findings).
'use strict';

const fs = require('fs');
const http = require('http');
const path = require('path');
const { execFileSync } = require('child_process');

function find(dir, name) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isFile() && entry.name === name) return full;
    if (entry.isDirectory()) {
      const found = find(full, name);
      if (found) return found;
    }
  }
  return null;
}

function list(port) {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${port}/json/list`, (res) => {
      let body = '';
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => resolve(JSON.parse(body)));
    }).on('error', reject);
  });
}

(async () => {
  const args = process.argv.slice(2);
  const port = Number(args[args.indexOf('--cdp') + 1]);
  let listening = [];
  try {
    listening = execFileSync('lsof', ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN', '-Fn'], { encoding: 'utf8' })
      .split('\n').filter((line) => line.startsWith('n')).map((line) => line.slice(1));
  } catch (_) {
    listening = [];
  }
  const pages = (await list(port)).filter((target) => target.type === 'page');
  const file = find(process.env.TMPDIR, 'DevToolsActivePort');
  const active = file ? Number(fs.readFileSync(file, 'utf8').split('\n')[0]) : null;
  fs.writeFileSync(process.env.DETECTOR_LOG, JSON.stringify({
    args, port, listening, active, pages: pages.map((p) => ({ title: p.title, url: p.url })),
  }));
  process.stdout.write(JSON.stringify([{
    antipattern: 'low-contrast', name: 'Low contrast text', description: 'Text does not meet WCAG AA contrast.',
    severity: 'warning', category: 'quality', file: pages.length ? pages[0].url : null, line: 0,
    snippet: '1.9:1 (need 4.5:1)', selector: '#low',
  }]));
  process.exit(2);
})().catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
