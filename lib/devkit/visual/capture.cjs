// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// The capture driver of visual diagnosis: it takes one picture of a web page or a PDF page with the recipe that
// reproduces it, builds its description (devkit.visual.description/1), runs the checks on the same page state, passes
// every string through redact.cjs, and prints one capture record. It drives playwright-core from the pinned
// @playwright/cli (config/npm-globals.txt) directly and never runs playwright-cli, whose snapshots go unredacted into
// the working folder.
//
// The web capture sequence, in one browser session; nothing is written before step 10 has passed:
//
//    1. Launch Chromium (channel chromium, headless, chromiumSandbox: true, no crash reporter) with its profile in a
//       new 0700 folder under TMPDIR, deleted when the capture ends, also after a failure. When the detector runs, the
//       launch adds --remote-debugging-port=0 on 127.0.0.1, and the port Chromium chose is read from
//       DevToolsActivePort in that profile. One context (the recipe's viewport, scale, color scheme, reduced motion,
//       locale, time zone, forced colors, contrast, storage state; service workers blocked), one page. Unless the
//       request is remote, a top-level navigation to another host is stopped before its request is sent.
//    2. Load the source, run the steps; for a full-page picture, scroll through the page (to BOUNDS.pageHeight) so lazy
//       images and content shown on scroll load, wait for those images to decode, and scroll back to where the steps
//       left it; wait for document.fonts.ready and two animation frames. After the load, after each step, and before
//       the read the page must still be on this Mac: a redirect, a link, or a script that takes it (or tried to take
//       it) to another host fails the capture unless the request is remote.
//    3. Check the width (the page's innerWidth is the asked width) and the height: a full page taller than
//       BOUNDS.pageHeight is cut there, in the picture and the description, and the record is marked truncated.
//    4. Read the page: the ARIA snapshot (mode ai, with boxes), and in every frame one walk in an isolated JavaScript
//       world (styles, short selectors, form values, the fields to mask, the text), so a page cannot fake what the
//       walk reads. The walk enters open shadow roots and the closed ones CDP finds, and an element masked once stays
//       masked for the capture. The adapter builds the description from an allowlist of snapshot fields; text a
//       masked element left in an element that holds it is taken out.
//    5. Redact in memory (redact.cjs): control characters, secrets (gitleaks), form values, URLs; the mask list for
//       every frame.
//    6. Take the full-page screenshot with the masks painted #000000 (the mask rules' locator in every frame), then
//       check what each masked element shows in the picture (its box, its descendants' boxes, and its text's
//       rectangles) and paint over any of it that shows through (a frame Playwright could not mask is painted whole).
//    7. Read what the masks show and the text again: when a mask moved, or the text changed and holds a secret, go
//       back to step 4 once (retried); when it happens again, fail. A change with no secret is recorded as
//       text_changed.
//    8. Run the checks on the same state, each in an isolated world: checks.js, then axe in each frame (axe.cjs),
//       then the detector (`<impeccable> detect --cdp <port> --target <page> --json`) as the last check.
//    9. Close the browser.
//   10. Redact the signals (those below a cut picture are left out; none keeps a masked element's text), fold the
//       duplicates (fold.js), and number them.
//   11. Write the picture (and the element's crop) and the description into the store, atomically, 0600 files in
//       0700 folders.
//
// The record's source URL passes redact.cjs's redactSourceUrl (the controls, the whole secret scan, the URL rules); a
// URL that redaction changed, like a recipe it changed, makes the record not reproducible. A document's record names
// its file by its base name.
//
// Steps 1 to 10 of a web capture end within BOUNDS.captureSeconds: at the bound the browser is closed, the capture
// fails naming the bound and the step it reached, and nothing is written.
//
// A PDF page renders through PDFKit (`osascript -l JavaScript`, part of macOS): landscape pages 1920 px wide,
// portrait pages 1240 px (150 dpi on A4); its text lines, with their boxes, are the description's elements.
//
// Interface:
//
//   run(request, deps) -> record           the whole capture, store writes included; throws CaptureError
//   capture(request, deps) -> {record, image, crop, description}   steps 1 to 10, writes nothing
//   adapt(snapshot, ctx) -> {elements, values, refs, joins, total, truncated}   the description adapter, pure
//
// Command line, for visual.py:
//
//   node capture.cjs --json < request
//     request: {"id": "VC-…", "source": {"url": "…"} | {"file": "<pdf>", "page": n}, "recipe": {…}, "state": "…",
//               "element": "<css>" | null, "mask": ["<css>", …], "components": [{"id", "selector"}], "remote": bool,
//               "check": bool, "impeccable": {"command": ["dev", "impeccable"], "version": "…"} | null,
//               "store": "<folder>", "core": "<playwright-core folder>" | null, "cwd": "<folder>" | null}
//     -> one capture record on stdout (the fields visual.py does not add itself), exit 0.
//   Exit 1 when the capture failed (stderr names the step and the fix), 2 when the request is wrong. On exit 1 or 2
//   stdout is empty and nothing was written.
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');
const { spawn, spawnSync } = require('child_process');

const redact = require('./redact.cjs');

const SCHEMA = 'devkit.visual.description/1';
const MAX_ELEMENTS = 5000;
const STEP_TIMEOUT_MS = 10000;
const LOAD_TIMEOUT_MS = 30000;
const DETECT_TIMEOUT_MS = 60000;
const LAZY_WAIT_MS = 5000; // how long a full-page capture waits for the images its scroll through the page loaded
const LAZY_QUIET_MS = 500; // the time without a layout shift a full-page capture waits for before that scroll
const CROP_MARGIN = 24;
const MASK_COLOR = '#000000';
const WORLD = 'devkit-capture';
const VERBS = ['goto', 'click', 'fill', 'select', 'check', 'hover', 'press', 'scroll', 'wait', 'route'];
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);
const STATES = ['checked', 'disabled', 'expanded', 'pressed', 'selected', 'level', 'invalid'];
const FORM_ROLES = new Set(['textbox', 'searchbox', 'combobox', 'spinbutton']);
const INTERACTIVE = new Set(['button', 'link', 'textbox', 'searchbox', 'combobox', 'spinbutton', 'checkbox', 'radio',
  'switch', 'slider', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'treeitem', 'gridcell']);
const NORMALIZE_BUDGET = 300;
// R74: one capture, its steps and checks included, ends within captureSeconds, and a full-page picture is at most
// pageHeight CSS px high (the picture and the description stop there; the record says the page's full height). A
// test lowers them in-process; a request cannot change them.
const BOUNDS = { captureSeconds: 90, pageHeight: 16384 };
const FIX_INSTALL = 'dev install --only tools';

// R13: the fields painted over in every frame, whatever their type, and the elements marked for it
const MASK_RULES = ['input[type=password i]', '[autocomplete~="current-password" i]', '[autocomplete~="new-password" i]',
  '[autocomplete~="one-time-code" i]', '[autocomplete^="cc-" i]', '[autocomplete*=" cc-" i]', '[data-visual-mask]'];

// the tags a snapshot role is first joined to when several elements share its box
const ROLE_TAGS = {
  button: ['button', 'input', 'summary'], link: ['a', 'area'], textbox: ['input', 'textarea'], searchbox: ['input'],
  combobox: ['select', 'input'], spinbutton: ['input'], checkbox: ['input'], radio: ['input'], slider: ['input'],
  img: ['img', 'svg', 'canvas', 'picture'], heading: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'], iframe: ['iframe', 'frame'],
  paragraph: ['p'], list: ['ul', 'ol', 'menu'], listitem: ['li'], table: ['table'], row: ['tr'], cell: ['td'],
  columnheader: ['th'], rowheader: ['th'], navigation: ['nav'], main: ['main'], banner: ['header'],
  contentinfo: ['footer'], form: ['form'], option: ['option'], dialog: ['dialog'], article: ['article'],
  region: ['section'], complementary: ['aside'],
};

// The screen pass each detector rule reports to (the rule catalog of impeccable skill-v4.8.0, by its type and layout
// scopes and what the rules measure); a rule not listed goes to the hierarchy pass, which the detector feeds.
const DETECTOR_PASSES = {
  color: ['low-contrast', 'gray-on-color', 'ai-color-palette', 'cream-palette', 'dark-glow', 'radial-halo',
    'radial-spotlight-glow', 'gradient-text', 'design-system-color'],
  typography: ['overused-font', 'flat-type-hierarchy', 'italic-serif-display', 'hero-eyebrow-chip',
    'kicker-above-heading', 'numbered-section-labels', 'oversized-h1', 'extreme-negative-tracking', 'line-length',
    'tight-leading', 'skipped-heading', 'justified-text', 'straggler', 'unbalanced-heading', 'tiny-text',
    'undersized-ui-text', 'all-caps-body', 'wide-tracking', 'design-system-font', 'design-system-font-size'],
  spacing: ['monotonous-spacing', 'cramped-padding'],
  responsive: ['first-viewport-column-overflow', 'body-text-viewport-edge'],
  layout: ['nested-cards', 'icon-tile-stack', 'content-hidden-at-rest', 'edge-flush-cards', 'text-occlusion',
    'heading-rhythm', 'text-overflow', 'clipped-overflow-container'],
  content: ['broken-image', 'marketing-buzzword', 'em-dash-overuse', 'aphoristic-cadence', 'theater-slop-phrase',
    'repeated-container-text'],
  states: ['script-error'],
};

const STEP_NAMES = {
  0: 'check the request', 1: 'launch the browser', 2: 'load the page and run the steps', 3: 'check the width',
  4: 'read the page', 5: 'redact', 6: 'take the screenshot', 7: 'read the text again', 8: 'run the checks',
  9: 'close the browser', 10: 'redact the signals', 11: 'write the store',
};

const USAGE = 'Usage: node capture.cjs --json < request\n' +
  '  Takes one capture: a web page (source.url) or one PDF page (source.file, source.page), with the recipe in the\n' +
  '  request, and prints its capture record on stdout. The picture and the description go into request.store.\n' +
  '  Every string is redacted first (redact.cjs); nothing is written when a step fails.\n' +
  '\n' +
  '  The request: {"source": {"url": "http://127.0.0.1:4200/x"}, "store": "<folder>",\n' +
  '    "recipe": {"viewport": [390, 844], "dpr": 1, "theme": "light", "steps": ["click #save", "scroll 3000"]},\n' +
  '    "element": "<css to crop>", "mask": ["<css>"], "check": true, "impeccable": {"command": ["dev", "impeccable"]}}\n' +
  `  Step verbs: ${VERBS.join(', ')}.\n` +
  '\n' +
  '  Exit 0 when done, 1 when the capture failed (stderr names the step and the fix), 2 when the request is wrong.\n' +
  '  When it fails:\n' +
  `    gitleaks or the pinned Playwright is not installed   ${FIX_INSTALL}\n` +
  '    "Step N, <step>, found nothing in 10 s"              fix the target, or add a wait step before it\n' +
  '    "The page\'s text changed after the screenshot"       add a wait step for what the page shows last\n' +
  '    "<host> is not this Mac"                             capture fixture or demo data, with "remote": true\n' +
  '    "The page went to <host> ..." or "tried to go to"    the source or a step leaves this Mac: fix the redirect or\n' +
  '                                                         the step, or capture with "remote": true\n' +
  '    "A masked element moved ..."                         add a wait step for what the page shows last\n' +
  '    "Playwright\'s snapshot has ..."                      the adapter does not know this Playwright: keep the pin\n';

class CaptureError extends Error {
  constructor(code, step, message) {
    super(message);
    this.name = 'CaptureError';
    this.code = code;
    this.step = step;
  }
}

function bad(message) {
  return new CaptureError('bad-request', 0, message);
}

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

