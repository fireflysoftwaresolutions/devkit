// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Redaction for visual diagnosis: the one place that decides what captured text may be stored. The capture driver
// (capture.cjs) runs every description and signal through it before anything is written, and visual.py runs the
// strings it writes into the product map through its JSON entry, so one implementation holds these rules:
//
//   Controls (R71)  C0 controls but tab and newline, DEL, C1 controls (U+0080-U+009F), and the bidirectional controls
//                   U+061C, U+200E, U+200F, U+202A-U+202E, U+2066-U+2069 are removed from every string first, so no
//                   stored or printed text carries a terminal escape or reorders what a reader sees, and a secret
//                   split by one of them is still found.
//   Secrets (R14)   gitleaks 8.30.1 scans the stripped text in memory (`gitleaks stdin`, no temporary file) with its
//                   default rules only. devkit reads each hit's rule id and line, never its Match or Secret (they
//                   come back as REDACTED). A string with a hit becomes `[redacted: <rule>]`; an element with a hit
//                   is marked `secret:<rule>` and painted over.
//   URLs (R15)      Query parameters named t or ticket, or whose name contains token, secret, key, sig, pass, auth,
//                   session, code, credential, or jwt in any letter case; user info; the fragment unless it is a
//                   hash route (`#/...`, whose own path and query follow these rules) or one plain word that is not
//                   token-shaped (`#export`, an overlay: kept, and part of the route pattern); and path segments
//                   shaped like a JWT or of 20 or more [A-Za-z0-9_-] characters that mix letters and digits with at
//                   least 3.5 bits of entropy a character. Each becomes `[redacted]`; in a route pattern such a
//                   segment is `:token`. Over-redaction is accepted (`postcode` matches `code`).
//   Form values (R16)  Never stored. A value is scanned before it is dropped, because the picture still shows it;
//                   a hit paints the field over.
//   Signals (R27)   Only the allowed fields are kept; the anchor's name comes from the redacted description; free
//                   text a check returns passes the rules above.
//
// The order for each string is: strip, scan, then the URL rules, then the cut to its length limit, so a secret
// that a cut or a control character would hide is found first.
//
// Interface (the visual diagnosis spec, section "The automated checks"):
//
//   strip(text) -> {text, removed}
//   redactUrl(url) -> {url, params}             params: the query parameter names whose values were redacted
//   routePattern(path) -> pattern               numbers, UUIDs, 16+ hex characters -> :id; token shapes -> :token
//   scan(texts) -> Hit[]                        Hit: {index, rule}, index being the string's position in `texts`
//   redactDescription(description, values, rules) -> {description, masks}
//       description  a devkit.visual.description/1 record without `redaction`, names and text whole (this cuts them)
//       values       {"E<n>": "<form value>"}: the form values the adapter held in memory
//       rules        {mask: ["E<n>", ...]}: the elements R13 masks (password and one-time-code fields, cards,
//                    data-visual-mask, the project's and the command's mask selectors)
//       masks        [{element, frame, box, reason}]: what the screenshot paints over; reason "mask" or "secret:<rule>"
//   redactSignals(signals, description, masked) -> Signal[]   description: the redacted one. Each Signal keeps check,
//                    pass, rule[], axe_rule, anchor {element, region, selector, name}, other (the second element of an
//                    overlap or what covers a control, an anchor of the same shape), advice (only `true`), frame
//                    (the index of the frame it was found in, only a whole number above 0), and the
//                    free text measured, threshold, text (cut at 120), and message (cut at 200); everything else
//                    (axe's html, failureSummary, target) is dropped. Signals are numbered and folded after this.
//       masked       {ids: ["E<n>"], texts: ["..."], boxes: [[x, y, w, h]]}, optional: the elements R13 masks, from the
//                    capture's walk (their description ids, their text, and the page boxes painted over them). A
//                    signal anchored on one (its anchor or `other` names a masked element, or one the description
//                    marks redacted "mask" or "secret:<rule>") or inside one (its region lies in a masked box) keeps
//                    no free text: its text, measured, and message, and the name of its `other`, are null. Any free
//                    text that holds a masked text of 4 or more characters, or ends at a cut (…) in the start of one,
//                    is null too.
//   redactStrings(strings) -> strings           for the map (R48)
//   redactSourceUrl(url) -> {url, params, rules, changed}   a capture's source URL: stripped; then gitleaks over the
//                    whole URL and each part of it, as written and decoded; a part with a hit becomes `[redacted]`
//                    (the user info, a path segment, a query parameter's value, or the whole parameter when its name
//                    holds the secret, the fragment), and a hit no single part holds makes the whole URL
//                    `[redacted: <rule>]`; then the URL rules. params: the names of the query parameters whose
//                    values were redacted; rules: the rule ids that hit; changed: whether the URL differs from the
//                    one given (the capture is then not reproducible from its record)
//
// Every function that scans throws a RedactionError whose `code` is "gitleaks-missing", "gitleaks-failed", or
// "bad-input"; the caller then writes nothing.
//
// Command line, for visual.py:
//
//   node redact.cjs --json < {"strings": [...]}
//     -> {"strings": [...], "secret_hits": n, "controls_stripped": n, "urls_redacted": n}
//   Exit 0 when done; 1 when gitleaks is missing or failed (install it with `dev install --only tools`); 2 when the
//   input is not that JSON. On exit 1 or 2 stdout is empty and stderr says why.
'use strict';

