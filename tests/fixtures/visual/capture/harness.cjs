// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Test harness for lib/devkit/visual/capture.cjs, used by tests/test_visual_capture.py. One node process runs one
// request and prints one JSON result:
//
//   {"mode": "call", "calls": [{"fn": "adapt", "args": [...]}, ...]}
//     -> {"results": [{"ok": true, "value": ...} | {"ok": false, "code": "...", "message": "..."}]}
//   {"mode": "capture", "core": "<playwright-core>", "request": {...}, "watch": ["text", ...],
//    "snapshot_file": "<fixture snapshot>" | null, "probe_port": true | false,
//    "before_screenshot": {"selector": "<css>", "texts": ["text set before the 1st screenshot", "before the 2nd"]}}
//     -> {"ok": true, "record": {...}} | {"ok": false, "code": "...", "step": "...", "message": "..."}, plus "spy":
//        the launch options, the browser's spawn arguments (read with ps), the context options, whether tracing
//        started, the files under the capture's folder that held a watched text just before the browser closed, and,
//        with probe_port, what a WebSocket opened from the captured page to the DevTools port did and whether the
//        port still answered after the capture
//   with "bounds": {"captureSeconds": n}, the capture runs with that bound lowered; the result adds "seconds" taken
//   {"mode": "bounds"} -> the bounds capture.cjs holds (capture.BOUNDS)
//   {"mode": "pixels", "core": "<playwright-core>", "png": "<file>", "boxes": [[x, y, w, h], ...],
//    "compare": {"png": "<file>", "at": [x, y]} | null}
//     -> {"width": w, "height": h, "boxes": [{"nonblack": n, "total": n}], "same": true | false | null}
//        decoded by a canvas in a browser of its own, so no test depends on the driver's own PNG code
//
// The spy wraps the real chromium of the pinned playwright-core: the capture still launches the real browser.
'use strict';

const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const http = require('http');
const { execFileSync } = require('child_process');

const capture = require(path.resolve(__dirname, '..', '..', '..', '..', 'lib', 'devkit', 'visual', 'capture.cjs'));

function filesUnder(folder) {
  const out = [];
  const walk = (dir) => {
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile()) out.push(full);
    }
  };
  walk(folder);
  return out;
}

function holding(folder, texts) {
  const found = [];
  for (const file of filesUnder(folder)) {
    let bytes = null;
    try {
      bytes = fs.readFileSync(file);
    } catch (_) {
      continue;
    }
    for (const text of texts) {
      if (bytes.includes(Buffer.from(text, 'utf8')) || bytes.includes(Buffer.from(text, 'utf16le'))) {
        found.push({ file: path.relative(folder, file), text });
      }
    }
  }
  return found;
}

function getJson(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let body = '';
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => {
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    }).on('error', reject);
  });
}

function connects(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: '127.0.0.1', port }, () => {
      socket.destroy();
      resolve(true);
    });
    socket.on('error', () => resolve(false));
  });
}

function devToolsPort(folder) {
  const file = filesUnder(folder).find((f) => path.basename(f) === 'DevToolsActivePort');
  return file ? Number(fs.readFileSync(file, 'utf8').split('\n')[0]) : null;
}