// URLs inside an error message keep no credential: each one passes the URL rules.
function safeMessage(error) {
  const first = String((error && error.message) || error).split('\n')[0];
  return first.replace(/[A-Za-z][A-Za-z0-9+.-]*:\/\/[^\s"'<>]+/g, (url) => redact.redactUrl(url).url).slice(0, 300);
}

// --------------------------------------------------------------------------- the request

// A step's target is a Playwright selector, which may hold spaces inside quotes and brackets: it ends at the first
// space outside them. -> [target, the rest]
function splitTarget(text) {
  let depth = 0;
  let quote = null;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quote) {
      if (ch === '\\') i += 1;
      else if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '[' || ch === '(') {
      depth += 1;
    } else if (ch === ']' || ch === ')') {
      depth -= 1;
    } else if (/\s/.test(ch) && depth <= 0) {
      return [text.slice(0, i), text.slice(i).trim()];
    }
  }
  return [text, ''];
}

function unquote(text) {
  const m = /^(["'])([\s\S]*)\1$/.exec(text);
  return m ? m[2] : text;
}

function isLocal(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch (_) {
    return null;
  }
  if (parsed.protocol === 'file:') return true;
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  return LOCAL_HOSTS.has(parsed.hostname);
}

function remoteRefusal(url) {
  let host = url;
  try {
    host = new URL(url).host;
  } catch (_) {
    // the URL was checked before
  }
  return bad(`${host} is not this Mac, so it is captured only with --remote (and then only fixture or demo data). ` +
    'Nothing was recorded.');
}

// "click role=link[name=\"Invoices\"]" -> {verb, target, value, text}; text is the step as written, shown in messages
// without its value (a fill step's text may be a password).
function parseStep(step, index, remote) {
  const where = `Step ${index + 1}, ${typeof step === 'string' ? step : JSON.stringify(step)}`;
  if (typeof step !== 'string' || !step.trim()) throw bad(`${where}: a step is a line of text such as "click #save".`);
  const line = step.trim();
  const space = line.search(/\s/);
  const verb = space < 0 ? line : line.slice(0, space);
  const rest = space < 0 ? '' : line.slice(space).trim();
  if (!VERBS.includes(verb)) throw bad(`${where}: unknown verb ${verb} (the verbs: ${VERBS.join(', ')}).`);
  // a verb with a value after its target splits them; the others take the whole rest as the target, so a selector
  // such as text=Export failed keeps its spaces
  const twoParts = ['fill', 'select', 'press', 'route'].includes(verb) || (verb === 'scroll' && /^-?[0-9]+\s+-?[0-9]+$/.test(rest));
  const [target, value] = twoParts ? splitTarget(rest) : [rest, ''];
  const shown = `Step ${index + 1}, ${verb}${target ? ` ${target}` : ''}`;
  const need = (ok, form) => {
    if (!ok) throw bad(`${where}: write it as ${form}.`);
  };
  const out = { verb, target, value: unquote(value), shown };
  if (verb === 'goto') {
    need(target && !value && isLocal(target) !== null, 'goto <http, https or file URL>');
    if (!isLocal(target) && !remote) throw remoteRefusal(target);
  } else if (verb === 'fill' || verb === 'select') {
    need(target && value !== '', `${verb} <target> <value>`);
  } else if (verb === 'press') {
    need(target, 'press <key> or press <target> <key>');
  } else if (verb === 'scroll') {
    need(target, 'scroll <y>, scroll <x> <y>, scroll top, scroll bottom, or scroll <target>');
  } else if (verb === 'wait') {
    need(target && !value, 'wait <target> or wait <milliseconds, at most 10000>');
    if (/^[0-9]+$/.test(target)) need(Number(target) <= STEP_TIMEOUT_MS, 'wait <milliseconds, at most 10000>');
  } else if (verb === 'route') {
    need(target && /^([1-5][0-9]{2}|abort|hold)$/.test(value), 'route <glob> <status>|abort|hold');
  } else {
    need(target && !value, `${verb} <target>`);
  }
  return out;
}

function normalize(request) {
  if (!request || typeof request !== 'object' || Array.isArray(request)) throw bad('The request is not a JSON object.');
  const source = request.source && typeof request.source === 'object' ? request.source : {};
  const remote = request.remote === true;
  const out = {
    id: typeof request.id === 'string' ? request.id : null,
    state: typeof request.state === 'string' ? request.state : null,
    remote, check: request.check !== false,
    impeccable: request.impeccable && Array.isArray(request.impeccable.command) && request.impeccable.command.length
      ? { command: request.impeccable.command.map(String), version: request.impeccable.version || null } : null,
    store: typeof request.store === 'string' && request.store ? path.resolve(request.store) : null,
    core: typeof request.core === 'string' && request.core ? request.core : null,
    cwd: typeof request.cwd === 'string' && request.cwd ? request.cwd : process.cwd(),
    element: typeof request.element === 'string' && request.element.trim() ? request.element.trim() : null,
    mask: Array.isArray(request.mask) ? request.mask : [],
    components: Array.isArray(request.components) ? request.components : [],
    // the project's spacing scale (DESIGN.md's tokens) off-scale-spacing checks against; without it the check does not run
    spacing: Array.isArray(request.spacing) && request.spacing.length &&
      request.spacing.every((n) => typeof n === 'number' && Number.isFinite(n)) ? request.spacing : null,
    recipe: request.recipe && typeof request.recipe === 'object' ? request.recipe : {},
  };
  if (!out.mask.every((s) => typeof s === 'string' && s.trim())) throw bad('Each mask selector is a CSS selector.');
  if (!out.components.every((c) => c && typeof c.id === 'string' && typeof c.selector === 'string')) {
    throw bad('Each map component is {"id": "...", "selector": "<css>"}.');
  }
  if (typeof source.file === 'string' && source.file) {
    if (!/\.pdf$/i.test(source.file)) throw bad(`${path.basename(source.file)} is not a PDF: convert it to PDF first.`);
    const page = Number(source.page);
    if (!Number.isInteger(page) || page < 1) throw bad('A PDF capture names its page: source.page, from 1.');
    out.kind = 'document';
    out.source = { file: path.resolve(source.file), page };
    return out;
  }
  if (typeof source.url !== 'string' || !source.url) throw bad('The request has no source: give source.url or source.file.');
  const local = isLocal(source.url);
  if (local === null) throw bad('The source URL is not an http, https or file URL.');
  if (!local && !remote) throw remoteRefusal(source.url);
  out.kind = 'web';
  out.source = { url: source.url, local };
  const r = out.recipe;
  const viewport = Array.isArray(r.viewport) ? r.viewport : [1280, 800];
  if (viewport.length !== 2 || !viewport.every((n) => Number.isInteger(n) && n >= 100 && n <= 8192)) {
    throw bad('recipe.viewport is [width, height] in CSS px, each from 100 to 8192.');
  }
  const dpr = r.dpr === undefined || r.dpr === null ? 1 : Number(r.dpr);
  if (!(dpr > 0 && dpr <= 4)) throw bad('recipe.dpr is a device pixel ratio from 0 to 4.');
  const steps = Array.isArray(r.steps) ? r.steps : [];
  out.steps = steps.map((step, i) => parseStep(step, i, remote));
  if (r.theme !== undefined && r.theme !== null && !['light', 'dark'].includes(r.theme)) throw bad('recipe.theme is light or dark.');
  const emulation = r.emulation && typeof r.emulation === 'object' ? r.emulation : {};
  out.web = {
    viewport, dpr, theme: r.theme || 'light', locale: r.locale || 'en-US', timezone: r.timezone || 'UTC',
    clock: r.clock || null, fullPage: r.full_page !== false, storageState: r.storage_state || null,
    forcedColors: emulation.forced_colors === true, contrast: emulation.contrast || null,
    vision: emulation.vision || null, css: typeof r.css === 'string' && r.css ? r.css : null,
  };
  if (out.web.contrast !== null && !['more', 'no-preference'].includes(out.web.contrast)) {
    throw bad('recipe.emulation.contrast is more or no-preference.');
  }
  return out;
}

// --------------------------------------------------------------------------- Playwright

function playwrightCore(req) {
  let core = req.core;
  if (!core) {
    const npm = spawnSync('npm', ['root', '-g'], { encoding: 'utf8', timeout: 60000 });
    core = path.join((npm.stdout || '').trim(), '@playwright', 'cli', 'node_modules', 'playwright-core');
  }
  let pkg;
  let browsers;
  try {
    pkg = JSON.parse(fs.readFileSync(path.join(core, 'package.json'), 'utf8'));
    browsers = JSON.parse(fs.readFileSync(path.join(core, 'browsers.json'), 'utf8')).browsers;
  } catch (_) {
    throw new CaptureError('no-playwright', 1, `The pinned Playwright is not installed (${core}). Install it: ${FIX_INSTALL}.`);
  }
  let cli = null;
  try {
    const parent = JSON.parse(fs.readFileSync(path.join(core, '..', '..', 'package.json'), 'utf8'));
    if (parent.name === '@playwright/cli') cli = parent.version;
  } catch (_) {
    cli = null;
  }
  const chromium = (browsers || []).find((b) => b.name === 'chromium');
  return { path: core, version: pkg.version, cli, revision: chromium ? chromium.revision : null };
}

// One isolated JavaScript world per frame, over CDP: the page's own scripts cannot reach it, so they cannot replace
// the built-ins the walk and the checks use. -> Map(frame -> {session, contextId, frameId})
async function isolatedWorlds(context, page) {
  const sessions = new Map();
  const ids = new Map();
  const trees = new Map();
  const order = [];
  const visit = (frame) => {
    order.push(frame);
    for (const child of frame.childFrames()) visit(child);
  };
  visit(page.mainFrame());
  for (const frame of order) {
    try {
      const session = await context.newCDPSession(frame);
      const tree = (await session.send('Page.getFrameTree')).frameTree;
      sessions.set(frame, session);
      trees.set(frame, tree);
      ids.set(frame, tree.frame.id);
    } catch (_) {
      // a frame in its parent's process shares the parent's session
    }
  }
  const owner = (frame) => (sessions.has(frame) ? frame : owner(frame.parentFrame()));
  const findIn = (tree, id) => {
    if (tree.frame.id === id) return tree;
    for (const child of tree.childFrames || []) {
      const found = findIn(child, id);
      if (found) return found;
    }
    return null;
  };
  const worlds = new Map();
  for (const frame of order) {
    try {
      const root = owner(frame);
      const session = sessions.get(root);
      if (!ids.has(frame)) {
        const parent = findIn(trees.get(root), ids.get(frame.parentFrame()));
        const taken = new Set(ids.values());
        const match = (parent && parent.childFrames ? parent.childFrames : []).find((c) => !taken.has(c.frame.id) &&
          c.frame.url === frame.url() && (c.frame.name || '') === (frame.name() || ''));
        if (!match) continue;
        ids.set(frame, match.frame.id);
      }
      const world = await session.send('Page.createIsolatedWorld', { frameId: ids.get(frame), worldName: WORLD,
        grantUniveralAccess: false });
      worlds.set(frame, { session, contextId: world.executionContextId, frameId: ids.get(frame),
        parentSession: frame.parentFrame() ? sessions.get(owner(frame.parentFrame())) : null });
    } catch (_) {
      // a frame that went away or cannot be reached: it is painted over whole
    }
  }
  return worlds;
}

async function inWorld(world, fn, ...args) {
  const result = await world.session.send('Runtime.callFunctionOn', {
    functionDeclaration: fn.toString(), executionContextId: world.contextId, arguments: args.map((value) => ({ value })),
    returnByValue: true, awaitPromise: true,
  });
  if (result.exceptionDetails) {
    const ex = result.exceptionDetails.exception;
    throw new Error((ex && ex.description) || result.exceptionDetails.text || 'the page script failed');
  }
  return result.result.value;
}

// Where a child frame's viewport starts in its parent frame's viewport: the frame element's box plus its border and
// padding, read in the parent's isolated world. -> [x, y]
async function frameOffset(world, parentWorld) {
  const { backendNodeId } = await world.parentSession.send('DOM.getFrameOwner', { frameId: world.frameId });
  const { object } = await world.parentSession.send('DOM.resolveNode', { backendNodeId,
    executionContextId: parentWorld.contextId });
  const result = await world.parentSession.send('Runtime.callFunctionOn', {
    objectId: object.objectId, returnByValue: true,
    functionDeclaration: 'function () { const b = this.getBoundingClientRect(), s = getComputedStyle(this); ' +
      'return [b.left + this.clientLeft + parseFloat(s.paddingLeft), b.top + this.clientTop + parseFloat(s.paddingTop), ' +
      'b.left, b.top, b.width, b.height]; }',
  });
  return result.result.value;
}

// A closed shadow root is out of reach of the page's own scripts and of Playwright's locators, so CDP finds every one
// in the frame's document (not in its frames, which have worlds of their own) and hands it to the frame's isolated
// world, where walk() reads it like an open one. -> how many there are
async function shareClosedRoots(world) {
  const { result } = await world.session.send('Runtime.evaluate', { expression: 'document', contextId: world.contextId });
  const { node } = await world.session.send('DOM.describeNode', { objectId: result.objectId, depth: -1, pierce: true });
  const closed = [];
  const visit = (n) => {
    for (const root of n.shadowRoots || []) {
      if (root.shadowRootType === 'closed') closed.push(root.backendNodeId);
      visit(root);
    }
    for (const child of n.children || []) visit(child);
  };
  visit(node);
  for (const backendNodeId of closed) {
    const { object } = await world.session.send('DOM.resolveNode', { backendNodeId, executionContextId: world.contextId });
    await world.session.send('Runtime.callFunctionOn', { objectId: object.objectId, functionDeclaration: 'function () { ' +
      'const roots = globalThis.__devkitClosedRoots || (globalThis.__devkitClosedRoots = []); ' +
      'if (!roots.includes(this)) roots.push(this); }' });
  }
  return closed.length;
}

// --------------------------------------------------------------------------- in-page functions (isolated world)

// Waits for the fonts and two animation frames. -> whether the fonts are loaded
async function settle() {
  await document.fonts.ready;
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  return document.fonts.status === 'loaded';
}

// Content that loads only near the viewport (loading="lazy" images, content an IntersectionObserver adds) is missing
// from a full-page picture, so a page taller than the viewport is scrolled through its height first: one viewport at a
// time with two animation frames each, `limit` CSS px down at most (the picture stops there); then its images that
// load are waited for until they decode (`waitMs` at most), and the page goes back to where the steps left it. The
// browser records a layout shift only for what is in the viewport, so the pass waits until the page has rendered
// four more frames (the work it scheduled for its first frames after the load or a step has then been laid out,
// however slow this Mac is), then until it has made no layout shift for `quietMs` (`waitMs` at most): a shift the page
// makes as it finishes loading is measured, as the layout-shift check measures the load and the steps.
async function revealLazy(limit, waitMs, quietMs) {
  const frames = () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const root = document.scrollingElement || document.documentElement;
  if (!root || root.scrollHeight <= innerHeight) return 0;
  await frames();
  await frames();
  if (PerformanceObserver.supportedEntryTypes.includes('layout-shift')) {
    await new Promise((done) => {
      let timer = null;
      const observer = new PerformanceObserver(() => {
        clearTimeout(timer);
        timer = setTimeout(finish, quietMs);
      });
      const cap = setTimeout(finish, waitMs);
      function finish() {
        observer.disconnect();
        clearTimeout(timer);
        clearTimeout(cap);
        done();
      }
      observer.observe({ type: 'layout-shift' });
      timer = setTimeout(finish, quietMs);
    });
  }
  const [x, y] = [scrollX, scrollY];
  const step = Math.max(100, innerHeight);
  for (let top = 0; top < Math.min(root.scrollHeight, limit); top += step) {
    scrollTo({ left: x, top, behavior: 'instant' });
    await frames();
  }
  const images = Array.from(document.images).filter((img) => {
    const r = img.getBoundingClientRect();
    return (r.width || r.height) && r.top + scrollY < limit && (img.loading === 'lazy' || !img.complete);
  });
  await Promise.race([Promise.all(images.map((img) => img.decode().catch(() => null))),
    new Promise((done) => setTimeout(done, waitMs))]);
  scrollTo({ left: x, top: y, behavior: 'instant' });
  await frames();
  return images.length;
}

// The page's text and form values, for the hash that tells whether the page changed after the screenshot.
function pageText() {
  const values = Array.from(document.querySelectorAll('input, textarea, select'), (el) => {
    if (el.localName === 'select') return Array.from(el.selectedOptions, (o) => o.text).join('\n');
    return el.value;
  });
  const editable = Array.from(document.querySelectorAll('[contenteditable]:not([contenteditable=false])'), (el) => el.innerText);
  return [document.documentElement ? document.documentElement.innerText : '', ...values, ...editable].join('\n');
}

// One pass over the frame's rendered elements, kept in this world for selectorsFor(): the document, every open shadow
// root in it, and the closed ones shareClosedRoots() handed this world. Each entry: its box in the frame's viewport,
// tag, style, whether a mask rule or a component matches, and the value of a form field. An element masked once stays
// masked for the capture, even when the page drops its marking. A masked entry also carries `cover`, what it shows in
// the frame's viewport: the union of its box, its descendants' boxes (across shadow roots), and its text's rectangles,
// since nowrap text runs past a narrow box and a positioned child can sit anywhere; a masked element with no box of its
// own (display: contents) is an entry when what it holds shows. It also carries, for this process's memory only, its
// text and the indexes of its ancestors (across shadow roots), so the text it leaves in an element that holds it can be
// taken out (coverMaskedText) and kept out of every signal (redact.cjs's redactSignals).
function walk(maskSelector, components) {
  const canvas = new OffscreenCanvas(1, 1).getContext('2d');
  const hex = (color) => {
    if (!color || color === 'transparent') return null;
    canvas.fillStyle = '#000000';
    canvas.fillStyle = color;
    const out = canvas.fillStyle;
    if (/^rgba\(.*,\s*0\)$/.test(out)) return null;
    const m = /^rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)$/.exec(out);
    if (m) {
      const h = (n) => Number(n).toString(16).padStart(2, '0');
      return `#${h(m[1])}${h(m[2])}${h(m[3])}${h(Math.round(Number(m[4]) * 255))}`;
    }
    return out;
  };
  let bad = null;
  const test = (sel) => {
    try {
      document.querySelector(sel);
      return true;
    } catch (_) {
      bad = sel;
      return false;
    }
  };
  const mask = maskSelector && test(maskSelector) ? maskSelector : null;
  const comps = components.filter((c) => test(c.selector));
  const BUTTONISH = new Set(['button', 'submit', 'reset', 'image', 'checkbox', 'radio', 'hidden']);
  if (!globalThis.__devkitMasked) globalThis.__devkitMasked = new Set();
  const sticky = globalThis.__devkitMasked;
  // what an element shows in the frame's viewport -> [x, y, width, height], or null when it shows nothing; kept in this
  // world for maskBoxes()
  const cover = (el) => {
    const edges = [Infinity, Infinity, -Infinity, -Infinity];
    const add = (r) => {
      if (!r.width && !r.height) return;
      edges[0] = Math.min(edges[0], r.left);
      edges[1] = Math.min(edges[1], r.top);
      edges[2] = Math.max(edges[2], r.right);
      edges[3] = Math.max(edges[3], r.bottom);
    };
    const closed = new Map((globalThis.__devkitClosedRoots || []).map((root) => [root.host, root]));
    const within = (root) => {
      const range = new Range();
      range.selectNodeContents(root);
      for (const r of range.getClientRects()) add(r);
      for (const d of root.querySelectorAll('*')) {
        add(d.getBoundingClientRect());
        const shadow = d.shadowRoot || closed.get(d);
        if (shadow) within(shadow);
      }
    };
    add(el.getBoundingClientRect());
    within(el);
    const own = el.shadowRoot || closed.get(el);
    if (own) within(own);
    return edges[0] === Infinity ? null : [edges[0], edges[1], edges[2] - edges[0], edges[3] - edges[1]];
  };
  globalThis.__devkitCover = cover;
  const list = [];
  const entries = [];
  const read = (el) => {
    const masked = (mask ? el.matches(mask) : false) || sticky.has(el);
    const r = el.getBoundingClientRect();
    const covered = masked ? cover(el) : null;
    if (!r.width && !r.height && !covered) return;
    const cs = getComputedStyle(el);
    const entry = {
      index: list.length, box: r.width || r.height ? [r.left, r.top, r.width, r.height] : covered, tag: el.localName,
      style: { font_size: Math.round(parseFloat(cs.fontSize) * 100) / 100, font_weight: Math.round(parseFloat(cs.fontWeight)),
        color: hex(cs.color), background: hex(cs.backgroundColor) },
      cursor: cs.cursor === 'pointer', mask: masked,
      testid: el.getAttribute('data-testid'), id: el.id || null, component: null,
    };
    if (masked) entry.cover = covered;
    const comp = comps.find((c) => el.matches(c.selector));
    if (comp) entry.component = comp.id;
    if (el.localName === 'input' && !BUTTONISH.has(el.type)) entry.value = el.value;
    else if (el.localName === 'textarea') entry.value = el.value;
    else if (el.localName === 'select') entry.value = Array.from(el.selectedOptions, (o) => o.text).join('\n');
    else if (el.isContentEditable && (!el.parentElement || !el.parentElement.isContentEditable)) entry.value = el.innerText;
    list.push(el);
    entries.push(entry);
  };
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      read(el);
      if (el.shadowRoot) visit(el.shadowRoot);
    }
  };
  visit(document);
  for (const root of globalThis.__devkitClosedRoots || []) if (root.isConnected) visit(root);
  const at = new Map(list.map((el, i) => [el, i]));
  const up = (node) => node.parentElement || (node.parentNode && node.parentNode.host) || null;
  entries.forEach((entry, i) => {
    if (!entry.mask) return;
    const el = list[i];
    sticky.add(el);
    entry.text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    entry.within = [];
    for (let node = up(el); node; node = up(node)) if (at.has(node)) entry.within.push(at.get(node));
  });
  globalThis.__devkitWalk = list;
  const root = document.documentElement;
  return { entries, bad, scroll: [scrollX, scrollY], viewport: [innerWidth, innerHeight],
    page: [root ? root.scrollWidth : 0, root ? root.scrollHeight : 0] };
}