const fs = require('fs');
const { spawnSync } = require('child_process');

const REDACTED = '[redacted]';
const NAME_LIMIT = 120; // description names and text, and a truncation signal's text
const MESSAGE_LIMIT = 200; // console messages and other text a check returns

// eslint-disable-next-line no-control-regex
const CONTROLS = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/g;
const SECRET_PARAM = /token|secret|key|sig|pass|auth|session|code|credential|jwt/;
const SECRET_PARAM_NAMES = new Set(['t', 'ticket']);
// A JWT's header is base64url JSON, so it starts with "ey"; that keeps file names such as app.min.js out.
const JWT_SHAPE = /^ey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$/;
const TOKEN_CHARS = /^[A-Za-z0-9_-]+$/; // a token's characters, and those of a fragment that is one plain word
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ID_VALUE = /^(?:[0-9]+|[0-9a-f]{16,})$/i;
// scheme, //authority, path, ?query, #fragment; every part is optional, so every string matches
const URL_PARTS = /^([A-Za-z][A-Za-z0-9+.-]*:)?(\/\/[^/?#]*)?([^?#]*)(?:\?([^#]*))?(?:#([\s\S]*))?$/;
// URLs inside free text: absolute ones, and paths or queries that start a word
const URLS_IN_TEXT = /[A-Za-z][A-Za-z0-9+.-]*:\/\/[^\s"'<>`]+|(?<![\w/.:~-])[/?][^\s"'<>`]*/g;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}$/; // check ids, passes, rule references, axe rule ids
const ELEMENT_ID = /^E[1-9][0-9]*$/;
const RULE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$/;
const FORM_ROLES = new Set(['textbox', 'searchbox', 'combobox', 'spinbutton']);
const ELEMENT_TEXT = ['name', 'text', 'placeholder', 'url', 'locator', 'selector'];
const HIT_ORDER = ['name', 'text', 'placeholder', 'outline', 'url', 'value', 'locator', 'selector'];
const SIGNAL_TEXT = { measured: MESSAGE_LIMIT, threshold: MESSAGE_LIMIT, text: NAME_LIMIT, message: MESSAGE_LIMIT };

// gitleaks' own default rules and nothing else: a .gitleaks.toml, a .gitleaksignore, or a GITLEAKS_CONFIG from the
// project or the page must not switch a rule off. The configuration goes in the environment (no file is written),
// gitleaks runs in this module's folder (it reads .gitleaksignore from its working folder whatever it is told), and
// `gitleaks:allow` in captured text is ignored.
const GITLEAKS_RULES = '[extend]\nuseDefault = true\n';
const GITLEAKS_ARGS = ['stdin', '--no-banner', '--log-level', 'error', '--report-format', 'json', '--report-path', '-',
  '--exit-code', '3', '--redact', '--ignore-gitleaks-allow', '--gitleaks-ignore-path', __dirname];
const GITLEAKS_TIMEOUT_MS = 60000;
const NO_GITLEAKS = 'gitleaks is not installed, so the text cannot be checked for secrets. Nothing was recorded. ' +
  'Install it: dev install --only tools.';
const GITLEAKS_CHECK = 'Check the install with: gitleaks version';

const USAGE = 'Usage: node redact.cjs --json\n' +
  '  Reads {"strings": [...]} on stdin and writes {"strings": [...], "secret_hits": n, "controls_stripped": n,\n' +
  '  "urls_redacted": n} on stdout. Each string loses its control and bidirectional characters; a string in which\n' +
  '  gitleaks finds a secret becomes "[redacted: <rule>]"; URLs in the others follow the URL rules.\n' +
  '  Exit 0 when done, 1 when gitleaks is missing or failed (dev install --only tools), 2 when the input is not\n' +
  '  that JSON. Nothing is written to stdout unless the exit is 0.\n';

class RedactionError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'RedactionError';
    this.code = code;
  }
}

// --------------------------------------------------------------------------- controls (R71)

function strip(text) {
  if (typeof text !== 'string') return { text, removed: 0 };
  let removed = 0;
  const out = text.replace(CONTROLS, () => {
    removed += 1;
    return '';
  });
  return { text: out, removed };
}

// A copy of a JSON value with every string stripped, and how many characters went.
function stripDeep(value) {
  let removed = 0;
  const walk = (v) => {
    if (typeof v === 'string') {
      const r = strip(v);
      removed += r.removed;
      return r.text;
    }
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === 'object') {
      const out = {};
      for (const key of Object.keys(v)) if (key !== '__proto__') out[key] = walk(v[key]);
      return out;
    }
    return v;
  };
  return { value: walk(value), removed };
}

