// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// The axe accessibility rules in a capture (R30). The capture driver (capture.cjs) calls run() on the page state it
// captured, after checks.js and before the detector and the focus walk, with the isolated JavaScript worlds it read
// the page in:
//
//   1. Read axe.min.js of the pinned axe-core (config/npm-globals.txt) from the global npm folder (`npm root -g`, as
//      the driver finds @playwright/cli; never a download) once into one buffer, check that buffer's SHA-256 against
//      the pin, and inject that same buffer, so the file cannot change between the check and the injection. A file
//      that is missing or does not match is never injected: the capture records why in checks.skipped.axe.
//   2. In each frame that has an isolated world (the page's own scripts cannot reach it, so they cannot replace what
//      axe reads; a CDP evaluation also works under a strict CSP and leaves it in force), inject axe and run it on
//      that frame's document with the wcag2a, wcag2aa, wcag21a, wcag21aa and wcag22aa tags (wcag22aa turns on
//      target-size, which axe ships off), violations and incomplete results, and `iframes: false`, so each frame's
//      results are counted once. In a child frame the rules axe runs on the top document only stay off.
//   3. Turn each violation node into a signal of the shape checks.js gives: check `axe`, the axe rule id, the pass
//      by criterion (passFor: 1.4.4 and 1.4.10 responsive, 1.4.12 typography, other 1.4.x color, 2.5.8 and the
//      others accessibility), `wcag:<criterion>` from each `wcag<nnn>` tag (wcag1410 is 1.4.10), or `check:axe` when
//      the rule has none; the anchor is the element's border box in page CSS px
//      (a frame's elements are moved by the frame's content box) and devkit's own selector for it, built like
//      checks.js builds it. A signal found in a child frame carries `frame`, the frame's index in page.frames(), the
//      number the capture's description gives the elements of that frame, so the driver can name the element there
//      (its selector belongs to the frame's own document). An incomplete node, one axe could not decide, is a review
//      signal (`advice: true`).
//      `measured` and `threshold` are built here from axe's numbers only (a contrast ratio, a target's size and its
//      safe clickable space); axe's html, failureSummary, target and check messages never leave the page.
//   4. Pass every signal through redact.cjs (R27) before returning it.
//
// Interface:
//
//   run(page, opts) -> Signal[]           not numbered, not folded
//     opts.worlds   Map(frame -> {session, contextId, frameId, parentSession}): the capture's isolated worlds
//     opts.ran      a list: `axe` is added when the rules ran
//     opts.skipped  an object: `axe` is set to "not installed", "hash mismatch", or "failed: <why>" when they did not
//     opts.file     another axe.min.js to read instead of the global one (it must still match the pin)
//   passFor(criteria) -> pass             the pass of a rule with these WCAG criteria (["1.4.4"] -> "responsive")
//   VERSION, SHA256, TAGS
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const npmroot = require('./npmroot.cjs');
const redact = require('./redact.cjs');