// Where masked elements of the last walk are now, by entry index: what each shows in the frame's viewport (the walk's
// cover), or null when one is gone or shows nothing; with the frame's scroll.
function maskBoxes(indexes) {
  const list = globalThis.__devkitWalk || [];
  return {
    scroll: [scrollX, scrollY],
    boxes: indexes.map((i) => {
      const el = list[i];
      return el && el.isConnected ? globalThis.__devkitCover(el) : null;
    }),
  };
}

// Short CSS selectors (at most four steps) that match only their element, for walk entries by index; null when none.
function selectorsFor(indexes) {
  const list = globalThis.__devkitWalk || [];
  const esc = CSS.escape;
  const unique = (sel) => {
    try {
      return document.querySelectorAll(sel).length === 1;
    } catch (_) {
      return false;
    }
  };
  const one = (el) => {
    if (!el || !el.isConnected) return null;
    // devkit's order: the test id, the id, then a short path
    const testid = el.getAttribute('data-testid');
    if (testid !== null && unique(`[data-testid="${esc(testid)}"]`)) return `[data-testid="${esc(testid)}"]`;
    if (el.id && unique(`#${esc(el.id)}`)) return `#${esc(el.id)}`;
    const steps = [];
    for (let node = el; node && node.nodeType === 1 && steps.length < 4; node = node.parentElement) {
      if (node !== el && node.id && unique(`#${esc(node.id)}`)) {
        steps.unshift(`#${esc(node.id)}`);
        return unique(steps.join(' > ')) ? steps.join(' > ') : null;
      }
      const tag = node.localName;
      const same = node.parentElement ? Array.from(node.parentElement.children).filter((c) => c.localName === tag) : [node];
      steps.unshift(same.length > 1 ? `${esc(tag)}:nth-of-type(${same.indexOf(node) + 1})` : esc(tag));
      if (unique(steps.join(' > '))) return steps.join(' > ');
    }
    return null;
  };
  return indexes.map((i) => one(list[i]));
}

