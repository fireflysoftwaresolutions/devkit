// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// The derived parts of a capture's description (R64), and the non-semantic-control check.
//
// derive(description) fills, from the structure layer alone (the elements capture.cjs's adapter built), the parts a
// reviewer reads before the picture:
//
//   regions         the top page's landmarks (banner, navigation, main, contentinfo, complementary, search, form,
//                   region), its dialogs, and its headed sections (a container whose first element is a heading),
//                   in reading order: {id: "R<n>", kind, role, name, element, box, elements}; `kind` is the
//                   landmark's role, "dialog", or "section"; `elements` are the headings, controls, and images
//                   inside, the elements a reviewer cites.
//   outline         the top page's headings in reading order: {level, text, element}. A frame's headings belong to
//                   its own document, so they are left out.
//   reading_order   the elements that carry text, a name, or a control, in the snapshot's order.
//   interactive     the controls (the roles in INTERACTIVE), in the snapshot's order.
//   primary_action  {element, basis: "heuristic"}: the enabled, unmasked, named button or link of the top page that
//                   scores highest (above the fold, a button, filled, inside main, then the larger one), or null.
//                   It is a candidate for the reviewer and never raises a signal by itself.
//
// nonSemanticControls(page, session, description) reads the live top page in an isolated world (a CDP world of its
// own, so the page's scripts cannot replace the built-ins it uses) and returns the non-semantic-control signals
// (pass accessibility, wcag:4.1.2). A candidate is a `generic` element of the top page's description with a box, no
// control among its descendants, and no control among its ancestors. In the page it is a group: the element its
// description selector names (else the generic-looking elements with its box) with the generic-looking elements
// inside it that share its box, since a wrapper and its content can share one box and the snapshot keeps one of
// them. The check fires on the outermost candidates whose computed cursor is pointer and that have a click handler:
// an onclick attribute, or a click listener that DOMDebugger.getEventListeners returns for the node in the page's own
// world. A cursor is inherited, so a card's pointer cursor cannot tell the clickable element inside it from the card;
// the handler can. Only when no pointer candidate has a handler (a framework that listens on the document) does it
// fall back to the outermost candidates whose own cursor is pointer and whose parent's is not. A signal holds
// devkit's fixed wording, the element's box as its region, and its selector (the description's, else a unique path
// computed in the page); capture.cjs names the element from the region at step 10. Frames are left to axe: their
// elements carry selectors of their own document.
//
// describe(page, session, description, {check}) -> {signals, skipped} does both: capture.cjs calls it at step 8,
// after the redaction, so the derived parts read the redacted names. Without check it only derives, and opens
// nothing. When the page cannot be read, the derived parts stay, no signal is raised, and `skipped` says why; a
// capture never fails because of this module.
'use strict';

const INTERACTIVE = new Set(['button', 'link', 'textbox', 'searchbox', 'combobox', 'spinbutton', 'checkbox', 'radio',
  'switch', 'slider', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'treeitem', 'gridcell']);
const LANDMARKS = new Set(['banner', 'navigation', 'main', 'contentinfo', 'complementary', 'search', 'form', 'region']);
const DIALOGS = new Set(['dialog', 'alertdialog']);
const CITED = new Set(['heading', 'img']);
const WORLD = 'devkit-describe';
const GROUP = 'devkit-describe';
const CHECK = 'non-semantic-control';

// --------------------------------------------------------------------------- the derived parts (pure)

function tree(elements) {
  const byId = new Map();
  const children = new Map();
  for (const e of elements) {
    byId.set(e.id, e);
    if (!children.has(e.parent)) children.set(e.parent, []);
    children.get(e.parent).push(e);
  }
  const descendants = (id) => {
    const out = [];
    const walk = (pid) => {
      for (const c of children.get(pid) || []) {
        out.push(c);
        walk(c.id);
      }
    };
    walk(id);
    return out;
  };
  const ancestors = (e) => {
    const out = [];
    for (let p = byId.get(e.parent); p; p = byId.get(p.parent)) out.push(p);
    return out;
  };
  return { byId, children, descendants, ancestors };
}

function hasBox(e) {
  return Array.isArray(e.box) && e.box.length === 4 && e.box[2] > 0 && e.box[3] > 0;
}

function cited(e) {
  return INTERACTIVE.has(e.role) || CITED.has(e.role);
}

function regionsOf(elements, t) {
  const out = [];
  for (const e of elements) {
    if (e.frame !== null && e.frame !== undefined) continue;
    let kind = null;
    let name = typeof e.name === 'string' ? e.name : null;
    if (LANDMARKS.has(e.role)) kind = e.role;
    else if (DIALOGS.has(e.role)) kind = 'dialog';
    else {
      const first = (t.children.get(e.id) || [])[0];
      if (first && first.role === 'heading' && !INTERACTIVE.has(e.role) && e.role !== 'heading') {
        kind = 'section';
        name = typeof first.name === 'string' ? first.name : first.text || null;
      }
    }
    if (!kind) continue;
    out.push({ id: `R${out.length + 1}`, kind, role: e.role, name, element: e.id, box: e.box ? [...e.box] : null,
      elements: t.descendants(e.id).filter(cited).map((d) => d.id) });
  }
  return out;
}