const VERSION = '4.14.0';
const SHA256 = '20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878'; // axe.min.js of axe-core 4.14.0
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];
// The rules of these tags that axe itself runs on the top document only (their `matches` is is-initiator-matches,
// and bypass counts the page as a whole); a child frame run on its own would otherwise take itself for the page.
const TOP_ONLY = ['aria-hidden-body', 'bypass', 'document-title', 'html-has-lang', 'meta-viewport'];
const URLS = /[A-Za-z][A-Za-z0-9+.-]*:\/\/[^\s"'<>]+/g;
// The passes of the 1.4.x criteria that are not color: resize text and reflow are responsive, text spacing is
// typography. Every other 1.4.x criterion reports to color, and every criterion outside 1.4 to accessibility.
const PASS_OF = { '1.4.4': 'responsive', '1.4.10': 'responsive', '1.4.12': 'typography' };

function passOf(criterion) {
  return PASS_OF[criterion] || (criterion.startsWith('1.4.') ? 'color' : 'accessibility');
}

// The pass of a rule with these criteria: the pass of its first criterion that is not accessibility, so a rule that
// also names a 1.4.x criterion reports where that criterion is judged (no rule of axe 4.14.0 names two such criteria).
function passFor(criteria) {
  for (const c of criteria) {
    const pass = passOf(String(c));
    if (pass !== 'accessibility') return pass;
  }
  return 'accessibility';
}

function axeFile(opts) {
  if (typeof opts.file === 'string' && opts.file) return path.resolve(opts.file);
  const { root } = npmroot.globalRoot(); // the folder the driver found Playwright in, asked once per process
  return root ? path.join(root, 'axe-core', 'axe.min.js') : null;
}

// The file, read once and checked against the pin. -> {source} or {skipped}
function load(file) {
  if (!file) return { skipped: 'not installed' };
  let bytes;
  try {
    bytes = fs.readFileSync(file);
  } catch (_) {
    return { skipped: 'not installed' };
  }
  if (crypto.createHash('sha256').update(bytes).digest('hex') !== SHA256) return { skipped: 'hash mismatch' };
  return { source: bytes.toString('utf8') };
}

// The first line of an error, with control characters removed and the URLs in it redacted.
function safeMessage(error) {
  const first = redact.strip(String((error && error.message) || error).split('\n')[0]).text;
  return first.replace(URLS, (url) => redact.redactUrl(url).url).slice(0, 200);
}

function frameOrder(page) {
  const order = [];
  const visit = (frame) => {
    order.push(frame);
    for (const child of frame.childFrames()) visit(child);
  };
  visit(page.mainFrame());
  return order;
}

async function evaluateIn(world, expression) {
  const result = await world.session.send('Runtime.evaluate', { expression, contextId: world.contextId, silent: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'axe could not be injected');
}

async function callIn(world, fn, ...args) {
  const result = await world.session.send('Runtime.callFunctionOn', {
    functionDeclaration: fn.toString(), executionContextId: world.contextId, arguments: args.map((value) => ({ value })),
    returnByValue: true, awaitPromise: true,
  });
  if (result.exceptionDetails) {
    const ex = result.exceptionDetails.exception;
    throw new Error((ex && ex.description) || result.exceptionDetails.text || 'axe failed in the page');
  }
  return result.result.value;
}

// Where a child frame's viewport starts in the top frame's viewport: each frame element's box plus its border and
// padding, read in its parent's isolated world, up to the top frame. -> [x, y], or null when a world is missing
async function frameOrigin(frame, worlds) {
  let x = 0;
  let y = 0;
  for (let f = frame; f.parentFrame(); f = f.parentFrame()) {
    const world = worlds.get(f);
    const parent = worlds.get(f.parentFrame());
    if (!world || !parent || !world.parentSession) return null;
    const { backendNodeId } = await world.parentSession.send('DOM.getFrameOwner', { frameId: world.frameId });
    const { object } = await world.parentSession.send('DOM.resolveNode', { backendNodeId,
      executionContextId: parent.contextId });
    const result = await world.parentSession.send('Runtime.callFunctionOn', {
      objectId: object.objectId, returnByValue: true,
      functionDeclaration: 'function () { const b = this.getBoundingClientRect(), s = getComputedStyle(this); ' +
        'return [b.left + this.clientLeft + parseFloat(s.paddingLeft), b.top + this.clientTop + parseFloat(s.paddingTop)]; }',
    });
    x += result.result.value[0];
    y += result.result.value[1];
  }
  return [x, y];
}

// --------------------------------------------------------------------------- in the page (isolated world)

// Runs the injected axe on this frame's document and returns its findings in the signal shape, with `criteria` (the
// WCAG criteria of the rule's tags, which run() turns into rule references and a pass), `box` as [left, top, right,
// bottom] (page CSS px in the top frame, this frame's viewport px in a child frame), and `review` for an incomplete
// node. Self-contained: it is sent as source. It builds selectors the way checks.js does (the test id, the id, then
// a path, devkit's order), so a signal of each on one element carries one selector.
async function axeSignals(tags, top, off) {
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch { return false; } };
  const selector = (el) => {
    const testid = el.getAttribute('data-testid');
    if (testid !== null && unique(`[data-testid="${CSS.escape(testid)}"]`)) return `[data-testid="${CSS.escape(testid)}"]`;
    if (el.id && unique(`#${CSS.escape(el.id)}`)) return `#${CSS.escape(el.id)}`;
    const steps = [];
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (node !== el && node.id && unique(`#${CSS.escape(node.id)}`)) {
        steps.unshift(`#${CSS.escape(node.id)}`);
        break;
      }
      const tag = node.localName;
      const same = node.parentElement ? Array.from(node.parentElement.children).filter((c) => c.localName === tag) : [node];
      steps.unshift(same.length > 1 ? `${CSS.escape(tag)}:nth-of-type(${same.indexOf(node) + 1})` : CSS.escape(tag));
    }
    return steps.join(' > ');
  };
  const num = (v) => typeof v === 'number' && Number.isFinite(v);
  const fmt = (v) => String(Math.round(v * 100) / 100);
  const KEY = /^[A-Za-z][A-Za-z0-9-]{0,40}$/;
  const RATIO = /^\d{1,2}(\.\d{1,2})?:1$/;
  // measured and threshold from the numbers of the node's checks: a contrast ratio, a target's size and its safe
  // clickable space; an undecided node says why with axe's message key
  const measure = (node, review) => {
    const data = [...node.any, ...node.all, ...node.none].map((c) => c && c.data).filter((d) => d && typeof d === 'object');
    const contrast = data.find((d) => num(d.contrastRatio) || RATIO.test(String(d.expectedContrastRatio)));
    const size = data.find((d) => num(d.minSize) && num(d.width) && num(d.height));
    const space = data.find((d) => num(d.closestOffset) && num(d.minOffset));
    let measured = null;
    let threshold = null;
    if (contrast) {
      // a ratio of 0 is one axe could not measure (text on an image or a gradient): only the threshold holds
      measured = num(contrast.contrastRatio) && contrast.contrastRatio > 0 ? `${fmt(contrast.contrastRatio)}:1` : null;
      threshold = RATIO.test(String(contrast.expectedContrastRatio)) ? String(contrast.expectedContrastRatio) : null;
    } else if (size) {
      measured = `${fmt(size.width)}×${fmt(size.height)} px`;
      threshold = `${fmt(size.minSize)}×${fmt(size.minSize)} px`;
      if (space) {
        measured += `, ${fmt(space.closestOffset)} px safe clickable space`;
        threshold += `, or ${fmt(space.minOffset)} px safe clickable space`;
      }
    }
    if (review) {
      const key = data.map((d) => d.messageKey).find((k) => typeof k === 'string' && KEY.test(k));
      measured = `needs review${key ? ` (${key})` : ''}${measured ? `: ${measured}` : ''}`;
    }
    return { measured, threshold };
  };

  const options = { runOnly: { type: 'tag', values: tags }, resultTypes: ['violations', 'incomplete'], iframes: false,
    elementRef: true, selectors: false, restoreScroll: true };
  if (off.length) options.rules = Object.fromEntries(off.map((id) => [id, { enabled: false }]));
  const results = await axe.run(document, options);
  const dx = top ? scrollX : 0;
  const dy = top ? scrollY : 0;
  const out = [];
  for (const [list, review] of [[results.violations, false], [results.incomplete, true]]) {
    for (const rule of list) {
      const criteria = [];
      for (const tag of rule.tags) {
        const m = /^wcag(\d)(\d)(\d{1,2})$/.exec(tag);
        if (m && !criteria.includes(`${m[1]}.${m[2]}.${m[3]}`)) criteria.push(`${m[1]}.${m[2]}.${m[3]}`);
      }
      for (const node of rule.nodes) {
        const el = node.element;
        if (!el || el.nodeType !== 1) continue;
        const r = el.getBoundingClientRect();
        out.push({ check: 'axe', axe_rule: rule.id, criteria, box: [r.left + dx, r.top + dy, r.right + dx, r.bottom + dy],
          selector: selector(el), ...measure(node, review), review });
      }
    }
  }
  return out;
}