// The page box of each selector that matches exactly one element, else null.
function boxesOf(selectors) {
  return selectors.map((sel) => {
    let found = [];
    try {
      found = document.querySelectorAll(sel);
    } catch (_) {
      return null;
    }
    if (found.length !== 1) return null;
    const r = found[0].getBoundingClientRect();
    return [r.left + scrollX, r.top + scrollY, r.width, r.height].map(Math.round);
  });
}

// The page box of the element a crop names, or how many elements the selector matches.
function elementBox(selector) {
  let found;
  try {
    found = document.querySelectorAll(selector);
  } catch (_) {
    return { error: 'invalid' };
  }
  if (found.length !== 1) return { count: found.length };
  const r = found[0].getBoundingClientRect();
  return { box: [r.left + scrollX, r.top + scrollY, r.width, r.height].map(Math.round) };
}

// --------------------------------------------------------------------------- the description adapter (R7)

function boxKey(box) {
  return box.map((n) => Math.round(n)).join(',');
}

function snapshotError(message) {
  return new CaptureError('snapshot', 4, `Playwright's snapshot has ${message}, which devkit's adapter does not know, ` +
    'so nothing in it can be checked for what it carries. Nothing was recorded. The adapter in capture.cjs needs to ' +
    'learn it before captures run with this Playwright.');
}

function nodeType(node) {
  if (node === null) return 'a null node';
  if (Array.isArray(node)) return 'a list where a node belongs';
  if (typeof node !== 'object') return `a ${typeof node} node`;
  const keys = Object.keys(node).filter((k) => /^[A-Za-z_]{1,40}$/.test(k)).slice(0, 6);
  return `an object without a role (keys: ${keys.join(', ')})`;
}