function cut(text, limit) {
  if (typeof text !== 'string') return text;
  const chars = Array.from(text);
  return chars.length <= limit ? text : `${chars.slice(0, limit - 1).join('')}…`;
}

// --------------------------------------------------------------------------- URLs (R15)

function entropy(text) {
  const counts = new Map();
  for (const ch of text) counts.set(ch, (counts.get(ch) || 0) + 1);
  let bits = 0;
  for (const n of counts.values()) {
    const p = n / text.length;
    bits -= p * Math.log2(p);
  }
  return bits;
}

function tokenShaped(segment) {
  if (JWT_SHAPE.test(segment)) return true;
  return segment.length >= 20 && TOKEN_CHARS.test(segment) && /[A-Za-z]/.test(segment) && /[0-9]/.test(segment) &&
    entropy(segment) >= 3.5;
}

function secretParam(raw) {
  let name = raw;
  try {
    name = decodeURIComponent(raw.replace(/\+/g, ' '));
  } catch (_) {
    // a malformed escape: match the name as written
  }
  name = name.toLowerCase();
  return SECRET_PARAM_NAMES.has(name) || SECRET_PARAM.test(name);
}

function redactPath(path) {
  return path.split('/').map((segment) => (tokenShaped(segment) ? REDACTED : segment)).join('/');
}

function redactQuery(query, params) {
  return query.split('&').map((part) => {
    const eq = part.indexOf('=');
    if (eq < 0) return tokenShaped(part) ? REDACTED : part;
    const name = part.slice(0, eq);
    if (eq === part.length - 1 || !secretParam(name)) return part;
    params.push(name);
    return `${name}=${REDACTED}`;
  }).join('&');
}

// A fragment of one plain word (letters, digits, `_` and `-`, so no `=`) that is not token-shaped names a place on
// the page or an overlay (`#export`), not a credential.
function plainWord(fragment) {
  return TOKEN_CHARS.test(fragment) && !tokenShaped(fragment);
}

function redactFragment(fragment, params) {
  if (fragment === '' || plainWord(fragment)) return fragment;
  if (!fragment.startsWith('/')) return REDACTED; // a hash route keeps its path and its query, under the same rules
  const q = fragment.indexOf('?');
  return q < 0 ? redactPath(fragment) : `${redactPath(fragment.slice(0, q))}?${redactQuery(fragment.slice(q + 1), params)}`;
}

function redactUrl(url) {
  if (typeof url !== 'string') return { url, params: [] };
  const params = [];
  const [, scheme = '', authority = '', path = '', query, fragment] = URL_PARTS.exec(url);
  let out = scheme;
  const at = authority.lastIndexOf('@');
  out += at > 2 ? `//${REDACTED}${authority.slice(at)}` : authority;
  out += redactPath(path);
  if (query !== undefined) out += `?${redactQuery(query, params)}`;
  if (fragment !== undefined) out += `#${redactFragment(fragment, params)}`;
  return { url: out, params };
}

// The URL rules applied to every URL inside free text (a console message, a map title, a link's name).
function redactUrlsInText(text) {
  let changed = false;
  const out = text.replace(URLS_IN_TEXT, (found) => {
    const redacted = redactUrl(found).url;
    if (redacted !== found) changed = true;
    return redacted;
  });
  return { text: out, changed };
}

function patternSegment(segment) {
  if (ID_VALUE.test(segment) || UUID.test(segment)) return ':id';
  if (segment === REDACTED || tokenShaped(segment)) return ':token';
  return segment;
}

function patternPath(path) {
  return path.split('/').map(patternSegment).join('/');
}

function routePattern(path) {
  if (typeof path !== 'string') return path;
  const [, , , rest = '', , fragment] = URL_PARTS.exec(path); // scheme, authority, and query are not part of a route
  let pattern = patternPath(rest || '/');
  if (fragment !== undefined && fragment.startsWith('/')) pattern += `#${patternPath(fragment.split('?')[0])}`;
  else if (fragment !== undefined && fragment !== '' && plainWord(fragment)) pattern += `#${fragment}`; // an overlay
  return pattern;
}