// --------------------------------------------------------------------------- run

async function run(page, opts = {}) {
  if (!opts.worlds || typeof opts.worlds.get !== 'function') throw new TypeError('axe.run takes the capture\'s isolated worlds.');
  const ran = Array.isArray(opts.ran) ? opts.ran : [];
  const skipped = opts.skipped && typeof opts.skipped === 'object' ? opts.skipped : {};
  const file = load(axeFile(opts));
  if (file.skipped) {
    skipped.axe = file.skipped;
    return [];
  }
  const main = page.mainFrame();
  const mainWorld = opts.worlds.get(main);
  if (!mainWorld) {
    skipped.axe = 'failed: the page has no isolated world';
    return [];
  }
  const signals = [];
  const numbers = new Map(page.frames().map((f, i) => [f, i])); // as the description numbers the frames
  for (const frame of frameOrder(page)) {
    const world = opts.worlds.get(frame); // a frame without one is painted over whole in the picture
    if (!world) continue;
    const top = frame === main;
    let found;
    let at = [0, 0];
    try {
      await evaluateIn(world, file.source);
      found = await callIn(world, axeSignals, TAGS, top, top ? [] : TOP_ONLY);
      if (!top) {
        const origin = await frameOrigin(frame, opts.worlds);
        if (!origin) continue;
        const scroll = await callIn(mainWorld, () => [scrollX, scrollY]);
        at = [origin[0] + scroll[0], origin[1] + scroll[1]];
      }
    } catch (error) {
      if (!top) continue; // a frame that went away while axe ran
      skipped.axe = `failed: ${safeMessage(error)}`;
      return [];
    }
    for (const s of found) {
      const [l, t, r, b] = [s.box[0] + at[0], s.box[1] + at[1], s.box[2] + at[0], s.box[3] + at[1]];
      const refs = s.criteria.length ? s.criteria.map((c) => `wcag:${c}`) : ['check:axe'];
      signals.push({ check: s.check, pass: passFor(s.criteria), rule: refs, axe_rule: s.axe_rule,
        anchor: { region: [l, t, r - l, b - t].map(Math.round), selector: s.selector },
        measured: s.measured, threshold: s.threshold, ...(s.review ? { advice: true } : {}),
        ...(top ? {} : { frame: numbers.get(frame) }) });
    }
  }
  const out = signals.length ? redact.redactSignals(signals, null) : [];
  ran.push('axe');
  return out;
}

module.exports = { run, passFor, VERSION, SHA256, TAGS };