function quoteJs(text) {
  return `'${String(text).replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
}

// devkit's own locator, when Playwright's normalized one is missing or carries page text or a form value: the test
// id or the id when the element's short selector is that alone (so it is unique), else its role with its name.
function fallbackLocator(role, name, selector) {
  const testid = typeof selector === 'string' ? /^\[data-testid="((?:[^"\\]|\\.)*)"\]$/.exec(selector) : null;
  if (testid) return `getByTestId(${quoteJs(testid[1].replace(/\\(.)/g, '$1'))})`;
  if (typeof selector === 'string' && /^#[^\s>:.[\]]+$/.test(selector)) return `locator(${quoteJs(selector)})`;
  if (role && name) return `getByRole(${quoteJs(role)}, { name: ${quoteJs(name)} })`;
  return null;
}

// adapt(snapshot, ctx) -> {elements, values, refs, joins, total, truncated}
//   snapshot  what page.ariaSnapshotJSON({mode: 'ai', boxes: true}) returned: a node or a list of nodes
//   ctx       {frames: {"<ref prefix>": frame index}, origins: [[x, y] page px of each frame's viewport],
//              walks: [[walk entries of each frame, boxes in that frame's viewport]], fold: viewport height,
//              limit: the page height the picture stops at, or null}
// An element whose box starts at or below `limit` is left out (its children keep its parent), and a box that
// crosses it ends there, so the description holds what the picture shows.
// Each element is built from the allowlisted snapshot fields only (role, name, the states, box, children, the text
// of roles that are not form controls, url, placeholder, cursor, ref); a form control's text is held apart in
// `values` for the secret scan, never in the element. Refs stay in `refs`, in memory, and are never stored.
function adapt(snapshot, ctx) {
  const frames = ctx.frames || {};
  const origins = ctx.origins || [[0, 0]];
  const walks = (ctx.walks || []).map((entries) => {
    const byKey = new Map();
    for (const entry of entries || []) {
      const key = boxKey(entry.box);
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key).push(entry);
    }
    return { byKey, used: new Set() };
  });
  const fold = ctx.fold || null;
  const limit = ctx.limit || null;
  const elements = [];
  const values = {};
  const refs = {};
  const joins = {};
  let total = 0;
  const frameOf = (ref, inherited) => {
    if (typeof ref !== 'string') return inherited;
    const m = /^(f[0-9]+)?e[0-9]+$/.exec(ref);
    if (!m) return inherited;
    const prefix = m[1] || '';
    return prefix === '' ? 0 : Object.prototype.hasOwnProperty.call(frames, prefix) ? frames[prefix] : inherited;
  };
  const join = (frame, role, box) => {
    const w = walks[frame];
    if (!w || !box) return null;
    const candidates = (w.byKey.get(boxKey(box)) || []).filter((e) => !w.used.has(e.index));
    const tags = ROLE_TAGS[role] || [];
    const entry = candidates.find((e) => tags.includes(e.tag)) || candidates[0] || null;
    if (entry) w.used.add(entry.index);
    return entry;
  };
  const visit = (node, parentId, frame) => {
    if (typeof node === 'string') return null;
    if (!node || typeof node !== 'object' || Array.isArray(node) || typeof node.role !== 'string') {
      throw snapshotError(nodeType(node));
    }
    const here = frameOf(node.ref, frame);
    const raw = node.box && typeof node.box === 'object' ? node.box : null;
    const vbox = raw && [raw.x, raw.y, raw.width, raw.height].every((n) => typeof n === 'number' && Number.isFinite(n))
      ? [raw.x, raw.y, raw.width, raw.height] : null;
    const empty = vbox && vbox.every((n) => n === 0);
    const origin = origins[here] || [0, 0];
    let box = vbox && !empty ? [vbox[0] + origin[0], vbox[1] + origin[1], vbox[2], vbox[3]].map(Math.round) : null;
    const children = Array.isArray(node.children) ? node.children : [];
    if (limit && box && box[1] >= limit) {
      for (const child of children) visit(child, parentId, here);
      return null;
    }
    if (limit && box && box[1] + box[3] > limit) box = [box[0], box[1], box[2], limit - box[1]];
    total += 1;
    const id = `E${total}`;
    const entry = empty ? null : join(here, node.role, vbox);
    const form = FORM_ROLES.has(node.role) || (entry && Object.prototype.hasOwnProperty.call(entry, 'value'));
    const fragments = children.filter((c) => typeof c === 'string');
    const ownText = typeof node.text === 'string' ? node.text : fragments.length ? fragments.join(' ') : null;
    const states = {};
    for (const key of STATES) {
      const v = node[key];
      if (key === 'level' ? Number.isInteger(v) : v === true || v === 'mixed') states[key] = v;
    }
    if (total <= MAX_ELEMENTS) {
      const name = typeof node.name === 'string' && node.name !== '' ? node.name : null;
      elements.push({
        id, parent: parentId, frame: here === 0 ? null : here, role: node.role, name,
        text: form ? null : ownText, states, box,
        locator: fallbackLocator(node.role, name, null), selector: null,
        url: typeof node.url === 'string' ? node.url : null,
        placeholder: typeof node.placeholder === 'string' ? node.placeholder : null,
        cursor: node.cursor === 'pointer' ? 'pointer' : null,
        style: entry ? entry.style : null,
        above_fold: box && fold ? box[1] < fold : null,
        component: entry ? entry.component : null, redacted: null,
      });
      if (form) {
        const held = [entry && typeof entry.value === 'string' ? entry.value : null, ownText]
          .filter((v, i, all) => typeof v === 'string' && v !== '' && all.indexOf(v) === i);
        values[id] = held.join('\n');
      }
      if (typeof node.ref === 'string') refs[id] = node.ref;
      if (entry) joins[id] = [here, entry.index, entry.mask];
    }
    for (const child of children) visit(child, total <= MAX_ELEMENTS ? id : null, here);
    return id;
  };
  for (const node of Array.isArray(snapshot) ? snapshot : [snapshot]) visit(node, null, 0);
  return { elements, values, refs, joins, total, truncated: total > MAX_ELEMENTS };
}

// --------------------------------------------------------------------------- PNG

function crcTable() {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
}
const CRC = crcTable();

function crc32(buffers) {
  let c = 0xffffffff;
  for (const buf of buffers) for (let i = 0; i < buf.length; i += 1) c = CRC[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// An 8-bit RGB or RGBA, non-interlaced PNG (what Chromium and PDFKit write) -> {width, height, data: RGBA}
function decodePng(buf) {
  let p = 8;
  let width = 0;
  let height = 0;
  let type = -1;
  const idat = [];
  while (p < buf.length) {
    const len = buf.readUInt32BE(p);
    const kind = buf.toString('ascii', p + 4, p + 8);
    const body = buf.subarray(p + 8, p + 8 + len);
    if (kind === 'IHDR') {
      width = body.readUInt32BE(0);
      height = body.readUInt32BE(4);
      if (body[8] !== 8 || body[12] !== 0 || (body[9] !== 6 && body[9] !== 2)) {
        throw new Error(`a PNG of a kind devkit does not read (bit depth ${body[8]}, color type ${body[9]})`);
      }
      type = body[9];
    } else if (kind === 'IDAT') {
      idat.push(body);
    }
    p += 12 + len;
  }
  const bpp = type === 6 ? 4 : 3;
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * bpp;
  const px = Buffer.alloc(height * stride);
  for (let y = 0; y < height; y += 1) {
    const filter = raw[y * (stride + 1)];
    const line = y * (stride + 1) + 1;
    const o = y * stride;
    for (let x = 0; x < stride; x += 1) {
      const a = x >= bpp ? px[o + x - bpp] : 0;
      const b = y ? px[o - stride + x] : 0;
      const c = x >= bpp && y ? px[o - stride + x - bpp] : 0;
      let v = raw[line + x];
      if (filter === 1) v += a;
      else if (filter === 2) v += b;
      else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) {
        const q = a + b - c;
        const pa = Math.abs(q - a);
        const pb = Math.abs(q - b);
        const pc = Math.abs(q - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      px[o + x] = v & 255;
    }
  }
  if (bpp === 4) return { width, height, data: px };
  const rgba = Buffer.alloc(width * height * 4);
  for (let i = 0, j = 0; i < px.length; i += 3, j += 4) {
    rgba[j] = px[i];
    rgba[j + 1] = px[i + 1];
    rgba[j + 2] = px[i + 2];
    rgba[j + 3] = 255;
  }
  return { width, height, data: rgba };
}

function encodePng(img) {
  const stride = img.width * 4;
  const raw = Buffer.alloc((stride + 1) * img.height);
  for (let y = 0; y < img.height; y += 1) img.data.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  const chunk = (kind, body) => {
    const head = Buffer.alloc(8);
    head.writeUInt32BE(body.length, 0);
    head.write(kind, 4, 'ascii');
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32([head.subarray(4), body]), 0);
    return Buffer.concat([head, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(img.width, 0);
  ihdr.writeUInt32BE(img.height, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })), chunk('IEND', Buffer.alloc(0))]);
}

// The pixel rectangle of a box in CSS px at this scale, clipped to the picture: [x0, y0, x1, y1), or null.
function pixelRect(img, box, scale, insetPx = 0) {
  const x0 = Math.max(0, Math.floor(box[0] * scale) + insetPx);
  const y0 = Math.max(0, Math.floor(box[1] * scale) + insetPx);
  const x1 = Math.min(img.width, Math.ceil((box[0] + box[2]) * scale) - insetPx);
  const y1 = Math.min(img.height, Math.ceil((box[1] + box[3]) * scale) - insetPx);
  return x1 > x0 && y1 > y0 ? [x0, y0, x1, y1] : null;
}

function isPainted(img, box, scale) {
  const r = pixelRect(img, box, scale, Math.ceil(scale));
  if (!r) return true;
  for (let y = r[1]; y < r[3]; y += 1) {
    for (let x = r[0]; x < r[2]; x += 1) {
      const o = (y * img.width + x) * 4;
      if (img.data[o] || img.data[o + 1] || img.data[o + 2]) return false;
    }
  }
  return true;
}

function paint(img, box, scale) {
  const r = pixelRect(img, box, scale);
  if (!r) return;
  for (let y = r[1]; y < r[3]; y += 1) {
    for (let x = r[0]; x < r[2]; x += 1) {
      const o = (y * img.width + x) * 4;
      img.data[o] = 0;
      img.data[o + 1] = 0;
      img.data[o + 2] = 0;
      img.data[o + 3] = 255;
    }
  }
}

function crop(img, box, scale) {
  const r = pixelRect(img, [box[0] - CROP_MARGIN, box[1] - CROP_MARGIN, box[2] + 2 * CROP_MARGIN, box[3] + 2 * CROP_MARGIN], scale);
  if (!r) return null;
  const width = r[2] - r[0];
  const height = r[3] - r[1];
  const data = Buffer.alloc(width * height * 4);
  for (let y = 0; y < height; y += 1) {
    img.data.copy(data, y * width * 4, ((r[1] + y) * img.width + r[0]) * 4, ((r[1] + y) * img.width + r[2]) * 4);
  }
  return { width, height, data };
}

// --------------------------------------------------------------------------- web capture

async function launchBrowser(chromium, folder, withPort) {
  const args = ['--disable-breakpad', '--disable-crash-reporter'];
  if (withPort) args.push('--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1');
  const browser = await chromium.launch({ channel: 'chromium', headless: true, chromiumSandbox: true, args });
  let port = null;
  if (withPort) {
    const profile = fs.readdirSync(folder).find((n) => n.startsWith('playwright_chromiumdev_profile-'));
    const file = profile ? path.join(folder, profile, 'DevToolsActivePort') : null;
    const deadline = Date.now() + 5000;
    while (file && !fs.existsSync(file) && Date.now() < deadline) await new Promise((r) => setTimeout(r, 20));
    const first = file && fs.existsSync(file) ? fs.readFileSync(file, 'utf8').split('\n')[0] : '';
    port = /^[0-9]{1,5}$/.test(first) ? Number(first) : null;
    if (!port) {
      await browser.close();
      throw new CaptureError('no-port', 1, 'Chromium did not write the DevToolsActivePort file, so the detector ' +
        'cannot reach the page. Nothing was recorded. Capture again, or capture with --no-check.');
    }
  }
  return { browser, port };
}

async function runStep(page, step) {
  const locate = (target) => page.locator(target).first();
  const timeoutMessage = () => `${step.shown}, found nothing in ${STEP_TIMEOUT_MS / 1000} s.`;
  try {
    switch (step.verb) {
      case 'goto':
        await page.goto(step.target, { waitUntil: 'load', timeout: LOAD_TIMEOUT_MS });
        break;
      case 'click':
        await locate(step.target).click({ timeout: STEP_TIMEOUT_MS });
        break;
      case 'fill':
        await locate(step.target).fill(step.value, { timeout: STEP_TIMEOUT_MS });
        break;
      case 'select':
        await locate(step.target).selectOption(step.value, { timeout: STEP_TIMEOUT_MS });
        break;
      case 'check':
        await locate(step.target).check({ timeout: STEP_TIMEOUT_MS });
        break;
      case 'hover':
        await locate(step.target).hover({ timeout: STEP_TIMEOUT_MS });
        break;
      case 'press':
        if (step.value) await locate(step.target).press(step.value, { timeout: STEP_TIMEOUT_MS });
        else await page.keyboard.press(step.target);
        break;
      case 'scroll': {
        const nums = [step.target, step.value].filter(Boolean);
        if (step.target === 'top' || step.target === 'bottom') {
          await page.evaluate((end) => window.scrollTo(0, end === 'top' ? 0 : document.documentElement.scrollHeight), step.target);
        } else if (nums.every((n) => /^-?[0-9]+$/.test(n))) {
          const [x, y] = nums.length === 2 ? nums.map(Number) : [0, Number(nums[0])];
          await page.evaluate(([sx, sy]) => window.scrollTo(sx, sy), [x, y]);
        } else {
          await locate(step.target).scrollIntoViewIfNeeded({ timeout: STEP_TIMEOUT_MS });
        }
        break;
      }
      case 'wait':
        if (/^[0-9]+$/.test(step.target)) await page.waitForTimeout(Number(step.target));
        else await locate(step.target).waitFor({ state: 'visible', timeout: STEP_TIMEOUT_MS });
        break;
      case 'route':
        await page.route(step.target, (route) => {
          if (step.value === 'abort') return route.abort();
          if (step.value === 'hold') return new Promise(() => {});
          return route.fulfill({ status: Number(step.value), body: '' });
        });
        break;
      default:
        throw bad(`${step.shown}: unknown verb.`);
    }
  } catch (error) {
    if (error instanceof CaptureError) throw error;
    if (error && error.name === 'TimeoutError') throw new CaptureError('step', 2, timeoutMessage());
    throw new CaptureError('step', 2, `${step.shown}: ${safeMessage(error)}`);
  }
}

// Step 4: the snapshot, the walks of every frame, and the description built from them, in memory.
async function readPage(page, worlds, req, maskSelector) {
  const frames = page.frames();
  const main = worlds.get(page.mainFrame());
  if (!main) throw new CaptureError('read', 4, 'The page could not be read in an isolated world. Nothing was recorded.');
  const walks = [];
  const scrolls = [];
  const origins = [];
  const unreadable = [];
  const frameBoxes = new Map(); // frame index -> the page box of its frame element
  const index = new Map(frames.map((f, i) => [f, i]));
  const visit = async (frame, origin) => {
    const i = index.get(frame);
    const world = worlds.get(frame);
    let result = null;
    if (world) {
      try {
        await shareClosedRoots(world);
        result = await inWorld(world, walk, maskSelector, req.components);
      } catch (_) {
        result = null;
      }
    }
    if (result && result.bad) {
      throw bad(`The selector ${result.bad} is not valid CSS (a mask or a component). Nothing was recorded.`);
    }
    walks[i] = result ? result.entries : [];
    scrolls[i] = result ? result.scroll : [0, 0];
    origins[i] = i === 0 && result ? result.scroll : origin;
    if (!result && i !== 0) unreadable.push(i);
    for (const child of frame.childFrames()) {
      const cw = worlds.get(child);
      let at = null;
      if (cw && world && cw.parentSession) {
        try {
          at = await frameOffset(cw, world);
        } catch (_) {
          at = null;
        }
      }
      const childOrigin = at ? [origins[i][0] + at[0], origins[i][1] + at[1]] : origins[i];
      if (!at) unreadable.push(index.get(child));
      await visit(child, childOrigin);
      if (at) frameBoxes.set(index.get(child), [origins[i][0] + at[2], origins[i][1] + at[3], at[4], at[5]]);
    }
    return result;
  };
  const top = await visit(page.mainFrame(), [0, 0]);
  if (!top) throw new CaptureError('read', 4, 'The page could not be read in an isolated world. Nothing was recorded.');
  // a frame that could not be read is painted over whole: its frame element's box, from Playwright when the walk
  // of its parent did not give it
  for (const i of unreadable) {
    if (frameBoxes.has(i)) continue;
    try {
      const handle = await frames[i].frameElement();
      const b = await handle.boundingBox();
      await handle.dispose();
      if (b) frameBoxes.set(i, [b.x + top.scroll[0], b.y + top.scroll[1], b.width, b.height]);
    } catch (_) {
      // a detached frame shows nothing
    }
  }
  const text = [];
  for (const frame of frames) {
    const world = worlds.get(frame);
    if (!world) continue;
    try {
      text.push(await inWorld(world, pageText));
    } catch (_) {
      // a frame that went away holds no text to compare
    }
  }
  const snapshot = await page.ariaSnapshotJSON({ mode: 'ai', boxes: true, timeout: LOAD_TIMEOUT_MS });
  // which frame each ref prefix (f1, f2…) belongs to
  const prefixes = {};
  const firstRefs = {};
  const findRefs = (node) => {
    if (!node || typeof node !== 'object') return;
    const m = typeof node.ref === 'string' ? /^(f[0-9]+)e[0-9]+$/.exec(node.ref) : null;
    if (m && !firstRefs[m[1]]) firstRefs[m[1]] = node.ref;
    for (const child of Array.isArray(node.children) ? node.children : []) findRefs(child);
  };
  for (const node of Array.isArray(snapshot) ? snapshot : [snapshot]) findRefs(node);
  for (const [prefix, ref] of Object.entries(firstRefs)) {
    try {
      const handle = await page.getByRef(ref).elementHandle({ timeout: STEP_TIMEOUT_MS });
      const owner = await handle.ownerFrame();
      await handle.dispose();
      if (owner && index.has(owner)) prefixes[prefix] = index.get(owner);
    } catch (_) {
      // a ref that no longer resolves: its nodes keep the frame of their parent
    }
  }
  const adapted = adapt(snapshot, { frames: prefixes, origins, walks, fold: req.web.viewport[1],
    limit: req.web.fullPage && top.page[1] > BOUNDS.pageHeight ? BOUNDS.pageHeight : null });
  // selectors from the walks, Playwright's normalized locators where they carry no page text or form value
  const byFrame = new Map();
  for (const [id, [frame, entryIndex]] of Object.entries(adapted.joins)) {
    if (!byFrame.has(frame)) byFrame.set(frame, []);
    byFrame.get(frame).push([id, entryIndex]);
  }
  const elements = new Map(adapted.elements.map((e) => [e.id, e]));
  for (const [frame, list] of byFrame) {
    const world = worlds.get(frames[frame]);
    if (!world) continue;
    let selectors = [];
    try {
      selectors = await inWorld(world, selectorsFor, list.map(([, i]) => i));
    } catch (_) {
      selectors = [];
    }
    list.forEach(([id], k) => {
      const e = elements.get(id);
      e.selector = selectors[k] || null;
      e.locator = fallbackLocator(e.role, e.name, e.selector);
    });
  }
  // normalize() costs a round trip that grows with the page (12 ms a ref on a 5,000-element page), so it runs only
  // where devkit's order gives no test id or id locator and the element is named or interactive, at most
  // NORMALIZE_BUDGET times; Playwright's text locators are not kept (they carry page text), so nameless containers
  // never ask for one
  const formValues = Object.values(adapted.values).flatMap((v) => v.split('\n')).filter((v) => v.length >= 4);
  const refs = Object.entries(adapted.refs).filter(([id]) => {
    const e = elements.get(id);
    return !/^(getByTestId|locator)\(/.test(e.locator || '') && (e.name !== null || INTERACTIVE.has(e.role));
  }).slice(0, NORMALIZE_BUDGET);
  const LIMIT = 16;
  for (let k = 0; k < refs.length; k += LIMIT) {
    await Promise.all(refs.slice(k, k + LIMIT).map(async ([id, ref]) => {
      try {
        const normalized = (await page.getByRef(ref).normalize()).toString();
        if (normalized.length <= 200 && !/getByText\(/.test(normalized) && !formValues.some((v) => normalized.includes(v))) {
          elements.get(id).locator = normalized;
        }
      } catch (_) {
        // keep devkit's own locator
      }
    }));
  }
  coverMaskedText(adapted, walks);
  // each masked entry is painted over where it shows (its cover), and masksMoved() compares that area
  const ruleMasks = [];
  const masked = new Map(); // frame -> [{index, box}]: the masked entries' covers, in their frame's viewport
  const maskTexts = [];
  walks.forEach((entries, frame) => {
    for (const entry of entries || []) {
      if (entry.mask) {
        const o = origins[frame];
        const shows = entry.cover || entry.box;
        ruleMasks.push({ frame, box: [shows[0] + o[0], shows[1] + o[1], shows[2], shows[3]] });
        if (!masked.has(frame)) masked.set(frame, []);
        masked.get(frame).push({ index: entry.index, box: shows });
        if (entry.text) maskTexts.push(entry.text);
      }
    }
  });
  const maskIds = Object.entries(adapted.joins).filter(([, join]) => join[2]).map(([id]) => id);
  const mainWalk = top;
  return {
    adapted, ruleMasks, maskIds, maskTexts, unreadable: [...new Set(unreadable)], frameBoxes,
    hash: sha256(text.join('\u0000')), mainWalk, frames, scrolls, masked,
  };
}

// R13: a masked element the snapshot has no node for (a block holding only text is folded into its parent's node)
// leaves its text in an element that holds it. That text goes from the innermost element joined to one of its
// ancestors, and from every element of its frame whose name, text, or locator holds it; each such element keeps its
// role and box and is marked redacted "mask". Its own node, when it has one, is masked by redactDescription.
function coverMaskedText(adapted, walks) {
  const idAt = new Map();
  for (const [id, [frame, index]] of Object.entries(adapted.joins)) idAt.set(`${frame}:${index}`, id);
  const byId = new Map(adapted.elements.map((e) => [e.id, e]));
  const flat = (text) => (typeof text === 'string' ? text.replace(/\s+/g, ' ').trim() : '');
  const cover = (e) => {
    if (e) Object.assign(e, { name: null, text: null, locator: null, redacted: 'mask' });
  };
  walks.forEach((entries, frame) => {
    for (const entry of entries || []) {
      if (!entry.mask) continue;
      if (!idAt.has(`${frame}:${entry.index}`)) {
        cover(byId.get((entry.within || []).map((i) => idAt.get(`${frame}:${i}`)).find(Boolean)));
      }
      const text = flat(entry.text);
      if (text.length < 4) continue;
      for (const e of adapted.elements) {
        if ((e.frame || 0) === frame && [e.name, e.text, e.locator].some((v) => flat(v).includes(text))) cover(e);
      }
    }
  });
}

function description(req, read, imageSha) {
  return {
    schema: SCHEMA, capture: req.id, kind: 'web', image_sha256: imageSha,
    engine: { playwright_core: req.pw.version, method: 'ariaSnapshotJSON(mode=ai, boxes) + style walk' },
    viewport: req.web.viewport, page: read.mainWalk.page, fold: req.web.viewport[1], coords: 'page-css-px',
    truncated: read.adapted.truncated, total: read.adapted.total, elements: read.adapted.elements,
    regions: [], outline: [], reading_order: [], interactive: [], primary_action: null,
  };
}

async function detect(req, port, page) {
  const target = page.url().split(/[?#]/)[0];
  const [command, ...rest] = req.impeccable.command;
  const args = [...rest, 'detect', '--cdp', String(port), '--target', target, '--json'];
  return new Promise((resolve) => {
    let stdout = '';
    let child;
    try {
      child = spawn(command, args, { cwd: req.cwd, env: process.env, stdio: ['ignore', 'pipe', 'ignore'] });
    } catch (_) {
      resolve({ skipped: 'not installed' });
      return;
    }
    const timer = setTimeout(() => child.kill('SIGKILL'), DETECT_TIMEOUT_MS);
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.on('error', (error) => {
      clearTimeout(timer);
      resolve({ skipped: error.code === 'ENOENT' ? 'not installed' : 'failed' });
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0 && code !== 2) {
        resolve({ skipped: code === null ? 'timed out' : `failed (exit ${code})` });
        return;
      }
      try {
        const findings = JSON.parse(stdout || '[]');
        resolve(Array.isArray(findings) ? { findings } : { skipped: 'unreadable output' });
      } catch (_) {
        resolve({ skipped: 'unreadable output' });
      }
    });
  });
}

// The recipe as the record keeps it: every string in it passes the redaction rules (a goto URL with a token, a fill
// value that is a key). A recipe that redaction changed cannot be replayed, so the record says it is not
// reproducible. -> {recipe, changed}
function recipeForRecord(recipe) {
  const out = JSON.parse(JSON.stringify(recipe || {}));
  const slots = [];
  const collect = (obj) => {
    for (const key of Object.keys(obj)) {
      if (typeof obj[key] === 'string') slots.push([obj, key]);
      else if (obj[key] && typeof obj[key] === 'object') collect(obj[key]);
    }
  };
  collect(out);
  const redacted = redact.redactStrings(slots.map(([obj, key]) => obj[key]));
  let changed = false;
  slots.forEach(([obj, key], i) => {
    if (redacted[i] !== obj[key]) {
      obj[key] = redacted[i];
      changed = true;
    }
  });
  return { recipe: out, changed };
}

function detectorPass(rule) {
  for (const [pass, rules] of Object.entries(DETECTOR_PASSES)) if (rules.includes(rule)) return pass;
  return 'hierarchy';
}

// Each signal's anchor names the description element at its region in its own frame (a signal without `frame` is
// the top page's), the one its selector names when several share the box, else the innermost.
function anchorElements(signals, desc) {
  const byBox = new Map();
  for (const e of desc.elements) {
    if (!e.box) continue;
    const key = `${e.frame || 0}:${e.box.join(',')}`;
    if (!byBox.has(key)) byBox.set(key, []);
    byBox.get(key).push(e);
  }
  for (const s of signals) {
    const region = s.anchor && Array.isArray(s.anchor.region) ? s.anchor.region.map(Math.round) : null;
    const found = region ? byBox.get(`${s.frame || 0}:${region.join(',')}`) || [] : [];
    const chosen = found.find((e) => e.selector && e.selector === s.anchor.selector) || found[found.length - 1] || null;
    s.anchor = { ...s.anchor, element: chosen ? chosen.id : null };
  }
  return signals;
}

// page -> {url}: the first top-level navigation of the captured page that guardNavigations() stopped
const STOPPED = new WeakMap();

// R17: without --remote, a top-level navigation to another host (a link, a form, a script, a popup) is stopped before
// its request is sent, as the crawler's guard does, and the captured page's first one fails the capture at the next
// stayOnThisMac(). Playwright does not route the hops of a redirect, so a redirect to another host is still followed
// and caught by stayOnThisMac() from where the page ended up.
async function guardNavigations(req, context) {
  if (req.remote) return;
  await context.route('**/*', (route) => {
    const request = route.request();
    let frame = null;
    try {
      frame = request.frame();
    } catch (_) {
      frame = null; // a worker's request is no navigation
    }
    if (!frame || frame.parentFrame() !== null || !request.isNavigationRequest() || isLocal(request.url()) !== false) {
      return route.fallback();
    }
    let page = null;
    try {
      page = frame.page();
    } catch (_) {
      page = null;
    }
    if (page && !STOPPED.has(page)) STOPPED.set(page, { url: request.url() });
    return route.abort('blockedbyclient');
  });
}

// R17: the page must still be on this Mac after it loads and after each step, since a redirect, a link, or a script
// can take it to another host; without --remote that fails the capture. A page on no host (about:blank, data:) passes.
function stayOnThisMac(req, page, when) {
  if (req.remote) return;
  const stopped = STOPPED.get(page);
  const url = stopped ? stopped.url : page.url();
  if (!stopped && isLocal(url) !== false) return;
  let host = url;
  try {
    host = new URL(url).host;
  } catch (_) {
    // isLocal parsed it
  }
  const what = stopped ? `tried to go to ${host} ${when}, which is not this Mac, so devkit stopped that request before ` +
    'it was sent. A page on another host is' : `went to ${host} ${when}, which is not this Mac, so it is`;
  throw new CaptureError('left-this-mac', 2, `The page ${what} captured only with --remote (and then only fixture or ` +
    'demo data). Nothing was recorded.');
}

// Step 1: the browser, its one context with the recipe's emulation, and the page. guard.browser is set at once, so
// the capture bound can close it.
async function openPage(req, deps, folder, guard) {
  const chromium = deps.chromium || require(req.pw.path).chromium;
  let launched;
  try {
    launched = await launchBrowser(chromium, folder, req.check && req.impeccable !== null);
  } catch (error) {
    if (error instanceof CaptureError) throw error;
    throw new CaptureError('launch', 1, `Chromium did not start: ${safeMessage(error)}. Install the pinned browser: ${FIX_INSTALL}.`);
  }
  guard.browser = launched.browser;
  const w = req.web;
  const context = await launched.browser.newContext({
    viewport: { width: w.viewport[0], height: w.viewport[1] }, deviceScaleFactor: w.dpr, colorScheme: w.theme,
    reducedMotion: 'reduce', locale: w.locale, timezoneId: w.timezone, forcedColors: w.forcedColors ? 'active' : 'none',
    contrast: w.contrast || 'no-preference', serviceWorkers: 'block',
    ...(w.storageState ? { storageState: w.storageState } : {}),
  });
  await guardNavigations(req, context);
  const page = await context.newPage();
  page.setDefaultTimeout(STEP_TIMEOUT_MS);
  if (w.clock) await page.clock.setFixedTime(new Date(w.clock));
  if (w.vision) {
    const session = await context.newCDPSession(page);
    await session.send('Emulation.setEmulatedVisionDeficiency', { type: w.vision });
  }
  return { browser: launched.browser, port: launched.port, context, page };
}

// Step 2: load the source, run the steps, add the --css rules; the page stays on this Mac throughout.
async function loadPage(req, page) {
  try {
    await page.goto(req.source.url, { waitUntil: 'load', timeout: LOAD_TIMEOUT_MS });
  } catch (error) {
    throw new CaptureError('load', 2, `The page did not load: ${safeMessage(error)}. Nothing was recorded.`);
  }
  stayOnThisMac(req, page, 'after it loaded');
  for (const s of req.steps) {
    await runStep(page, s);
    await page.waitForLoadState('load', { timeout: LOAD_TIMEOUT_MS }).catch(() => {});
    stayOnThisMac(req, page, `after ${s.shown}`);
  }
  if (req.web.css) {
    try {
      await page.addStyleTag({ content: req.web.css });
    } catch (error) {
      throw new CaptureError('css', 2, `The --css rules could not be added to the page (${safeMessage(error)}). Nothing was recorded.`);
    }
  }
}

// The end of step 2 and step 3: the isolated worlds, the fonts and two animation frames, the page still on this Mac,
// and the page box of the element to crop. -> {worlds, fontsReady, elementBox}
async function preparePage(req, context, page, at) {
  const worlds = await isolatedWorlds(context, page);
  const mainWorld = worlds.get(page.mainFrame());
  if (!mainWorld) throw new CaptureError('read', 2, 'The page could not be read in an isolated world. Nothing was recorded.');
  if (req.web.fullPage) await inWorld(mainWorld, revealLazy, BOUNDS.pageHeight, LAZY_WAIT_MS, LAZY_QUIET_MS);
  const fontsReady = await inWorld(mainWorld, settle);
  stayOnThisMac(req, page, 'before it was read');
  at(3);
  if (!req.element) return { worlds, fontsReady, elementBox: null };
  const found = await inWorld(mainWorld, elementBox, req.element);
  if (found.error) throw bad(`The element selector ${req.element} is not valid CSS. Nothing was recorded.`);
  if (!found.box) {
    throw new CaptureError('element', 3, `The element selector ${req.element} matches ${found.count} elements, not one. ` +
      'Nothing was recorded.');
  }
  return { worlds, fontsReady, elementBox: found.box };
}

// Step 6: the screenshot with the masks painted #000000 (the mask rules' locator in every frame, which reaches open
// shadow roots, and the elements a secret was found in), then each masked box checked in the picture and painted over
// where it shows through; a frame Playwright could not mask is painted whole. -> {img, png, wholeFrames}
async function screenshot(req, page, read, redacted, maskSelector, truncated) {
  const w = req.web;
  const masks = page.frames().map((frame) => frame.locator(maskSelector));
  for (const mask of redacted.masks) {
    const ref = read.maskIds.includes(mask.element) ? null : read.adapted.refs[mask.element];
    if (ref) masks.push(page.getByRef(ref));
  }
  const clip = truncated ? { clip: { x: 0, y: 0, width: read.mainWalk.page[0], height: BOUNDS.pageHeight } } : {};
  const shot = await page.screenshot({ fullPage: w.fullPage, animations: 'disabled', caret: 'hide', mask: masks,
    maskColor: MASK_COLOR, type: 'png', scale: 'device', timeout: LOAD_TIMEOUT_MS, ...clip });
  const img = decodePng(shot);
  let painted = false;
  let wholeFrames = 0;
  const paintWhole = new Set(read.unreadable);
  const boxes = [...read.ruleMasks.map((m) => ({ frame: m.frame, box: m.box })),
    ...redacted.masks.filter((m) => m.box).map((m) => ({ frame: m.frame === null ? 0 : m.frame, box: m.box }))];
  for (const m of boxes) {
    if (isPainted(img, m.box, w.dpr)) continue;
    if (m.frame !== 0 && read.frameBoxes.has(m.frame)) {
      paintWhole.add(m.frame);
    } else {
      paint(img, m.box, w.dpr);
      painted = true;
    }
  }
  for (const frame of paintWhole) {
    const box = read.frameBoxes.get(frame);
    if (!box) continue;
    paint(img, box, w.dpr);
    painted = true;
    wholeFrames += 1;
  }
  return { img, png: painted ? encodePng(img) : shot, wholeFrames };
}

// Whether a masked element moved after the read (a late layout shift, or a page that moves it when the screenshot
// starts): its box now against the box the read painted, both with their frame's scroll.
async function masksMoved(read, worlds) {
  for (const [frame, entries] of read.masked) {
    const world = worlds.get(read.frames[frame]);
    if (!world) continue;
    let now;
    try {
      now = await inWorld(world, maskBoxes, entries.map((e) => e.index));
    } catch (_) {
      continue; // a frame that went away shows nothing
    }
    const then = read.scrolls[frame] || [0, 0];
    const moved = entries.some((e, k) => {
      const b = now.boxes[k];
      if (!b) return false;
      const was = [e.box[0] + then[0], e.box[1] + then[1], e.box[2], e.box[3]];
      const is = [b[0] + now.scroll[0], b[1] + now.scroll[1], b[2], b[3]];
      return was.some((v, i) => Math.abs(v - is[i]) >= 1);
    });
    if (moved) return true;
  }
  return false;
}

// The page's text after the screenshot against the read's hash -> {changed, rule}: rule is the first secret the
// changed text holds, or null.
async function textAfter(page, worlds, hash) {
  const again = [];
  for (const frame of page.frames()) {
    const world = worlds.get(frame);
    if (!world) continue;
    try {
      again.push(await inWorld(world, pageText));
    } catch (_) {
      // a frame that went away
    }
  }
  if (sha256(again.join('\u0000')) === hash) return { changed: false, rule: null };
  const hits = redact.scan(again);
  return { changed: true, rule: hits.length ? hits[0].rule : null };
}

// Steps 4 to 7: read the page, redact, take the screenshot, then read the masks' boxes and the text again. A mask
// that moved, or text that changed and holds a secret, takes the capture again once (after a text change in new
// isolated worlds); the second time the capture fails. -> {read, redacted, image, truncated, textChanged, retried,
// worlds}
async function readAndShoot(req, context, page, firstWorlds, at) {
  const w = req.web;
  const maskSelector = [...MASK_RULES, ...req.mask].join(', ');
  let worlds = firstWorlds;
  for (let attempt = 0; ; attempt += 1) {
    at(4);
    const read = await readPage(page, worlds, req, maskSelector);
    if (read.mainWalk.viewport[0] !== w.viewport[0]) {
      at(3);
      throw new CaptureError('width', 3, `The page is ${read.mainWalk.viewport[0]} px wide, not ${w.viewport[0]}. ` +
        'Nothing was recorded.');
    }
    const truncated = w.fullPage && read.mainWalk.page[1] > BOUNDS.pageHeight;
    at(5);
    const redacted = redact.redactDescription(description(req, read, null), read.adapted.values, { mask: read.maskIds });
    at(6);
    const image = await screenshot(req, page, read, redacted, maskSelector, truncated);
    stayOnThisMac(req, page, 'while it was captured');
    at(7);
    const moved = await masksMoved(read, worlds);
    const text = moved ? { changed: false, rule: null } : await textAfter(page, worlds, read.hash);
    if (!moved && !text.rule) {
      return { read, redacted, image, truncated, textChanged: text.changed, retried: attempt > 0, worlds };
    }
    if (attempt === 1 && moved) {
      throw new CaptureError('mask-moved', 7, 'A masked element moved while the picture was taken, and again when it ' +
        'was taken a second time, so the masks may not cover it. Nothing was recorded. Capture a state that stays ' +
        'still: add a wait step for what the page shows last.');
    }
    if (attempt === 1) {
      throw new CaptureError('text-changed', 7, `The page's text changed after the screenshot twice, and it holds a ` +
        `secret (${text.rule}) the masks may not cover. Nothing was recorded. Capture a state that stays still: ` +
        'add a wait step for what the page shows last.');
    }
    if (!moved) worlds = await isolatedWorlds(context, page);
  }
}