// --------------------------------------------------------------------------- secrets (R14)

function scan(texts) {
  if (!Array.isArray(texts)) throw new RedactionError('bad-input', 'scan takes a list of strings.');
  const owner = []; // owner[line - 1]: the position in `texts` of the string that line belongs to
  const lines = [];
  texts.forEach((text, index) => {
    if (typeof text !== 'string') return;
    for (const line of text.split('\n')) {
      lines.push(line);
      owner.push(index);
    }
  });
  const env = { ...process.env, GITLEAKS_CONFIG_TOML: GITLEAKS_RULES };
  delete env.GITLEAKS_CONFIG;
  const run = spawnSync('gitleaks', GITLEAKS_ARGS, {
    input: `${lines.join('\n')}\n`, cwd: __dirname, env, encoding: 'utf8', maxBuffer: 256 * 1024 * 1024,
    timeout: GITLEAKS_TIMEOUT_MS, windowsHide: true,
  });
  if (run.error && run.error.code === 'ENOENT') throw new RedactionError('gitleaks-missing', NO_GITLEAKS);
  if (run.error || (run.status !== 0 && run.status !== 3)) {
    const why = run.error ? (run.error.code || run.error.name) : (run.status === null ? run.signal : `exit ${run.status}`);
    throw new RedactionError('gitleaks-failed',
      `gitleaks did not finish (${why}), so the text was not checked for secrets. Nothing was recorded. ${GITLEAKS_CHECK}`);
  }
  let report = null;
  try {
    report = JSON.parse(run.stdout);
  } catch (_) {
    report = null;
  }
  if (!Array.isArray(report)) {
    throw new RedactionError('gitleaks-failed',
      `gitleaks printed a report devkit cannot read, so the text was not checked for secrets. Nothing was recorded. ${GITLEAKS_CHECK}`);
  }
  const hits = new Map();
  for (const finding of report) {
    const first = Number(finding && finding.StartLine);
    const last = Number(finding && finding.EndLine) || first;
    if (!Number.isInteger(first) || first < 1 || !Number.isInteger(last) || last < first || last > owner.length) {
      throw new RedactionError('gitleaks-failed',
        'gitleaks reported a secret on a line devkit cannot place, so it cannot be masked. Nothing was recorded.');
    }
    const rule = typeof finding.RuleID === 'string' && RULE_ID.test(finding.RuleID) ? finding.RuleID : 'secret';
    for (let line = first; line <= last; line += 1) {
      const index = owner[line - 1];
      hits.set(`${index}\u0000${rule}`, { index, rule });
    }
  }
  return [...hits.values()].sort((a, b) => a.index - b.index || (a.rule < b.rule ? -1 : a.rule > b.rule ? 1 : 0));
}

// The first rule (by name) that hit each string, or null.
function firstRules(texts) {
  const rules = texts.map(() => null);
  for (const hit of scan(texts)) if (rules[hit.index] === null) rules[hit.index] = hit.rule;
  return rules;
}

function secretText(rule) {
  return `[redacted: ${rule}]`;
}

// --------------------------------------------------------------------------- descriptions (R13 to R16)