function spyOn(chromium, options, log) {
  const spy = log;
  return {
    launch: async (launchOptions) => {
      log.launch = JSON.parse(JSON.stringify(launchOptions));
      const browser = await chromium.launch(launchOptions);
      log.folder = process.env.TMPDIR;
      log.spawn = execFileSync('ps', ['-axww', '-o', 'args='], { encoding: 'utf8' }).split('\n')
        .filter((line) => line.includes(log.folder) && !line.includes('--type=')).map((line) => line.split(' '));
      const newContext = browser.newContext.bind(browser);
      browser.newContext = async (contextOptions) => {
        log.context = JSON.parse(JSON.stringify(contextOptions));
        const context = await newContext(contextOptions);
        const start = context.tracing.start.bind(context.tracing);
        context.tracing.start = async (...args) => {
          log.tracing = true;
          return start(...args);
        };
        const newPage = context.newPage.bind(context);
        context.newPage = async () => {
          const page = await newPage();
          if (options.snapshot_file) {
            const fixture = JSON.parse(fs.readFileSync(options.snapshot_file, 'utf8'));
            page.ariaSnapshotJSON = async () => fixture;
          }
          if (options.before_screenshot) {
            // the page changes its text right before the screenshot: once, or before every screenshot
            const { selector, texts } = options.before_screenshot;
            const screenshot = page.screenshot.bind(page);
            let calls = 0;
            page.screenshot = async (...args) => {
              if (calls < texts.length) {
                const text = texts[calls];
                await page.evaluate(([s, t]) => { document.querySelector(s).textContent = t; }, [selector, text]);
              }
              calls += 1;
              spy.screenshots = calls;
              return screenshot(...args);
            };
          }
          return page;
        };
        return context;
      };
      const close = browser.close.bind(browser);
      browser.close = async (...args) => {
        if (Array.isArray(options.watch) && options.watch.length) log.held = holding(log.folder, options.watch);
        if (options.probe_port) {
          const port = devToolsPort(log.folder);
          log.port = port;
          if (port) {
            const version = await getJson(`http://127.0.0.1:${port}/json/version`);
            const page = browser.contexts()[0].pages()[0];
            log.page_websocket = await page.evaluate((url) => new Promise((resolve) => {
              const ws = new WebSocket(url);
              ws.onopen = () => { ws.close(); resolve('open'); };
              ws.onerror = () => resolve('error');
            }), version.webSocketDebuggerUrl);
          }
        }
        const result = await close(...args);
        if (log.port) log.after_close = (await connects(log.port)) ? 'answered' : 'refused';
        return result;
      };
      return browser;
    },
  };
}

async function runCapture(input) {
  const { chromium } = require(input.core);
  const spy = { tracing: false };
  // a test lowers a bound (capture.BOUNDS) so it can reach it in seconds; the request cannot change them
  if (input.bounds) Object.assign(capture.BOUNDS, input.bounds);
  const started = Date.now();
  try {
    const record = await capture.run(input.request, { chromium: spyOn(chromium, input, spy) });
    return { ok: true, record, spy, seconds: (Date.now() - started) / 1000 };
  } catch (error) {
    return { ok: false, code: error.code || null, step: error.step || null, message: String(error.message), spy,
      seconds: (Date.now() - started) / 1000 };
  }
}

async function pixels(input) {
  const { chromium } = require(input.core);
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'visual-pixels-'));
  process.env.TMPDIR = folder;
  const browser = await chromium.launch({ channel: 'chromium', headless: true, chromiumSandbox: true });
  try {
    const page = await browser.newPage();
    const data = fs.readFileSync(input.png).toString('base64');
    const other = input.compare ? fs.readFileSync(input.compare.png).toString('base64') : null;
    return await page.evaluate(async ({ data, other, boxes, at }) => {
      const load = async (b64) => {
        const bitmap = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
        const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
        const g = canvas.getContext('2d');
        g.drawImage(bitmap, 0, 0);
        return { g, width: bitmap.width, height: bitmap.height };
      };
      const img = await load(data);
      const out = { width: img.width, height: img.height, same: null };
      out.boxes = boxes.map(([x, y, w, h]) => {
        const d = img.g.getImageData(x, y, w, h).data;
        let nonblack = 0;
        for (let i = 0; i < d.length; i += 4) if (d[i] || d[i + 1] || d[i + 2]) nonblack += 1;
        return { nonblack, total: d.length / 4 };
      });
      if (other) {
        const big = await load(other);
        const a = img.g.getImageData(0, 0, img.width, img.height).data;
        const b = big.g.getImageData(at[0], at[1], img.width, img.height).data;
        out.same = a.length === b.length && a.every((v, i) => v === b[i]);
      }
      return out;
    }, { data, other, boxes: input.boxes || [], at: input.compare ? input.compare.at : null });
  } finally {
    await browser.close();
    fs.rmSync(folder, { recursive: true, force: true });
  }
}

function calls(input) {
  const results = input.calls.map(({ fn, args }) => {
    if (typeof capture[fn] !== 'function') return { ok: false, code: 'no-such-function', message: `capture.cjs has no ${fn}` };
    try {
      return { ok: true, value: capture[fn](...args) };
    } catch (error) {
      return { ok: false, code: error.code || null, message: String(error.message) };
    }
  });
  return { results };
}

(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  let out;
  if (input.mode === 'call') out = calls(input);
  else if (input.mode === 'capture') out = await runCapture(input);
  else if (input.mode === 'pixels') out = await pixels(input);
  else if (input.mode === 'bounds') out = { ...capture.BOUNDS };
  else throw new Error(`unknown mode ${input.mode}`);
  process.stdout.write(JSON.stringify(out));
})().catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