// The stand-in detector's findings as signals, each anchored on the region its selector names.
async function detectorSignals(req, port, page, world, ran, skipped) {
  const result = await detect(req, port, page);
  if (result.skipped) {
    skipped.impeccable = result.skipped;
    return [];
  }
  ran.push('impeccable');
  const findings = result.findings.filter((f) => f && typeof f === 'object');
  const regions = await inWorld(world, boxesOf, findings.map((f) => (typeof f.selector === 'string' ? f.selector : '')));
  return findings.map((f, i) => {
    const rule = typeof f.antipattern === 'string' ? f.antipattern : 'unknown';
    return { check: 'impeccable', pass: detectorPass(rule), rule: [`impeccable:${rule}`],
      anchor: { region: regions[i], selector: typeof f.selector === 'string' ? f.selector : null },
      measured: typeof f.snippet === 'string' ? f.snippet : null, threshold: null };
  });
}

// Step 8: the checks on the same page state, each in an isolated world: checks.js, axe in each frame, then the
// detector as the last check. off-scale-spacing runs only with the project's spacing scale (req.spacing, DESIGN.md's
// tokens); without one the record says it did not run. -> {signals, ran, skipped}
async function runChecks(req, page, worlds, port) {
  const ran = [];
  const skipped = {};
  let signals = [];
  if (!req.check) return { signals, ran, skipped };
  const world = worlds.get(page.mainFrame());
  const { runChecks: devkitChecks } = require('./checks.js');
  try {
    // the request's mask selectors (the project's visual.mask and the command's --mask): checks.js looks at nothing
    // masked, and redactSignals keeps any masked text out of the signals that remain
    signals = signals.concat(await inWorld(world, devkitChecks, req.web.viewport[0], { spacing: req.spacing,
      mask: req.mask }));
    ran.push('devkit');
    if (!req.spacing) skipped['off-scale-spacing'] = 'no spacing scale';
  } catch (error) {
    skipped.devkit = `failed: ${safeMessage(error)}`;
  }
  signals = signals.concat(await require('./axe.cjs').run(page, { worlds, ran, skipped }));
  if (req.impeccable === null) skipped.impeccable = 'not installed';
  else signals = signals.concat(await detectorSignals(req, port, page, world, ran, skipped));
  return { signals, ran, skipped };
}