function own(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function maskOf(element, reason) {
  return { element: element.id, frame: element.frame === undefined ? null : element.frame,
    box: Array.isArray(element.box) ? element.box : null, reason };
}

function redactDescription(description, values, rules) {
  if (!description || typeof description !== 'object' || !Array.isArray(description.elements)) {
    throw new RedactionError('bad-input', 'redactDescription takes a devkit.visual.description/1 record with its elements.');
  }
  const stripped = stripDeep(description);
  const formStripped = stripDeep(values && typeof values === 'object' ? values : {});
  const desc = stripped.value;
  const formValues = formStripped.value;
  const byId = new Map();
  for (const element of desc.elements) {
    if (!element || typeof element.id !== 'string' || !ELEMENT_ID.test(element.id)) {
      throw new RedactionError('bad-input', 'Every element of the description needs an id E<n>. Nothing was recorded.');
    }
    byId.set(element.id, element);
  }
  const unknown = (id) => new RedactionError('bad-input',
    `${typeof id === 'string' && ELEMENT_ID.test(id) ? id : 'An id that is not E<n>'} is not in the description, ` +
    'so it cannot be masked. Nothing was recorded.');
  for (const id of Object.keys(formValues)) if (!byId.has(id)) throw unknown(id);
  const maskIds = new Set();
  for (const id of (rules && Array.isArray(rules.mask) ? rules.mask : [])) {
    if (!byId.has(id)) throw unknown(id);
    maskIds.add(id);
  }

  // A masked element's children go with its text (R13); so do theirs.
  const children = new Map();
  for (const element of desc.elements) {
    if (element.parent && byId.has(element.parent)) {
      if (!children.has(element.parent)) children.set(element.parent, []);
      children.get(element.parent).push(element.id);
    }
  }
  const dropped = new Set();
  const stack = [...maskIds].flatMap((id) => children.get(id) || []);
  while (stack.length) {
    const id = stack.pop();
    if (!dropped.has(id)) {
      dropped.add(id);
      stack.push(...(children.get(id) || []));
    }
  }
  for (const id of dropped) maskIds.delete(id);
  const kept = desc.elements.filter((element) => !dropped.has(element.id));
  const isForm = (element) => FORM_ROLES.has(element.role) || own(formValues, element.id);
  const valuesOf = (element) => [formValues[element.id], element.text].filter((v) => typeof v === 'string' && v !== '');

  // Scan every string that could be stored, and the form values, in one gitleaks run.
  const texts = [];
  const owners = [];
  const add = (owner, text) => {
    if (typeof text === 'string' && text !== '') {
      texts.push(text);
      owners.push(owner);
    }
  };
  for (const element of kept) {
    if (maskIds.has(element.id)) continue; // dropped and painted over whatever it says
    const form = isForm(element);
    for (const field of ELEMENT_TEXT) if (!(form && field === 'text')) add({ id: element.id, field }, element[field]);
    if (form) for (const value of valuesOf(element)) add({ id: element.id, field: 'value' }, value);
  }
  const outline = Array.isArray(desc.outline) ? desc.outline : [];
  const regions = Array.isArray(desc.regions) ? desc.regions : [];
  outline.forEach((entry, i) => add({ outline: i }, entry && entry.text));
  regions.forEach((region, i) => add({ region: i }, region && region.name));
  const ruleOf = firstRules(texts);

  const hits = new Map(); // element id -> Map(field -> rule)
  const outlineHits = new Map();
  const regionHits = new Map();
  const hit = (id, field, rule) => {
    if (!hits.has(id)) hits.set(id, new Map());
    if (!hits.get(id).has(field)) hits.get(id).set(field, rule);
  };
  owners.forEach((owner, i) => {
    const rule = ruleOf[i];
    if (!rule) return;
    if (owner.id) {
      hit(owner.id, owner.field, rule);
    } else if (owner.outline !== undefined) {
      outlineHits.set(owner.outline, rule);
      const heading = outline[owner.outline] && outline[owner.outline].element;
      if (byId.has(heading) && !dropped.has(heading) && !maskIds.has(heading)) hit(heading, 'outline', rule);
    } else {
      regionHits.set(owner.region, rule);
    }
  });

  const counts = { values_dropped: 0, secret_hits: 0, value_secret_hits: 0, urls_redacted: 0 };
  const masks = [];
  for (const element of kept) {
    if (maskIds.has(element.id)) {
      Object.assign(element, { name: null, text: null, placeholder: null, url: null, locator: null, redacted: 'mask' });
      masks.push(maskOf(element, 'mask'));
      continue;
    }
    const found = hits.get(element.id) || new Map();
    if (isForm(element)) {
      const formValuesHere = valuesOf(element);
      element.text = null; // R16: a form control's text is its value
      for (const field of ['name', 'placeholder', 'locator', 'selector']) {
        const text = element[field];
        if (typeof text === 'string' && formValuesHere.some((v) => v.length >= 4 && text.includes(v))) element[field] = null;
      }
      if (!found.has('value')) {
        element.redacted = 'value';
        if (formValuesHere.length) counts.values_dropped += 1;
      }
    }
    for (const [field, rule] of found) {
      if (field === 'locator' || field === 'selector') element[field] = null;
      else if (field === 'outline') ['name', 'text'].forEach((f) => { if (element[f] !== null && element[f] !== undefined) element[f] = secretText(rule); });
      else if (field !== 'value') element[field] = secretText(rule);
    }
    if (['name', 'text', 'placeholder', 'outline'].some((field) => found.has(field))) element.locator = null;
    if (found.size) {
      const rule = found.get(HIT_ORDER.find((field) => found.has(field)));
      element.redacted = `secret:${rule}`;
      masks.push(maskOf(element, element.redacted));
      if (found.has('value')) counts.value_secret_hits += 1;
      if ([...found.keys()].some((field) => field !== 'value')) counts.secret_hits += 1;
    }
    if (typeof element.url === 'string' && !found.has('url')) {
      const url = redactUrl(element.url).url;
      if (url !== element.url) {
        element.url = url;
        counts.urls_redacted += 1;
      }
    }
    for (const field of ['name', 'text', 'placeholder', 'locator', 'selector']) {
      if (typeof element[field] === 'string' && !found.has(field)) {
        const r = redactUrlsInText(element[field]);
        if (r.changed) {
          element[field] = r.text;
          counts.urls_redacted += 1;
        }
      }
    }
    element.name = cut(element.name, NAME_LIMIT);
    element.text = cut(element.text, NAME_LIMIT);
  }

  const listed = (ids) => (Array.isArray(ids) ? ids.filter((id) => !dropped.has(id)) : ids);
  desc.elements = kept;
  if (Array.isArray(desc.outline)) {
    desc.outline = outline.flatMap((entry, i) => {
      if (!entry || dropped.has(entry.element)) return [];
      let text = entry.text;
      if (outlineHits.has(i)) text = secretText(outlineHits.get(i));
      else if (maskIds.has(entry.element)) text = null;
      else if (typeof text === 'string') text = cut(redactUrlsInText(text).text, NAME_LIMIT);
      return [{ ...entry, text }];
    });
  }
  if (Array.isArray(desc.regions)) {
    desc.regions = regions.map((region, i) => {
      let name = region.name;
      if (regionHits.has(i)) name = secretText(regionHits.get(i));
      else if (typeof name === 'string') name = cut(redactUrlsInText(name).text, NAME_LIMIT);
      return { ...region, name, elements: listed(region.elements) };
    });
  }
  desc.reading_order = listed(desc.reading_order);
  desc.interactive = listed(desc.interactive);
  if (desc.primary_action && dropped.has(desc.primary_action.element)) desc.primary_action = null;
  desc.redaction = {
    values_dropped: counts.values_dropped,
    masked: kept.filter((element) => maskIds.has(element.id)).map((element) => element.id),
    secret_hits: counts.secret_hits,
    value_secret_hits: counts.value_secret_hits,
    urls_redacted: counts.urls_redacted,
    controls_stripped: stripped.removed + formStripped.removed,
  };
  return { description: desc, masks };
}

// --------------------------------------------------------------------------- signals (R27)

function idOrNull(value) {
  return typeof value === 'string' && ID.test(value) ? value : null;
}

function ruleList(value) {
  const list = Array.isArray(value) ? value : value === undefined || value === null ? [] : [value];
  return list.filter((rule) => idOrNull(rule) !== null);
}

function regionOf(value) {
  if (!Array.isArray(value) || value.length !== 4 || !value.every((n) => typeof n === 'number' && Number.isFinite(n))) {
    return null;
  }
  return value.map(Math.round);
}

function anchorName(element) {
  const label = typeof element.name === 'string' && element.name !== '' ? element.name
    : typeof element.text === 'string' && element.text !== '' ? element.text : null;
  const role = typeof element.role === 'string' ? element.role : null;
  return label === null ? role : `${role} "${label}"`;
}

function flat(text) {
  return typeof text === 'string' ? text.replace(/\s+/g, ' ').trim() : '';
}

// The masked elements a signal must not speak for (R13): -> {isMasked(anchor), holds(text)}
function maskedElements(masked, elements) {
  const given = masked && typeof masked === 'object' && !Array.isArray(masked) ? masked : {};
  const ids = new Set((Array.isArray(given.ids) ? given.ids : []).filter((id) => typeof id === 'string' && ELEMENT_ID.test(id)));
  // the description's own masks: an element that holds masked text, and one painted over for a secret (a check may
  // have cut the secret to a fragment gitleaks cannot find)
  for (const [id, element] of elements) {
    if (element.redacted === 'mask' || (typeof element.redacted === 'string' && element.redacted.startsWith('secret:'))) {
      ids.add(id);
    }
  }
  const texts = (Array.isArray(given.texts) ? given.texts : []).map((t) => flat(strip(t).text)).filter((t) => t.length >= 4);
  const boxes = (Array.isArray(given.boxes) ? given.boxes : []).map(regionOf).filter(Boolean);
  // a region inside a masked box, with a pixel for rounding
  const inside = (r) => boxes.some((b) => r[0] >= b[0] - 1 && r[1] >= b[1] - 1 && r[0] + r[2] <= b[0] + b[2] + 1 &&
    r[1] + r[3] <= b[1] + b[3] + 1);
  const isMasked = (anchor) => !!anchor && ((anchor.element !== null && ids.has(anchor.element)) ||
    (anchor.region !== null && inside(anchor.region)));
  // a text that holds a masked text, or that a check cut (…) right in the start of one
  const holds = (value) => {
    const text = flat(value);
    if (!text) return false;
    const head = text.endsWith('…') ? text.slice(0, -1) : null;
    return texts.some((t) => {
      if (text.includes(t)) return true;
      if (head === null) return false;
      for (let k = Math.min(t.length - 1, head.length); k >= 4; k -= 1) if (head.endsWith(t.slice(0, k))) return true;
      return false;
    });
  };
  return { isMasked, holds, any: ids.size > 0 || texts.length > 0 || boxes.length > 0 };
}

function redactSignals(signals, description, masked) {
  if (!Array.isArray(signals)) throw new RedactionError('bad-input', 'redactSignals takes a list of signals.');
  const elements = new Map();
  for (const element of (description && Array.isArray(description.elements) ? description.elements : [])) {
    if (element && typeof element.id === 'string') elements.set(element.id, element);
  }
  const mask = maskedElements(masked, elements);
  const pending = []; // free text to scan in one run: {target, key, limit, nullOnHit}
  const free = (target, key, raw, limit, nullOnHit = false) => {
    if (typeof raw === 'number' && Number.isFinite(raw)) {
      target[key] = raw;
    } else if (typeof raw === 'string' && mask.any && mask.holds(strip(raw).text)) {
      target[key] = null;
    } else if (typeof raw === 'string') {
      target[key] = strip(raw).text;
      pending.push({ target, key, limit, nullOnHit });
    } else {
      target[key] = null;
    }
  };
  // an anchor: the element only when the description has it, its name from there, the selector scanned
  const anchorOf = (given) => {
    const anchor = given && typeof given === 'object' && !Array.isArray(given) ? given : {};
    const id = typeof anchor.element === 'string' && ELEMENT_ID.test(anchor.element) && elements.has(anchor.element)
      ? anchor.element : null;
    const kept = { element: id, region: regionOf(anchor.region), selector: null,
      name: id === null ? null : anchorName(elements.get(id)) };
    if (anchor.selector !== undefined && anchor.selector !== null) free(kept, 'selector', anchor.selector, MESSAGE_LIMIT, true);
    return kept;
  };
  const out = signals.map((given) => {
    const signal = given && typeof given === 'object' ? given : {};
    const kept = { check: idOrNull(signal.check), pass: idOrNull(signal.pass), rule: ruleList(signal.rule) };
    if (signal.axe_rule !== undefined) kept.axe_rule = idOrNull(signal.axe_rule);
    kept.anchor = anchorOf(signal.anchor);
    if (signal.other !== undefined) {
      kept.other = signal.other && typeof signal.other === 'object' && !Array.isArray(signal.other)
        ? anchorOf(signal.other) : null;
    }
    if (signal.advice === true) kept.advice = true;
    if (Number.isInteger(signal.frame) && signal.frame > 0) kept.frame = signal.frame;
    // a signal on or inside a masked element, or on what crosses one, says nothing of what it shows
    const silent = mask.isMasked(kept.anchor) || mask.isMasked(kept.other);
    for (const key of Object.keys(SIGNAL_TEXT)) {
      if (signal[key] === undefined) continue;
      if (silent && key !== 'threshold') kept[key] = null;
      else free(kept, key, signal[key], SIGNAL_TEXT[key]);
    }
    if (silent && kept.other) kept.other.name = null;
    return kept;
  });
  const rules = firstRules(pending.map((p) => p.target[p.key]));
  pending.forEach((p, i) => {
    if (rules[i]) p.target[p.key] = p.nullOnHit ? null : secretText(rules[i]);
    else p.target[p.key] = cut(redactUrlsInText(p.target[p.key]).text, p.limit);
  });
  return out;
}

// --------------------------------------------------------------------------- map strings (R48)

function redactStringList(strings) {
  if (!Array.isArray(strings) || !strings.every((s) => s === null || typeof s === 'string')) {
    throw new RedactionError('bad-input', 'redactStrings takes a list of strings (or nulls).');
  }
  let controls = 0;
  const stripped = strings.map((s) => {
    const r = strip(s);
    controls += r.removed;
    return r.text;
  });
  const rules = firstRules(stripped);
  let secrets = 0;
  let urls = 0;
  const out = stripped.map((s, i) => {
    if (typeof s !== 'string') return s;
    if (rules[i]) {
      secrets += 1;
      return secretText(rules[i]);
    }
    const r = redactUrlsInText(s);
    if (r.changed) urls += 1;
    return r.text;
  });
  return { strings: out, secret_hits: secrets, controls_stripped: controls, urls_redacted: urls };
}

function redactStrings(strings) {
  return redactStringList(strings).strings;
}

// --------------------------------------------------------------------------- the source URL (R14, R15)

function decoded(text) {
  try {
    return strip(decodeURIComponent(text.replace(/\+/g, ' '))).text;
  } catch (_) {
    return text; // a malformed escape: the server cannot decode it either
  }
}

function redactSourceUrl(url) {
  if (typeof url !== 'string') throw new RedactionError('bad-input', 'redactSourceUrl takes a URL string.');
  const text = strip(url).text;
  const [, scheme = '', authority = '', path = '', query, fragment] = URL_PARTS.exec(text);
  const at = authority.lastIndexOf('@');
  const segments = path.split('/');
  const params = query === undefined ? [] : query.split('&');
  // what one gitleaks run reads, each as written and decoded: the whole URL, then each part that can be redacted
  const parts = [{ kind: 'url', text }];
  if (at > 2) parts.push({ kind: 'user', text: authority.slice(2, at) });
  segments.forEach((segment, i) => { if (segment) parts.push({ kind: 'segment', i, text: segment }); });
  params.forEach((param, i) => {
    if (!param) return;
    parts.push({ kind: 'param', i, text: param });
    const eq = param.indexOf('=');
    if (eq > 0) parts.push({ kind: 'name', i, text: param.slice(0, eq) });
  });
  if (fragment) parts.push({ kind: 'fragment', text: fragment });
  const hits = scan(parts.flatMap((part) => [part.text, decoded(part.text)]));
  const rules = [...new Set(hits.map((h) => h.rule))].sort();
  if (!rules.length) {
    const out = redactUrl(text);
    return { url: out.url, params: out.params, rules, changed: out.url !== url };
  }
  const hit = new Set(hits.map((h) => parts[Math.floor(h.index / 2)]));
  const names = new Set([...hit].filter((part) => part.kind === 'name').map((part) => part.i));
  let parted = false;
  let user = at > 2 ? authority.slice(2, at) : null;
  const named = []; // the parameters whose value alone went
  for (const part of hit) {
    parted = parted || part.kind !== 'url';
    if (part.kind === 'user') user = REDACTED;
    else if (part.kind === 'segment') segments[part.i] = REDACTED;
    else if ((part.kind === 'param' || part.kind === 'name') && params[part.i] !== REDACTED) {
      const eq = params[part.i].indexOf('=');
      if (eq > 0 && !names.has(part.i)) {
        named.push(params[part.i].slice(0, eq));
        params[part.i] = `${params[part.i].slice(0, eq)}=${REDACTED}`;
      } else {
        params[part.i] = REDACTED;
      }
    }
  }
  if (!parted) {
    const rule = hits[0].rule; // scan sorts its hits; the whole URL is the first text
    return { url: secretText(rule), params: [], rules, changed: true };
  }
  const host = at > 2 ? `//${user}${authority.slice(at)}` : authority;
  let out = `${scheme}${host}${segments.join('/')}`;
  if (query !== undefined) out += `?${params.join('&')}`;
  if (fragment !== undefined) out += `#${[...hit].some((part) => part.kind === 'fragment') ? REDACTED : fragment}`;
  const ruled = redactUrl(out);
  return { url: ruled.url, params: [...new Set([...named, ...ruled.params])], rules, changed: ruled.url !== url };
}

// --------------------------------------------------------------------------- command line

function main(argv) {
  if (argv.length === 1 && (argv[0] === '--help' || argv[0] === '-h')) {
    process.stdout.write(USAGE);
    return 0;
  }
  if (argv.length !== 1 || argv[0] !== '--json') {
    process.stderr.write(USAGE);
    return 2;
  }
  let input = null;
  try {
    input = JSON.parse(fs.readFileSync(0, 'utf8'));
  } catch (_) {
    input = null;
  }
  const bad = '✗ redact.cjs --json takes {"strings": [...]} on stdin: a list of strings or nulls.\n';
  if (!input || typeof input !== 'object' || Array.isArray(input) || !Array.isArray(input.strings)) {
    process.stderr.write(bad);
    return 2;
  }
  try {
    process.stdout.write(`${JSON.stringify(redactStringList(input.strings))}\n`);
    return 0;
  } catch (error) {
    if (!(error instanceof RedactionError)) throw error;
    process.stderr.write(error.code === 'bad-input' ? bad : `✗ ${error.message}\n`);
    return error.code === 'bad-input' ? 2 : 1;
  }
}

module.exports = {
  strip, redactUrl, routePattern, scan, redactDescription, redactSignals, redactStrings, redactSourceUrl,
  RedactionError,
};

if (require.main === module) process.exitCode = main(process.argv.slice(2));
