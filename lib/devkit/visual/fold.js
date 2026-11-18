// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Folding of duplicate signals (R68): the capture driver folds the redacted signals of one capture just before it
// numbers them, so one defect that two sources measure on one element is triaged once.
//
// Two signals fold when they hit the same element and either
//   - repeat each other: the same check, the same axe rule, and the same rule references; or
//   - map to the same WCAG criterion through the equivalence table shipped beside this file (equivalents.json): an
//     axe rule, a devkit check, or an impeccable rule listed under that criterion. A pair the table does not list is
//     never folded. A check the table lists under `both_controls` (an overlap, for target size) counts only when its
//     element and the element it overlaps are both controls in the description.
// The kept signal is the one from the first source in the table's order (axe, then devkit's own checks, then
// impeccable), the first in the list among signals of one source; the others go into its `duplicates`, each with its
// check, axe rule, rule references, measured value, threshold, and the second element of an overlap. The kept
// signals stay in their order, and every signal comes out with a `duplicates` list (empty when nothing folded into
// it), so folding the result again changes nothing.
//
// The element of a signal is its anchor's E<n>; without one, the description element at the anchor's box (the one
// its selector names when several share the box, else the innermost, as capture.cjs anchors them), else the one
// description element with that selector, else the selector at that box. A signal with no element and no selector
// (a console error) is never folded.
//
// Interface:
//
//   fold(signals, description) -> Signal[]     description: the capture's redacted description, or null
//   EQUIVALENTS                                the table, as equivalents.json holds it
'use strict';

const fs = require('fs');
const path = require('path');

const ELEMENT_ID = /^E[1-9][0-9]*$/;
const EQUIVALENTS = JSON.parse(fs.readFileSync(path.join(__dirname, 'equivalents.json'), 'utf8'));

// the table, indexed once: source -> rule or check id -> [criterion]
const TABLE = (() => {
  const index = Object.fromEntries(EQUIVALENTS.order.map((source) => [source, new Map()]));
  const bothControls = new Set();
  for (const [criterion, row] of Object.entries(EQUIVALENTS.criteria)) {
    for (const source of EQUIVALENTS.order) {
      for (const id of row[source] || []) {
        if (!index[source].has(id)) index[source].set(id, []);
        index[source].get(id).push(criterion);
      }
    }
    for (const id of row.both_controls || []) bothControls.add(`${criterion}\u0000${id}`);
  }
  return { index, bothControls, controls: new Set(EQUIVALENTS.controls) };
})();

function isBox(value) {
  return Array.isArray(value) && value.length === 4 && value.every((n) => typeof n === 'number' && Number.isFinite(n));
}

function boxKey(box) {
  return box.map(Math.round).join(',');
}

function sourceOf(signal) {
  return signal.check === 'axe' ? 'axe' : signal.check === 'impeccable' ? 'impeccable' : 'devkit';
}

function rulesOf(signal) {
  return Array.isArray(signal.rule) ? signal.rule.filter((r) => typeof r === 'string') : [];
}

// The ids the table knows a signal by: its axe rule, its impeccable rules, or its check.
function idsOf(signal) {
  if (signal.check === 'axe') return typeof signal.axe_rule === 'string' ? [signal.axe_rule] : [];
  if (signal.check === 'impeccable') {
    return rulesOf(signal).filter((r) => r.startsWith('impeccable:')).map((r) => r.slice('impeccable:'.length));
  }
  return typeof signal.check === 'string' ? [signal.check] : [];
}