// The source as the record keeps it: the URL through the controls, the whole secret scan, and the URL rules
// (redact.cjs's redactSourceUrl), with the route of the URL kept. A URL redaction changed cannot be loaded again.
function recordSource(req) {
  const source = redact.redactSourceUrl(req.source.url);
  let route = null;
  try {
    route = req.source.local && req.source.url.startsWith('file:') ? null : redact.routePattern(new URL(source.url).pathname);
  } catch (_) {
    route = null; // a URL redacted whole has no route
  }
  return { url: source.url, origin: req.source.local ? 'local' : 'remote', route, params: source.params,
    changed: source.changed };
}

// Redacted signals folded (fold.js) and numbered from 1, each untriaged.
function number(signals, desc) {
  return require('./fold.js').fold(signals, desc).map(({ duplicates, ...s }, i) => ({ n: i + 1, ...s, triage: null, duplicates }));
}

// Step 10: the signals redacted (those below a cut picture left out), folded, and numbered; the record; the crop.
// -> {record, image, crop, description}
function webRecord(req, ready, shot, checks) {
  const w = req.web;
  const { read, redacted, image, truncated } = shot;
  const desc = redacted.description;
  desc.image_sha256 = sha256(image.png);
  const shown = truncated ? checks.signals.filter((s) => !(s.anchor && Array.isArray(s.anchor.region) &&
    s.anchor.region[1] >= BOUNDS.pageHeight)) : checks.signals;
  // R13: no signal keeps what a masked element shows, whichever check or the detector found it
  const masked = { ids: read.maskIds, texts: read.maskTexts, boxes: read.ruleMasks.map((m) => m.box) };
  const numbered = number(redact.redactSignals(anchorElements(shown, desc), desc, masked), desc);
  const source = recordSource(req);
  const secretMasked = redacted.masks.filter((m) => !read.maskIds.includes(m.element)).length;
  const kept = recipeForRecord(req.recipe);
  const record = {
    kind: 'web', state: req.state, source: { url: source.url, origin: source.origin, route: source.route },
    element: null, recipe: kept.recipe, reproducible: !kept.changed && !source.changed, experiment: w.css !== null,
    again_of: null,
    env: { browser: req.pw.revision ? `chromium ${req.pw.revision}` : 'chromium', playwright_cli: req.pw.cli,
      playwright_core: req.pw.version, axe: checks.ran.includes('axe') ? require('./axe.cjs').VERSION : null,
      impeccable: req.impeccable ? req.impeccable.version : null, fonts_ready: ready.fontsReady },
    image: { sha256: desc.image_sha256, width: image.img.width, height: image.img.height, fold: w.viewport[1],
      scale: w.dpr, truncated, page_height: read.mainWalk.page[1] },
    description: null,
    redaction: {
      masked: read.ruleMasks.length + secretMasked + image.wholeFrames, values_dropped: desc.redaction.values_dropped,
      secret_hits: desc.redaction.secret_hits, value_secret_hits: desc.redaction.value_secret_hits,
      url_params: source.params, controls_stripped: desc.redaction.controls_stripped,
      frames_masked_whole: image.wholeFrames, text_changed: shot.textChanged, retried: shot.retried,
    },
    checks: { ran: checks.ran, skipped: checks.skipped, signals: numbered },
  };
  let cropped = null;
  if (ready.elementBox) {
    const c = crop(image.img, ready.elementBox, w.dpr);
    if (!c) {
      throw new CaptureError('element', 10, `The element ${req.element} lies outside the picture, so it cannot be ` +
        'cropped. Nothing was recorded. Capture the full page, or scroll it into view first.');
    }
    cropped = encodePng(c);
    record.element = { selector: req.element, box: ready.elementBox, sha256: sha256(cropped) };
  }
  return { record, image: image.png, crop: cropped, description: desc };
}