function outlineOf(elements) {
  return elements.filter((e) => e.role === 'heading' && (e.frame === null || e.frame === undefined)).map((e) => ({
    level: e.states && Number.isInteger(e.states.level) ? e.states.level : null,
    text: typeof e.name === 'string' && e.name ? e.name : e.text || null, element: e.id,
  }));
}

function filled(style) {
  const bg = style && typeof style.background === 'string' ? style.background.toLowerCase() : null;
  return Boolean(bg) && !/^#(?:fff(?:fff)?|ffffff00|[0-9a-f]{6}00)$/.test(bg);
}

function primaryOf(elements, t) {
  const main = new Set(elements.filter((e) => e.role === 'main').flatMap((m) => t.descendants(m.id).map((d) => d.id)));
  let best = null;
  for (const e of elements) {
    if (!(e.role === 'button' || e.role === 'link') || !hasBox(e)) continue;
    if ((e.frame !== null && e.frame !== undefined) || !e.name || e.redacted === 'mask') continue;
    if (e.states && e.states.disabled) continue;
    const score = [e.above_fold === true ? 1 : 0, e.role === 'button' ? 1 : 0, filled(e.style) ? 1 : 0,
      main.has(e.id) ? 1 : 0, e.box[2] * e.box[3]];
    const better = !best || score.some((v, i) => v !== best.score[i] && v > best.score[i] &&
      score.slice(0, i).every((w, j) => w === best.score[j]));
    if (better) best = { id: e.id, score };
  }
  return best ? { element: best.id, basis: 'heuristic' } : null;
}

function derive(description) {
  const elements = Array.isArray(description.elements) ? description.elements : [];
  const t = tree(elements);
  description.regions = regionsOf(elements, t);
  description.outline = outlineOf(elements);
  description.reading_order = elements.filter((e) => e.text || e.name || INTERACTIVE.has(e.role) || e.role === 'img')
    .map((e) => e.id);
  description.interactive = elements.filter((e) => INTERACTIVE.has(e.role)).map((e) => e.id);
  description.primary_action = primaryOf(elements, t);
  return description;
}

// --------------------------------------------------------------------------- in the page (isolated world)