// anchor -> the description element it hits ({id, role, ...}), or null
function resolver(description) {
  const byId = new Map();
  const byBox = new Map();
  const bySelector = new Map();
  const push = (map, key, e) => {
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(e);
  };
  const elements = description && Array.isArray(description.elements) ? description.elements : [];
  for (const e of elements) {
    if (!e || typeof e.id !== 'string' || !ELEMENT_ID.test(e.id)) continue;
    byId.set(e.id, e);
    if (e.frame !== null && e.frame !== undefined) continue; // signals are anchored in the top frame's elements
    if (isBox(e.box)) push(byBox, boxKey(e.box), e);
    if (typeof e.selector === 'string' && e.selector) push(bySelector, e.selector, e);
  }
  return (anchor) => {
    if (!anchor || typeof anchor !== 'object') return null;
    if (typeof anchor.element === 'string' && ELEMENT_ID.test(anchor.element)) {
      return byId.get(anchor.element) || { id: anchor.element };
    }
    const found = isBox(anchor.region) ? byBox.get(boxKey(anchor.region)) || [] : [];
    const chosen = found.find((e) => e.selector && e.selector === anchor.selector) || found[found.length - 1];
    if (chosen) return chosen;
    const named = typeof anchor.selector === 'string' ? bySelector.get(anchor.selector) || [] : [];
    return named.length === 1 ? named[0] : null;
  };
}

function elementKey(anchor, element) {
  if (element) return `E:${element.id}`;
  if (anchor && typeof anchor.selector === 'string' && anchor.selector && isBox(anchor.region)) {
    return `S:${anchor.selector}@${boxKey(anchor.region)}`;
  }
  return null;
}

function isControl(element) {
  return !!element && typeof element.role === 'string' && TABLE.controls.has(element.role);
}

// The criteria the table maps a signal to, on its element.
function criteriaOf(signal, element, resolve) {
  const source = sourceOf(signal);
  const out = new Set();
  for (const id of idsOf(signal)) {
    for (const criterion of TABLE.index[source].get(id) || []) {
      if (TABLE.bothControls.has(`${criterion}\u0000${id}`) &&
          !(isControl(element) && isControl(resolve(signal.other)))) continue;
      out.add(criterion);
    }
  }
  return [...out];
}

function duplicateOf(signal) {
  const entry = { check: signal.check };
  if (signal.axe_rule !== undefined) entry.axe_rule = signal.axe_rule;
  entry.rule = rulesOf(signal);
  entry.measured = signal.measured === undefined ? null : signal.measured;
  entry.threshold = signal.threshold === undefined ? null : signal.threshold;
  if (signal.other !== undefined) entry.other = signal.other;
  return entry;
}

function fold(signals, description = null) {
  if (!Array.isArray(signals)) throw new TypeError('fold takes a list of signals.');
  const resolve = resolver(description);
  const order = EQUIVALENTS.order;
  const items = signals.map((given, i) => {
    const signal = given && typeof given === 'object' ? given : {};
    const element = resolve(signal.anchor);
    const at = elementKey(signal.anchor, element);
    const repeat = `${signal.check}\u0000${signal.axe_rule || ''}\u0000${[...rulesOf(signal)].sort().join(',')}`;
    const keys = at === null ? [] : [`repeat:${repeat}@${at}`, ...criteriaOf(signal, element, resolve).map((c) => `wcag:${c}@${at}`)];
    return { i, signal, rank: order.indexOf(sourceOf(signal)), keys };
  });
  const byPriority = [...items].sort((a, b) => a.rank - b.rank || a.i - b.i);
  const owner = new Map(); // key -> the index of the kept signal that holds it
  const into = new Map(); // index of a folded signal -> index of the signal it folds into
  for (const item of byPriority) {
    const taken = item.keys.find((k) => owner.has(k));
    if (taken !== undefined) {
      into.set(item.i, owner.get(taken));
      continue;
    }
    for (const k of item.keys) owner.set(k, item.i);
  }
  const folded = new Map(); // index of a kept signal -> the duplicates that fold into it, in priority order
  for (const item of byPriority) {
    if (!into.has(item.i)) continue;
    const kept = into.get(item.i);
    if (!folded.has(kept)) folded.set(kept, []);
    const own = Array.isArray(item.signal.duplicates) ? item.signal.duplicates : [];
    folded.get(kept).push(duplicateOf(item.signal), ...own);
  }
  return items.filter((item) => !into.has(item.i)).map(({ i, signal }) => ({
    ...signal,
    duplicates: [...(Array.isArray(signal.duplicates) ? signal.duplicates : []), ...(folded.get(i) || [])],
  }));
}

module.exports = { fold, EQUIVALENTS };