// Steps 1 to 10 of a web capture; guard.step follows the step reached, for the capture bound's message.
async function captureWeb(req, deps, folder, guard = {}) {
  let step = 1;
  const at = (n) => {
    step = n;
    guard.step = n;
  };
  at(1);
  try {
    const opened = await openPage(req, deps, folder, guard);
    at(2);
    await loadPage(req, opened.page);
    const ready = await preparePage(req, opened.context, opened.page, at);
    const shot = await readAndShoot(req, opened.context, opened.page, ready.worlds, at);
    at(8);
    const checks = await runChecks(req, opened.page, shot.worlds, opened.port);
    // The description's derived parts, and the non-semantic-control signal read in the live page (describe.js)
    const session = req.check ? await opened.context.newCDPSession(opened.page) : null;
    const described = await require('./describe.js').describe(opened.page, session, shot.redacted.description, { check: req.check });
    checks.signals.push(...described.signals);
    if (described.skipped) checks.skipped['non-semantic-control'] = described.skipped;
    at(9);
    await opened.browser.close();
    at(10);
    return webRecord(req, ready, shot, checks);
  } catch (error) {
    if (error instanceof CaptureError || error instanceof redact.RedactionError) {
      if (error instanceof redact.RedactionError) error.step = error.step || step;
      throw error;
    }
    throw new CaptureError('failed', step, `The capture failed at step ${step} (${STEP_NAMES[step]}): ${safeMessage(error)}. ` +
      'Nothing was recorded.');
  } finally {
    // a capture that stopped before step 9 closed the browser closes it here
    if (guard.browser && step < 10) await guard.browser.close().catch(() => {});
  }
}

// --------------------------------------------------------------------------- PDF pages

// Runs in osascript's JavaScript: renders one page into an RGBA bitmap at the scale it is given and returns it as
// base64 PNG (no file), with the page count, the page size in points, and each text line with its box (points, from
// the bottom left).
const PDF_SCRIPT = `
ObjC.import('Foundation'); ObjC.import('AppKit'); ObjC.import('PDFKit');
function run(argv) {
  const doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  if (!doc || doc.isNil()) return JSON.stringify({error: 'unreadable'});
  const count = doc.pageCount, index = parseInt(argv[1], 10);
  if (index < 0 || index >= count) return JSON.stringify({pages: count});
  const page = doc.pageAtIndex(index), bounds = page.boundsForBox(0);
  const W = bounds.size.width, H = bounds.size.height;
  if (argv[2] === 'size') return JSON.stringify({pages: count, size: [W, H]});
  const scale = parseFloat(argv[2]), w = Math.round(W * scale), h = Math.round(H * scale);
  const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
    null, w, h, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
  const ctx = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.setCurrentContext(ctx);
  $.NSColor.whiteColor.setFill;
  $.NSRectFill($.NSMakeRect(0, 0, w, h));
  $.CGContextScaleCTM(ctx.CGContext, scale, scale);
  page.drawWithBoxToContext(0, ctx.CGContext);
  ctx.flushGraphics;
  $.NSGraphicsContext.restoreGraphicsState;
  const png = rep.representationUsingTypeProperties(4, $());
  const lines = [];
  const all = page.selectionForRect(bounds);
  if (all && !all.isNil()) {
    const list = all.selectionsByLine;
    for (let i = 0; i < list.count; i++) {
      const s = list.objectAtIndex(i), b = s.boundsForPage(page);
      lines.push({text: ObjC.unwrap(s.string), box: [b.origin.x, b.origin.y, b.size.width, b.size.height]});
    }
  }
  return JSON.stringify({pages: count, size: [W, H], lines, png: ObjC.unwrap(png.base64EncodedStringWithOptions(0))});
}`;

function pdfkit(file, index, scaleOrSize) {
  const run = spawnSync('osascript', ['-l', 'JavaScript', '-e', PDF_SCRIPT, file, String(index), String(scaleOrSize)],
    { encoding: 'utf8', maxBuffer: 512 * 1024 * 1024, timeout: LOAD_TIMEOUT_MS });
  if (run.error && run.error.code === 'ENOENT') {
    throw new CaptureError('no-pdfkit', 2, 'PDF pages render with PDFKit through osascript, which only macOS has. ' +
      'Nothing was recorded.');
  }
  let out = null;
  try {
    out = JSON.parse(run.stdout);
  } catch (_) {
    out = null;
  }
  if (run.status !== 0 || !out || out.error) {
    throw new CaptureError('pdf', 2, `PDFKit could not read ${path.basename(file)}. Nothing was recorded.`);
  }
  return out;
}

function capturePdf(req) {
  const file = req.source.file;
  let bytes;
  try {
    bytes = fs.readFileSync(file);
  } catch (_) {
    throw bad(`${file} cannot be read. Nothing was recorded.`);
  }
  const index = req.source.page - 1;
  const first = pdfkit(file, index, 'size');
  if (!first.size) {
    throw new CaptureError('page', 2, `${path.basename(file)} has ${first.pages} pages, so there is no page ${req.source.page}. ` +
      'Nothing was recorded.');
  }
  const [W, H] = first.size;
  const scale = (W > H ? 1920 : 1240) / W;
  const out = pdfkit(file, index, scale);
  const img = decodePng(Buffer.from(out.png, 'base64'));
  const elements = (out.lines || []).map((line, i) => {
    const [x, y, w, h] = line.box;
    const x0 = Math.floor(x * scale);
    const y0 = Math.floor((H - y - h) * scale);
    const box = [x0, y0, Math.ceil((x + w) * scale) - x0, Math.ceil((H - y) * scale) - y0];
    return { id: `E${i + 1}`, parent: null, frame: null, role: 'text', name: null, text: line.text, states: {}, box,
      locator: null, selector: null, url: null, placeholder: null, cursor: null, style: null, above_fold: true,
      component: null, redacted: null };
  });
  const desc = {
    schema: SCHEMA, capture: req.id, kind: 'document', image_sha256: null,
    engine: { method: 'PDFKit text lines' }, viewport: [img.width, img.height], page: [img.width, img.height],
    fold: img.height, coords: 'page-px', truncated: false, total: elements.length, elements,
    regions: [], outline: [], reading_order: elements.map((e) => e.id), interactive: [], primary_action: null,
  };
  const redacted = redact.redactDescription(desc, {}, { mask: [] });
  for (const mask of redacted.masks) if (mask.box) paint(img, mask.box, 1);
  const png = redacted.masks.length ? encodePng(img) : Buffer.from(out.png, 'base64');
  const d = redacted.description;
  d.image_sha256 = sha256(png);
  const kept = recipeForRecord({ ...req.recipe, page: req.source.page, converter: null, scale });
  const record = {
    kind: 'document', state: req.state,
    source: { file: path.basename(file), sha256: sha256(bytes), page: req.source.page },
    element: null, recipe: kept.recipe,
    reproducible: !kept.changed, experiment: false, again_of: null,
    env: { renderer: 'PDFKit (osascript)', fonts_ready: true },
    image: { sha256: d.image_sha256, width: img.width, height: img.height, fold: img.height, scale, truncated: false,
      page_height: img.height },
    description: null,
    redaction: { masked: redacted.masks.length, values_dropped: 0, secret_hits: d.redaction.secret_hits,
      value_secret_hits: 0, url_params: [], controls_stripped: d.redaction.controls_stripped, frames_masked_whole: 0,
      text_changed: false, retried: false },
    checks: { ran: [], skipped: {}, signals: [] },
  };
  if (req.check) {
    const found = require('./doccheck.js').check(d, { size: [img.width, img.height], scale, image: img });
    record.checks = { ran: ['devkit'], skipped: {}, signals: number(redact.redactSignals(found, d), d) };
  }
  return { record, image: png, crop: null, description: d };
}

// --------------------------------------------------------------------------- the store

function ensureFolder(folder) {
  if (fs.existsSync(folder)) return;
  ensureFolder(path.dirname(folder));
  fs.mkdirSync(folder, { mode: 0o700 });
  fs.chmodSync(folder, 0o700);
}

function writeAtomic(folder, name, bytes) {
  ensureFolder(folder);
  const final = path.join(folder, name);
  const tmp = path.join(folder, `.${name}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`);
  fs.writeFileSync(tmp, bytes, { mode: 0o600, flag: 'wx' });
  fs.chmodSync(tmp, 0o600);
  fs.renameSync(tmp, final);
}

function writeStore(store, result) {
  const descBytes = zlib.gzipSync(Buffer.from(JSON.stringify(result.description)), { level: 9 });
  const descSha = sha256(descBytes);
  writeAtomic(path.join(store, 'blobs'), `${result.record.image.sha256}.png`, result.image);
  if (result.crop) writeAtomic(path.join(store, 'blobs'), `${result.record.element.sha256}.png`, result.crop);
  writeAtomic(path.join(store, 'desc'), `${descSha}.json.gz`, descBytes);
  result.record.description = { sha256: descSha, schema: SCHEMA, elements: result.description.elements.length,
    truncated: result.description.truncated };
  return result.record;
}

// --------------------------------------------------------------------------- run

// The web capture within BOUNDS.captureSeconds: at the bound the browser is closed, which ends what the capture was
// waiting for, and the capture fails with the bound's message, whatever the closed browser made it fail with.
async function withinBound(req, deps, folder) {
  const guard = { browser: null, step: 1, expired: false };
  const seconds = BOUNDS.captureSeconds;
  const expired = () => new CaptureError('time-bound', guard.step, `The capture took longer than ${seconds} s, the ` +
    `bound for one capture with its steps and checks, and stopped at step ${guard.step} (${STEP_NAMES[guard.step]}). ` +
    'Nothing was recorded. Capture a state that is reached sooner: fewer steps, or a wait for what the page shows ' +
    'instead of a fixed time.');
  let timer = null;
  const bound = new Promise((resolve) => {
    timer = setTimeout(() => {
      guard.expired = true;
      const closed = guard.browser ? guard.browser.close().catch(() => {}) : Promise.resolve();
      Promise.race([closed, new Promise((r) => setTimeout(r, 5000).unref())]).then(resolve);
    }, seconds * 1000);
  });
  try {
    const result = await Promise.race([captureWeb(req, deps, folder, guard), bound.then(() => { throw expired(); })]);
    if (guard.expired) throw expired();
    return result;
  } catch (error) {
    throw guard.expired ? expired() : error;
  } finally {
    clearTimeout(timer);
  }
}

async function capture(request, deps = {}) {
  const req = normalize(request);
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'devkit-capture-'));
  fs.chmodSync(folder, 0o700);
  const tmpdir = process.env.TMPDIR;
  process.env.TMPDIR = folder;
  try {
    if (req.kind === 'document') return capturePdf(req);
    req.pw = playwrightCore(req);
    return await withinBound(req, deps, folder);
  } finally {
    if (tmpdir === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = tmpdir;
    // a browser closed a moment ago may still be removing its profile: retry what is not empty yet
    fs.rmSync(folder, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  }
}

async function run(request, deps = {}) {
  const req = normalize(request);
  if (!req.store) throw bad('The request names no store folder (request.store).');
  const result = await capture(request, deps);
  try {
    return writeStore(req.store, result);
  } catch (error) {
    throw new CaptureError('store', 11, `The store could not be written (${safeMessage(error)}). Check ${req.store}.`);
  }
}

async function main(argv) {
  if (argv.length === 1 && (argv[0] === '--help' || argv[0] === '-h')) {
    process.stdout.write(USAGE);
    return 0;
  }
  if (argv.length !== 1 || argv[0] !== '--json') {
    process.stderr.write(USAGE);
    return 2;
  }
  let request = null;
  try {
    request = JSON.parse(fs.readFileSync(0, 'utf8'));
  } catch (_) {
    process.stderr.write('✗ capture.cjs --json takes a capture request as JSON on stdin (see --help).\n');
    return 2;
  }
  try {
    const record = await run(request);
    process.stdout.write(`${JSON.stringify(record)}\n`);
    return 0;
  } catch (error) {
    if (error instanceof CaptureError || error instanceof redact.RedactionError) {
      process.stderr.write(`✗ ${error.message}\n`);
      return error.code === 'bad-request' || error.code === 'bad-input' ? 2 : 1;
    }
    process.stderr.write(`✗ The capture failed: ${safeMessage(error)}. Nothing was recorded.\n`);
    return 1;
  }
}

module.exports = { run, capture, adapt, anchorElements, normalize, CaptureError, runStep, playwrightCore, BOUNDS };

if (require.main === module) main(process.argv.slice(2)).then((code) => { process.exitCode = code; });