// For each candidate {selector, box}: its group, the element its description selector names (else the generic-looking
// elements with its page box, in document order) and the generic-looking elements inside it with the same box, since
// a wrapper and its content can share one box and the snapshot keeps one of them. The groups stay in this world for
// the listener check. -> per candidate {found, pointer, parentPointer, handlers, pointers, paths}: whether any of the
// group's computed cursors is pointer and its outermost element's parent's is, the group indexes with an onclick
// attribute and with a pointer cursor, and for a pointer group a selector that matches each of its elements alone.
function locate(candidates) {
  const GENERIC = new Set(['div', 'span', 'section', 'p', 'li', 'td', 'b', 'i', 'em', 'strong', 'small', 'label',
    'header', 'footer', 'article', 'aside', 'figure', 'figcaption', 'body', 'html']);
  const generic = (node) => GENERIC.has(node.localName) && !node.hasAttribute('role');
  const pageBox = (node) => {
    const r = node.getBoundingClientRect();
    return [r.left + scrollX, r.top + scrollY, r.width, r.height].map(Math.round).join(',');
  };
  const pointer = (node) => getComputedStyle(node).cursor === 'pointer';
  const unique = (sel) => {
    try {
      return document.querySelectorAll(sel).length === 1;
    } catch (_) {
      return false;
    }
  };
  const esc = CSS.escape;
  // devkit's order, as the capture's walk writes selectors without its four-step limit: the element's test id, its
  // id, then the shortest path of tags (nth-of-type where a tag repeats) up to an id that is unique
  const path = (el) => {
    const testid = el.getAttribute('data-testid');
    if (testid !== null && unique(`[data-testid="${esc(testid)}"]`)) return `[data-testid="${esc(testid)}"]`;
    if (el.id && unique(`#${esc(el.id)}`)) return `#${esc(el.id)}`;
    const steps = [];
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
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
  let byBox = null;
  const boxed = (key) => {
    if (!byBox) {
      byBox = new Map();
      for (const node of document.querySelectorAll('*')) {
        if (!generic(node)) continue;
        const k = pageBox(node);
        if (!byBox.has(k)) byBox.set(k, []);
        byBox.get(k).push(node);
      }
    }
    return byBox.get(key) || [];
  };
  const groups = candidates.map((c) => {
    if (typeof c.selector === 'string' && c.selector) {
      let hits = [];
      try {
        hits = document.querySelectorAll(c.selector);
      } catch (_) {
        hits = [];
      }
      if (hits.length === 1) {
        const key = pageBox(hits[0]);
        return [hits[0], ...Array.from(hits[0].querySelectorAll('*')).filter((n) => generic(n) && pageBox(n) === key)];
      }
    }
    return Array.isArray(c.box) ? boxed(c.box.map(Math.round).join(',')) : [];
  });
  globalThis.__devkitDescribe = groups;
  return groups.map((group) => {
    if (!group.length) return { found: false };
    const pointers = group.map((n, j) => (pointer(n) ? j : -1)).filter((j) => j >= 0);
    const parent = group[0].parentElement;
    return {
      found: true, pointer: pointers.length > 0, parentPointer: parent ? pointer(parent) : false, pointers,
      handlers: group.map((n, j) => (n.hasAttribute('onclick') ? j : -1)).filter((j) => j >= 0),
      paths: pointers.length ? group.map(path) : [],
    };
  });
}

// --------------------------------------------------------------------------- the check

async function isolatedWorld(session) {
  const { frameTree } = await session.send('Page.getFrameTree');
  const world = await session.send('Page.createIsolatedWorld', { frameId: frameTree.frame.id, worldName: WORLD,
    grantUniveralAccess: false });
  return world.executionContextId;
}

async function call(session, contextId, fn, arg) {
  const result = await session.send('Runtime.callFunctionOn', { functionDeclaration: fn.toString(),
    executionContextId: contextId, arguments: [{ value: arg }], returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'the page script failed');
  return result.result.value;
}

// Whether the group member has a click listener. DOMDebugger.getEventListeners lists the listeners of the world the
// node's object belongs to, so the node is resolved again in the page's own world, by its backend node id; asking
// for the listeners runs no page script.
async function hasClickListener(session, contextId, index, member) {
  const { result } = await session.send('Runtime.callFunctionOn', {
    functionDeclaration: 'function (i, j) { return globalThis.__devkitDescribe[i][j]; }', executionContextId: contextId,
    arguments: [{ value: index }, { value: member }], returnByValue: false, objectGroup: GROUP,
  });
  if (!result || !result.objectId) return false;
  const { node } = await session.send('DOM.describeNode', { objectId: result.objectId });
  const { object } = await session.send('DOM.resolveNode', { backendNodeId: node.backendNodeId, objectGroup: GROUP });
  const { listeners } = await session.send('DOMDebugger.getEventListeners', { objectId: object.objectId });
  return (listeners || []).some((l) => l.type === 'click');
}

function outermost(chosen, t) {
  const ids = new Set(chosen.map((c) => c.e.id));
  return chosen.filter((c) => !t.ancestors(c.e).some((a) => ids.has(a.id)));
}

function signal(c, basis) {
  const selector = typeof c.e.selector === 'string' && c.e.selector ? c.e.selector : c.fact.paths[c.member] || null;
  return {
    check: CHECK, pass: 'accessibility', rule: ['wcag:4.1.2'], anchor: { region: [...c.e.box], selector },
    measured: basis === 'handler' ? 'click handler and pointer cursor on a generic element, no role'
      : 'pointer cursor on a generic element, no role',
    threshold: 'a control role (button or link)',
  };
}

async function nonSemanticControls(page, session, description) {
  const elements = Array.isArray(description.elements) ? description.elements : [];
  const t = tree(elements);
  const candidates = elements.filter((e) => e.role === 'generic' && (e.frame === null || e.frame === undefined) &&
    hasBox(e) && !t.descendants(e.id).some((d) => INTERACTIVE.has(d.role)) &&
    !t.ancestors(e).some((a) => INTERACTIVE.has(a.role)));
  if (!candidates.length) return [];
  const contextId = await isolatedWorld(session);
  try {
    const facts = await call(session, contextId, locate, candidates.map((e) => ({ selector: e.selector, box: e.box })));
    const rows = candidates.map((e, i) => ({ e, fact: facts[i] || { found: false }, index: i }))
      .filter((r) => r.fact.found && r.fact.pointer);
    const handled = [];
    for (const r of rows) {
      // an onclick attribute is a listener too; reading it first saves the CDP round trips
      let member = r.fact.handlers.length ? r.fact.handlers[0] : -1;
      for (let j = 0; member < 0 && j < r.fact.paths.length; j += 1) {
        if (await hasClickListener(session, contextId, r.index, j)) member = j;
      }
      if (member >= 0) handled.push({ ...r, member });
    }
    if (handled.length) return outermost(handled, t).map((r) => signal(r, 'handler'));
    return outermost(rows.filter((r) => !r.fact.parentPointer), t)
      .map((r) => signal({ ...r, member: r.fact.pointers[0] }, 'cursor'));
  } finally {
    await session.send('Runtime.releaseObjectGroup', { objectGroup: GROUP }).catch(() => {});
  }
}

function why(error) {
  const text = String(error && error.message ? error.message : error).split('\n')[0];
  return text.replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '').slice(0, 160);
}

async function describe(page, session, description, options = {}) {
  derive(description);
  if (!options.check) return { signals: [], skipped: null };
  let own = null;
  try {
    const cdp = session || (own = await page.context().newCDPSession(page));
    return { signals: await nonSemanticControls(page, cdp, description), skipped: null };
  } catch (error) {
    return { signals: [], skipped: `failed: ${why(error)}` };
  } finally {
    if (own) await own.detach().catch(() => {});
  }
}

module.exports = { describe, derive, nonSemanticControls, INTERACTIVE };
