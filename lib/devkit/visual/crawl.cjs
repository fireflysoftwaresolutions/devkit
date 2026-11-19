// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// The crawler and the journey walker of visual diagnosis. Both drive the pinned playwright-core's Chromium (found as
// capture.cjs finds it) with the capture's browser footprint: a new 0700 TMPDIR for the profile, deleted at the end
// also after a failure, chromiumSandbox on, no crash reporter, no downloads, service workers blocked.
//
// The crawl (R48) builds the nodes and edges of the product map from a start URL: same origin only, breadth first,
// a depth limit (3) and a page limit (60), one node per route pattern (redact.cjs's routePattern, plus the query
// parameters a map node lists as making a separate screen, ?tab=), one edge per link or navigation control with its
// trigger (role, accessible name, a CSS selector that matches it alone), the HTTP status of its target, and the
// redirects it passes. It causes no side effects in the app beyond GET requests for documents and their resources
// (R70):
//
//   - every request that is not a GET is aborted and counted (a form's POST never leaves the browser);
//   - the avoid words (delete, remove, pay, send, submit, sign out, logout, and the project's and the command's)
//     apply to control names, link text, and URL paths and queries: such a link is recorded as an edge to a node the
//     crawl does not open, such a control is never clicked, and no request of any kind goes to such a path;
//     --exclude globs skip paths the same way, matched on the path as written and as the app decodes it
//     (/%61dmin/panel is /admin/panel);
//   - every WebSocket of a page goes to a routeWebSocket handler that never calls connectToServer() and closes it, so
//     the server never sees a handshake; a worker's WebSocket goes around that handler, so each document and each
//     worker script gets one more policy, connect-src http: https: data: blob:, which refuses ws: and wss: in the
//     page and in its workers (a second policy only narrows the app's own): a worker script in a header of its
//     answer, a document as a <meta> policy an init script adds before the page's scripts run, which a worker made
//     from a blob: URL takes too;
//   - service workers are blocked on the context (page.route does not see their requests), and a window a page
//     opens itself gets no request through;
//   - Chromium runs a shared worker in a target of its own, which neither the context's route nor a page's CDP session
//     sees, so a guard on the whole browser (a browser CDP session's Fetch domain, at both stages) holds every request
//     of every target to the same rules before it is sent: GET only, counted when blocked; no document on another
//     origin; no avoided or excluded path; the redirect hops of the response guard below, and CONNECT_POLICY on each
//     answer;
//   - a document request to another origin is aborted. The browser loads each page from the app itself, so the page
//     keeps the app's address and its images, scripts, and fetches on another port of this Mac load (Chromium's
//     local network access checks refuse them for a page whose answer the crawler handed it). A response guard on
//     each page (a CDP session's Fetch domain, at the response stage) stops a redirect before the browser follows
//     it: a document's always, since the crawler walks each page's redirect chain itself, one hop at a time with
//     maxRedirects 0, so a redirect to another origin is recorded as an external edge and that origin gets no
//     request; an image's, a script's, a fetch's, or a worker request's when the hop leaves the app's origin or goes
//     to a path the avoid words or --exclude keep the crawl from. Every other hop is followed by the browser, so a
//     script that redirected resolves its relative imports where the redirect ended.
//
// Controls (buttons, tabs, menu items) are clicked one at a time on a fresh load of their page; a navigation the
// click starts is recorded and aborted before it leaves the browser, then followed as a GET like a link.
//
// Every string of the result passes redact.cjs (control characters, gitleaks, the URL rules) before it is printed;
// map.py redacts the map again when it saves it.
//
// The walk (R51) performs a journey's recipe steps in one browser session, with capture.cjs's own step runner so a
// capture's recipe replays the same state, and records each step reached: the HTTP status of the document the step
// loaded (null when it stayed on the page), the redirects it passed, and whether each value the journey keeps was
// on the page (its text, its form values, its URL), tested in memory and reported only as present or absent (R16).
// A step whose trigger is not found stops the walk there.
//
// Interface:
//
//   crawl(request) -> {start, origin, nodes, edges, partial, left, depth, timed_out, stopped_by, timeout_s, blocked,
//                      blocked_count, not_clicked, excluded, external, websockets}
//     stopped_by  the bound that left links unfollowed: "time", "pages", "depth", or null
//     nodes  [{key, kind: "screen"|"external", route, title, example, status, skipped}]; key is the route (screens)
//            or the origin (external destinations); skipped: null, "avoid", "exclude", "answered <status>", or
//            "did not load"
//     edges  [{from, to, kind: "nav"|"action"|"submit"|"external", trigger: {role, name, selector}, status,
//              redirects}]; from and to are node keys
//   walk(request) -> {steps: [{step, route, title, status, redirects, keeps: {name: true|false|null}}],
//                     broken: null|{step, do, error}}; route is the step's route pattern in the map's screens, title
//                     its first h1 or its document title
//   avoided(text, words) -> the avoid word the text holds, or null;   avoidWords(extra) -> the words with extra ones
//   excluded(path, globs) -> whether a glob matches the path;          routeOf(url, screens) -> the route pattern
//
// Command line, for map.py and walk.py:
//
//   node crawl.cjs --json < request
//     crawl: {"mode": "crawl", "start": "<url>", "within": "<route prefix>"|null, "depth": 3, "max": 60,
//             "avoid": ["<word>"], "exclude": ["<glob>"], "screens": {"params": {"<path>": ["tab"]},
//             "overlays": ["/invoices#export"]}, "storage_state": "<file>"|null, "steps": ["<step>"],
//             "remote": false, "timeout_s": 600, "core": "<playwright-core folder>"|null}
//     walk:  {"mode": "walk", "start": "<url>", "steps": [["<step>", ...] for each edge], "keeps": {"<name>":
//             "<value>"|null}, "screens": {...as for the crawl}, "viewport": [w, h], "theme": "light"|"dark",
//             "storage_state": null, "remote": false, "core": null}
//   -> the result as JSON on stdout, exit 0. Exit 1 when the crawl or walk failed (stderr says why), 2 when the
//   request is wrong. On exit 1 or 2 stdout is empty.
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const redact = require('./redact.cjs');
const capture = require('./capture.cjs');

const DEFAULT_AVOID = ['delete', 'remove', 'pay', 'send', 'submit', 'sign out', 'logout'];
const WORD_ENDINGS = ['', 's', 'es', 'd', 'ed', 'ing', 'ted', 'ting'];
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);
const LOAD_TIMEOUT_MS = 30000;
const IDLE_TIMEOUT_MS = 3000;
const CLICK_TIMEOUT_MS = 2000;
const PROBE_WAIT_MS = 1500;
const MAX_REDIRECTS = 10;
const REDIRECTS = new Set([301, 302, 303, 307, 308]);
const MAX_CONTROLS = 20; // controls clicked on one page
const FIX_INSTALL = 'dev install --only tools';
const ACCEPT = 'text/html,application/xhtml+xml,*/*;q=0.8';
// added to every answer the response guard lets through: no WebSocket from a page or a worker (ws: and wss: match
// none of these)
const CONNECT_POLICY = 'connect-src http: https: data: blob:';

class CrawlError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'CrawlError';
    this.code = code;
  }
}

function bad(message) {
  return new CrawlError('bad-request', message);
}

function firstLine(error) {
  const text = String((error && error.message) || error).split('\n')[0];
  return text.replace(/[A-Za-z][A-Za-z0-9+.-]*:\/\/[^\s"'<>]+/g, (url) => redact.redactUrl(url).url).slice(0, 300);
}

// --------------------------------------------------------------------------- names and paths

function words(text) {
  return String(text || '').toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
}

function avoidWords(extra) {
  const out = [];
  for (const w of [...DEFAULT_AVOID, ...(extra || [])]) {
    const term = words(w).join(' ');
    if (term && !out.includes(term)) out.push(term);
  }
  return out;
}

// The avoid word `text` holds, or null. Words are compared whole, joined, and with common endings, so "Sign out",
// "SIGN-OUT", and "signout" all hold "sign out", "/logout" and "Log out" hold "logout", "Deleting" holds "delete",
// and "Payments" holds nothing.
function avoided(text, terms) {
  const have = words(text);
  for (const term of terms) {
    const want = words(term).join('');
    if (!want) continue;
    for (let i = 0; i < have.length; i += 1) {
      let joined = '';
      for (let j = i; j < Math.min(have.length, i + 3); j += 1) {
        joined += have[j];
        if (WORD_ENDINGS.some((end) => joined === want + end)) return term;
        if (joined.length >= want.length + 4) break;
      }
    }
  }
  return null;
}

function globRegex(glob) {
  let out = '';
  for (let i = 0; i < glob.length; i += 1) {
    const ch = glob[i];
    if (glob.startsWith('/**', i) && (i + 3 === glob.length || glob[i + 3] === '/')) {
      out += '(?:/.*)?';
      i += 2;
    } else if (glob.startsWith('**', i)) {
      out += '.*';
      i += 1;
    } else if (ch === '*') {
      out += '[^/]*';
    } else if (ch === '?') {
      out += '[^/]';
    } else {
      out += ch.replace(/[\\^$.|+()[\]{}]/g, '\\$&');
    }
  }
  return new RegExp(`^${out}$`);
}

// Whether a glob matches the path as written or as the app decodes it, so an encoded letter (/%61dmin) or slash
// (/admin%2Fusers) does not slip past --exclude; a path whose escapes do not decode is matched as written.
function excluded(pathname, globs) {
  const paths = [pathname];
  try {
    const decoded = decodeURIComponent(pathname);
    if (decoded !== pathname) paths.push(decoded);
  } catch (_) {
    // a malformed escape: the app cannot decode it either
  }
  return (globs || []).some((g) => paths.some((p) => globRegex(g).test(p)));
}

// The route pattern of a URL: redact.cjs's routePattern, plus the query parameters the map lists as making a separate
// screen at that path (screens.params, as "?tab=security"), with a plain fragment kept only when the map has a node
// at it (screens.overlays: an overlay such as /invoices#export); any other plain fragment is an anchor on the page.
function routeOf(url, screens = {}) {
  const pattern = redact.routePattern(redact.strip(url).text);
  const hash = pattern.indexOf('#');
  const base = hash < 0 ? pattern : pattern.slice(0, hash);
  let fragment = hash < 0 ? '' : pattern.slice(hash);
  if (fragment && !fragment.startsWith('#/') && !(screens.overlays || []).includes(base + fragment)) fragment = '';
  const listed = (screens.params || {})[base] || [];
  let query = '';
  if (listed.length) {
    let parsed = null;
    try {
      parsed = new URL(url, 'http://localhost');
    } catch (_) {
      parsed = null;
    }
    const pairs = parsed ? listed.filter((n) => parsed.searchParams.has(n))
      .map((n) => `${encodeURIComponent(n)}=${encodeURIComponent(parsed.searchParams.get(n))}`) : [];
    if (pairs.length) query = redact.redactUrl(`?${pairs.join('&')}`).url;
  }
  return base + query + fragment;
}

function within(route, prefix) {
  if (!prefix) return true;
  const base = prefix.replace(/\/+$/, '') || '/';
  return route === base || route.startsWith(`${base.replace(/\/$/, '')}/`) || route.startsWith(`${base}#`) ||
    route.startsWith(`${base}?`);
}

function isLocal(url) {
  return LOCAL_HOSTS.has(url.hostname);
}

// --------------------------------------------------------------------------- the request

function stringList(value, what) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || !value.every((v) => typeof v === 'string' && v.trim())) {
    throw bad(`${what} is a list of non-empty strings.`);
  }
  return value.map((v) => v.trim());
}

function startUrl(request) {
  let url;
  try {
    url = new URL(String(request.start || ''));
  } catch (_) {
    throw bad('The start is an http or https URL, such as http://127.0.0.1:4200/.');
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') throw bad('The start is an http or https URL.');
  if (!isLocal(url) && request.remote !== true) {
    throw bad(`${url.host} is not this Mac, so it is crawled only with --remote (and then only fixture or demo ` +
      'data). Nothing was written.');
  }
  return url;
}

function parseSteps(start, steps, remote) {
  try {
    return capture.normalize({ source: { url: start }, recipe: { steps }, remote }).steps;
  } catch (error) {
    throw bad(error.message);
  }
}

function normalizeCrawl(request) {
  const start = startUrl(request);
  const number = (value, fallback, what) => {
    const n = value === undefined || value === null ? fallback : value;
    if (!Number.isInteger(n) || n < 0) throw bad(`${what} is a whole number.`);
    return n;
  };
  const screens = request.screens && typeof request.screens === 'object' ? request.screens : {};
  const params = screens.params && typeof screens.params === 'object' ? screens.params : {};
  for (const [at, names] of Object.entries(params)) stringList(names, `The screen parameters of ${at}`);
  const req = {
    start, origin: start.origin, remote: request.remote === true,
    within: typeof request.within === 'string' && request.within.trim() ? request.within.trim() : null,
    depth: number(request.depth, 3, 'The depth limit'), max: number(request.max, 60, 'The page limit'),
    avoid: avoidWords(stringList(request.avoid, 'The avoid words')),
    exclude: stringList(request.exclude, 'The exclude globs'),
    screens: { params, overlays: stringList(screens.overlays, 'The overlay routes') },
    storageState: typeof request.storage_state === 'string' && request.storage_state ? request.storage_state : null,
    timeoutMs: number(request.timeout_s, 600, 'The time bound') * 1000,
    core: typeof request.core === 'string' && request.core ? request.core : null,
  };
  if (req.max < 1) throw bad('The page limit is 1 or more.');
  req.steps = parseSteps(start.href, stringList(request.steps, 'The steps'), req.remote);
  return req;
}

// --------------------------------------------------------------------------- the browser

async function openBrowser(req, contextOptions) {
  let pw;
  try {
    pw = capture.playwrightCore({ core: req.core });
  } catch (error) {
    throw new CrawlError('no-playwright', error.message);
  }
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'devkit-crawl-'));
  fs.chmodSync(folder, 0o700);
  const before = process.env.TMPDIR;
  process.env.TMPDIR = folder; // Playwright puts the profile under os.tmpdir()
  const done = async (browser) => {
    if (browser) await browser.close().catch(() => {});
    if (before === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = before;
    fs.rmSync(folder, { recursive: true, force: true });
  };
  let browser = null;
  try {
    const { chromium } = require(pw.path);
    browser = await chromium.launch({ channel: 'chromium', headless: true, chromiumSandbox: true,
      args: ['--disable-breakpad', '--disable-crash-reporter'] });
    const context = await browser.newContext({
      serviceWorkers: 'block', acceptDownloads: false, locale: 'en-US', timezoneId: 'UTC', reducedMotion: 'reduce',
      storageState: req.storageState || undefined, ...contextOptions,
    });
    return { browser, context, close: () => done(browser) };
  } catch (error) {
    await done(browser);
    if (error instanceof CrawlError) throw error;
    throw new CrawlError('no-browser', `The browser did not start: ${firstLine(error)}. Install it: ${FIX_INSTALL}.`);
  }
}

// What the crawler reads on a page, in the page: its heading, its links, and the controls it may click.
function readPage() {
  const text = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const nameOf = (el) => {
    const label = (el.getAttribute('aria-label') || '').trim();
    if (label) return label;
    const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map((id) => document.getElementById(id))
      .filter(Boolean).map(text).join(' ').trim();
    if (by) return by;
    if (el.tagName === 'INPUT') return (el.value || (el.type === 'submit' ? 'Submit' : '')).trim();
    const own = text(el);
    if (own) return own;
    const img = el.querySelector('img[alt]');
    return ((img && img.alt) || el.getAttribute('title') || '').trim();
  };
  const unique = (selector) => {
    try {
      return document.querySelectorAll(selector).length === 1;
    } catch (_) {
      return false;
    }
  };
  const quoted = (value) => JSON.stringify(value);
  const selectorOf = (el) => {
    if (el.id && unique(`#${CSS.escape(el.id)}`)) return `#${CSS.escape(el.id)}`;
    const testid = el.getAttribute('data-testid');
    if (testid && unique(`[data-testid=${quoted(testid)}]`)) return `[data-testid=${quoted(testid)}]`;
    const tag = el.tagName.toLowerCase();
    const href = el.getAttribute('href');
    if (href !== null && unique(`${tag}[href=${quoted(href)}]`)) return `${tag}[href=${quoted(href)}]`;
    const parts = [];
    for (let node = el; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
      if (node !== el && node.id && unique(`#${CSS.escape(node.id)}`)) {
        parts.unshift(`#${CSS.escape(node.id)}`);
        return parts.join(' > ');
      }
      let n = 1;
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === node.tagName) n += 1;
      parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${n})`);
    }
    return ['body', ...parts].join(' > ');
  };
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const h1 = document.querySelector('h1');
  const controls = 'button, input[type=submit], input[type=button], input[type=image], [role=button], [role=tab], ' +
    '[role=menuitem]';
  return {
    title: (h1 && text(h1)) || document.title.trim(),
    links: [...document.querySelectorAll('a[href], area[href]')]
      .map((a) => ({ href: a.href, name: nameOf(a), selector: selectorOf(a) })),
    controls: [...document.querySelectorAll(controls)]
      .filter((el) => !el.disabled && visible(el) && !el.closest('a[href]'))
      .map((el) => ({ name: nameOf(el), role: el.getAttribute('role') || 'button', selector: selectorOf(el),
        submit: Boolean(el.form && (el.type === 'submit' || el.type === 'image')) })),
  };
}

// --------------------------------------------------------------------------- the crawl

// The headers of an answer the response guard lets through: the app's own, with CONNECT_POLICY as one more
// Content-Security-Policy header beside any the app sent (the browser enforces each policy), once: an answer passes
// both the browser's guard and its page's. Chromium applies a header added here to a worker's script but not to a
// document, so connectPolicy gives each document the same policy.
function withPolicy(headers) {
  const own = headers || [];
  if (own.some((h) => h.name.toLowerCase() === 'content-security-policy' && h.value === CONNECT_POLICY)) return own;
  return [...own, { name: 'Content-Security-Policy', value: CONNECT_POLICY }];
}

// The crawl's init script, in each document before the page's own scripts run: `policy` as a <meta> policy, removed
// again at once (a policy a meta element delivered stays in force), so the document keeps the DOM the app built. The
// parser adds the head before it runs a script, and the observer's callback runs before that script.
function connectPolicy(policy) {
  const apply = () => {
    const meta = document.createElement('meta');
    meta.httpEquiv = 'Content-Security-Policy';
    meta.content = policy;
    document.head.prepend(meta);
    meta.remove();
  };
  if (document.head) {
    apply();
    return;
  }
  new MutationObserver((_, observer) => {
    const root = document.documentElement;
    if (root && root.namespaceURI !== 'http://www.w3.org/1999/xhtml') {
      observer.disconnect(); // an SVG or other XML document has no head to hold a policy
    } else if (document.head) {
      observer.disconnect();
      apply();
    }
  }).observe(document, { childList: true, subtree: true });
}

class Crawl {
  constructor(req, context) {
    this.req = req;
    this.context = context;
    this.pages = new WeakSet(); // the pages the crawler opened, each with its response guard
    this.nodes = new Map(); // key -> node
    this.edges = new Map(); // "from\0to" -> edge
    this.queue = [];
    this.queued = new Set(); // screen routes queued or opened
    this.left = new Set(); // keys not added because of a limit
    this.limits = new Set(); // the limits that left keys: "pages", "depth"
    this.resolved = new Map(); // url -> where its redirect chain ends
    this.blocked = [];
    this.blockedCount = 0;
    this.notClicked = [];
    this.excluded = 0;
    this.external = 0;
    this.websockets = 0;
    this.probe = null; // {page, target}: a click being probed, whose navigation is recorded and aborted
    this.timedOut = false;
    this.deadline = Date.now() + req.timeoutMs;
  }

  route(url) {
    return routeOf(url, this.req.screens);
  }

  // Why the crawl stays away from a same-origin URL: "avoid", "exclude", or null.
  stayAway(url, name) {
    const decode = (text) => {
      try {
        return decodeURIComponent(text);
      } catch (_) {
        return text;
      }
    };
    const where = `${decode(url.pathname)} ${decode(url.search.replace(/\+/g, ' '))}`;
    if (avoided(name || '', this.req.avoid) || avoided(where, this.req.avoid)) return 'avoid';
    if (excluded(url.pathname, this.req.exclude) || excluded(this.route(url.href), this.req.exclude)) return 'exclude';
    return null;
  }

  block(method, url) {
    this.blockedCount += 1;
    const entry = { method, route: this.route(url) };
    if (!this.blocked.some((b) => b.method === entry.method && b.route === entry.route)) this.blocked.push(entry);
  }

  // Whether a GET must stay in the browser: a document on another origin, or a path the crawl keeps away from.
  refused(url, document) {
    if (url.origin !== this.req.origin) return document;
    return Boolean(this.stayAway(url, ''));
  }

  async guard(route) {
    const request = route.request();
    let url = null;
    try {
      url = new URL(request.url());
    } catch (_) {
      url = null;
    }
    if (!url || (url.protocol !== 'http:' && url.protocol !== 'https:')) return route.continue();
    if (request.method() !== 'GET') {
      this.block(request.method(), request.url());
      return route.abort('blockedbyclient');
    }
    let page = null;
    try {
      page = request.frame().page(); // a worker's request comes with the frame that started the worker
    } catch (_) {
      page = null; // a new window's first navigation comes before its frame
    }
    if (!page || !this.pages.has(page)) return route.abort('blockedbyclient');
    const document = request.resourceType() === 'document';
    if (this.refused(url, document)) return route.abort('blockedbyclient');
    if (url.origin !== this.req.origin) return route.continue();
    const probe = this.probe;
    if (document && probe && page === probe.page && request.isNavigationRequest() &&
      request.frame() === page.mainFrame()) {
      probe.target = url.href;
      return route.abort('aborted');
    }
    return route.continue();
  }

  // A page with its response guard. Playwright routes only the first request of a redirect chain, so a CDP session's
  // Fetch domain pauses each response of the page, its frames, and its workers (respond).
  async newPage() {
    const page = await this.context.newPage();
    try {
      const session = await this.context.newCDPSession(page);
      session.on('Fetch.requestPaused', (event) => this.respond(session, event));
      await session.send('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Response' }] });
    } catch (error) {
      await page.close().catch(() => {});
      throw error;
    }
    this.pages.add(page);
    return page;
  }

  // A response a guard's Fetch domain paused: a redirect hop the crawl keeps away from (hopAllowed) fails there,
  // before the browser sends it. Every other answer goes on as the app sent it, with CONNECT_POLICY added to its
  // headers (withPolicy).
  respond(session, event) {
    const send = (method, params) => session.send(method, params).catch(() => {});
    const { requestId } = event;
    const status = event.responseStatusCode;
    if (event.responseErrorReason || !status) return send('Fetch.continueRequest', { requestId });
    const location = (event.responseHeaders || []).find((h) => h.name.toLowerCase() === 'location');
    if (REDIRECTS.has(status) && location) {
      if (this.hopAllowed(event, location.value)) return send('Fetch.continueRequest', { requestId });
      return send('Fetch.failRequest', { requestId, errorReason: 'BlockedByClient' });
    }
    return session.send('Fetch.continueResponse', { requestId, responseCode: status,
      responseHeaders: withPolicy(event.responseHeaders) }).catch(() => send('Fetch.continueRequest', { requestId }));
  }

  // The guard of the whole browser, for what no page sees. Chromium runs a shared worker in a target of its own,
  // outside the page that started it, so neither the context's route nor the page's CDP session sees its requests.
  // A browser CDP session's Fetch domain pauses every request of every target, and holds it to the page's rules
  // before it is sent: one that is not a GET fails and is counted, a document on another origin and a path the crawl
  // keeps away from fail; then its response goes through respond. A page's request comes here only after the page's
  // own guards let it through, so it passes again.
  async guardBrowser(browser) {
    const session = await browser.newBrowserCDPSession();
    const send = (method, params) => session.send(method, params).catch(() => {});
    session.on('Fetch.requestPaused', (event) => {
      if (event.responseStatusCode || event.responseErrorReason) return this.respond(session, event);
      const { requestId, request } = event;
      let url = null;
      try {
        url = new URL(request.url);
      } catch (_) {
        url = null;
      }
      const go = () => send('Fetch.continueRequest', { requestId });
      const stop = () => send('Fetch.failRequest', { requestId, errorReason: 'BlockedByClient' });
      if (!url || (url.protocol !== 'http:' && url.protocol !== 'https:')) return go();
      if (request.method !== 'GET') {
        this.block(request.method, request.url);
        return stop();
      }
      return this.refused(url, event.resourceType === 'Document') ? stop() : go();
    });
    await session.send('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' },
      { urlPattern: '*', requestStage: 'Response' }] });
  }

  // Whether the browser may follow a redirect of a page's request. A document's never: the crawl walks a page's
  // redirect chain itself (resolve). Any other hop to the app's origin only when the avoid words and --exclude allow
  // its path, and a hop to another origin only from another origin, where the request already was.
  hopAllowed(event, location) {
    if (event.resourceType === 'Document') return false;
    let from = null;
    let next = null;
    try {
      from = new URL(event.request.url);
      next = new URL(location, from);
    } catch (_) {
      return false;
    }
    if (next.origin === this.req.origin) return !this.stayAway(next, '');
    return from.origin !== this.req.origin;
  }

  addNode(key, node) {
    if (this.nodes.has(key)) return this.nodes.get(key);
    if (this.nodes.size >= this.req.max) {
      this.left.add(key);
      this.limits.add('pages');
      return null;
    }
    const record = { key, kind: 'screen', route: key, title: key, example: null, status: null, skipped: null, ...node };
    this.nodes.set(key, record);
    return record;
  }

  addEdge(from, to, kind, trigger, status, redirects) {
    const id = `${from}\u0000${to}`;
    if (!this.edges.has(id)) this.edges.set(id, { from, to, kind, trigger, status, redirects });
  }

  // Where a same-origin URL's redirect chain ends, one GET at a time with maxRedirects 0, so no hop to another origin
  // or to an avoided path is ever requested: {url, status, redirects, external?, skipped?, error?}
  async resolve(start) {
    if (this.resolved.has(start)) return this.resolved.get(start);
    let url = start;
    let redirects = 0;
    let out = null;
    while (!out) {
      const at = new URL(url);
      const away = at.origin === this.req.origin ? this.stayAway(at, '') : null;
      if (at.origin !== this.req.origin) {
        out = { url, status: null, redirects, external: true };
      } else if (away) {
        out = { url, status: null, redirects, skipped: away };
      } else if (redirects > MAX_REDIRECTS) {
        out = { url, status: null, redirects, error: `more than ${MAX_REDIRECTS} redirects` };
      } else {
        let response = null;
        try {
          response = await this.context.request.get(url, { maxRedirects: 0, failOnStatusCode: false,
            timeout: LOAD_TIMEOUT_MS, headers: { Accept: ACCEPT } });
        } catch (error) {
          out = { url, status: null, redirects, error: firstLine(error) };
        }
        if (response) {
          const status = response.status();
          const location = response.headers().location;
          await response.dispose().catch(() => {});
          if (status >= 300 && status < 400 && location) {
            url = new URL(location, url).href;
            redirects += 1;
          } else {
            out = { url, status, redirects };
          }
        }
      }
    }
    this.resolved.set(start, out);
    return out;
  }

  externalEdge(from, url, trigger, redirects) {
    this.external += 1;
    const origin = new URL(url).origin;
    if (this.addNode(origin, { kind: 'external', route: origin, title: new URL(url).host, example: url })) {
      this.addEdge(from, origin, 'external', trigger, null, redirects);
    }
  }

  // One link or clicked control from the screen `from` (its route) to `url`.
  async follow(from, url, trigger, kind, depth) {
    const fromKey = from && this.nodes.has(from) ? from : null;
    if (url.origin !== this.req.origin) {
      if (fromKey) this.externalEdge(fromKey, url.href, trigger, 0);
      return;
    }
    const first = this.route(url.href);
    if (first === from) return;
    const away = this.stayAway(url, trigger.name);
    if (away) {
      if (away === 'avoid') this.noteAvoided(trigger.name || first);
      else this.excluded += 1;
      if (fromKey && within(first, this.req.within) && this.addNode(first, { example: url.href, skipped: away })) {
        this.addEdge(fromKey, first, kind, trigger, null, 0);
      }
      return;
    }
    if (!within(first, this.req.within)) return;
    const end = await this.resolve(url.href);
    if (end.external) {
      if (fromKey) this.externalEdge(fromKey, end.url, trigger, end.redirects);
      return;
    }
    const route = this.route(end.url);
    if (route === from || !within(route, this.req.within)) return;
    if (end.skipped) {
      if (end.skipped === 'avoid') this.noteAvoided(trigger.name || route);
      if (fromKey && this.addNode(route, { example: end.url, skipped: end.skipped })) {
        this.addEdge(fromKey, route, kind, trigger, null, end.redirects);
      }
      return;
    }
    if (fromKey) this.addEdge(fromKey, route, kind, trigger, end.status, end.redirects);
    if (end.status === null || end.status >= 400) {
      const why = end.status === null ? 'did not load' : `answered ${end.status}`;
      this.addNode(route, { example: end.url, status: end.status, skipped: why });
      return;
    }
    if (this.queued.has(route)) return;
    if (depth + 1 > this.req.depth) {
      this.left.add(route);
      this.limits.add('depth');
      return;
    }
    this.queued.add(route);
    this.queue.push({ url: end.url, route, depth: depth + 1, status: end.status });
  }

  noteAvoided(name) {
    if (!this.notClicked.includes(name)) this.notClicked.push(name);
  }

  async load(page, url) {
    await page.goto(url, { waitUntil: 'load', timeout: LOAD_TIMEOUT_MS });
    await page.waitForLoadState('networkidle', { timeout: IDLE_TIMEOUT_MS }).catch(() => {});
  }

  // Click one control on a fresh load of its page; -> the URL of the navigation it started, or null.
  async probeControl(url, control) {
    const page = await this.newPage();
    try {
      await this.load(page, url);
      this.probe = { page, target: null };
      const started = page.waitForRequest((r) => r.isNavigationRequest() && r.frame() === page.mainFrame(),
        { timeout: PROBE_WAIT_MS }).catch(() => null);
      await page.locator(control.selector).first().click({ timeout: CLICK_TIMEOUT_MS }).catch(() => {});
      await started;
      return this.probe.target;
    } catch (_) {
      return null;
    } finally {
      this.probe = null;
      await page.close().catch(() => {});
    }
  }

  async open(item) {
    const inside = within(item.route, this.req.within);
    const node = inside ? this.addNode(item.route, { example: item.url, status: item.status }) : null;
    if (inside && !node) return;
    const page = await this.newPage();
    let read = null;
    try {
      await this.load(page, item.url);
      if (item.depth === 0) {
        for (const step of this.req.steps) await capture.runStep(page, step);
      }
      if (new URL(page.url()).origin !== this.req.origin) {
        read = null;
      } else {
        read = await page.evaluate(readPage);
      }
    } catch (error) {
      if (error instanceof capture.CaptureError) throw new CrawlError('step', `${error.message} Nothing was written.`);
      read = null;
    } finally {
      await page.close().catch(() => {});
    }
    if (!read) {
      if (node) node.skipped = 'did not load';
      return;
    }
    if (node) node.title = read.title || item.route;
    const from = node ? item.route : null;
    for (const link of read.links) {
      let url = null;
      try {
        url = new URL(link.href);
      } catch (_) {
        url = null;
      }
      if (!url || (url.protocol !== 'http:' && url.protocol !== 'https:')) continue;
      await this.follow(from, url, { role: 'link', name: link.name || null, selector: link.selector }, 'nav', item.depth);
    }
    let clicked = 0;
    for (const control of read.controls) {
      if (avoided(control.name, this.req.avoid)) {
        this.noteAvoided(control.name);
        continue;
      }
      if (clicked >= MAX_CONTROLS || Date.now() > this.deadline) break;
      clicked += 1;
      const target = await this.probeControl(item.url, control);
      if (target) {
        await this.follow(from, new URL(target), { role: control.role, name: control.name || null,
          selector: control.selector }, control.submit ? 'submit' : 'action', item.depth);
      }
    }
  }

  async run() {
    const start = this.req.start.href;
    const startRoute = this.route(start);
    if (this.stayAway(this.req.start, '')) {
      throw bad(`The start URL's path holds an avoided word or an excluded path (${startRoute}). Start from another ` +
        'page. Nothing was written.');
    }
    const end = await this.resolve(start);
    if (end.external) {
      throw new CrawlError('start', `${redact.redactUrl(start).url} redirects to another origin ` +
        `(${new URL(end.url).origin}). Start from a page on ${this.req.origin}. Nothing was written.`);
    }
    if (end.skipped) throw bad(`${redact.redactUrl(start).url} redirects to an avoided or excluded path. Nothing was written.`);
    if (end.status === null || end.status >= 400) {
      const said = end.status === null ? `did not answer (${end.error || 'no response'})` : `answered ${end.status}`;
      throw new CrawlError('start', `${redact.redactUrl(start).url} ${said}. Start the app, then crawl again. ` +
        'Nothing was written.');
    }
    const first = this.route(end.url);
    this.queued.add(first);
    this.queue.push({ url: end.url, route: first, depth: 0, status: end.status });
    while (this.queue.length) {
      if (Date.now() > this.deadline) {
        this.timedOut = true;
        for (const item of this.queue) this.left.add(item.route);
        break;
      }
      await this.open(this.queue.shift());
    }
    return within(first, this.req.within) ? first : null;
  }

  result(start) {
    const keys = new Set(this.nodes.keys());
    const edges = [...this.edges.values()].filter((e) => keys.has(e.from) && keys.has(e.to));
    const left = [...this.left].filter((k) => !keys.has(k)).length;
    // a screen's example is its path, query, and fragment: the origin is the map's base, and changes with the port
    const nodes = [...this.nodes.values()].map((n) => {
      if (n.kind !== 'screen' || !n.example) return n;
      const url = new URL(n.example);
      return { ...n, example: url.pathname + url.search + url.hash };
    });
    return {
      start: start && keys.has(start) ? start : null, origin: this.req.origin, nodes, edges,
      partial: left > 0 || this.timedOut, left, depth: this.req.depth, timed_out: this.timedOut,
      stopped_by: this.timedOut ? 'time' : (left ? ['pages', 'depth'].find((l) => this.limits.has(l)) : null) || null,
      timeout_s: this.req.timeoutMs / 1000, blocked: this.blocked, blocked_count: this.blockedCount,
      not_clicked: this.notClicked, excluded: this.excluded, external: this.external, websockets: this.websockets,
    };
  }
}

// Every string of a result through redact.cjs: strip, gitleaks, the URL rules. Equal strings stay equal, so node
// keys still join edges to nodes.
function redactResult(result) {
  const slots = [];
  const visit = (value) => {
    if (Array.isArray(value)) {
      value.forEach((item, i) => (typeof item === 'string' ? slots.push([value, i]) : visit(item)));
    } else if (value && typeof value === 'object') {
      for (const key of Object.keys(value)) {
        if (typeof value[key] === 'string') slots.push([value, key]);
        else visit(value[key]);
      }
    }
  };
  visit(result);
  const out = redact.redactStrings(slots.map(([holder, key]) => holder[key]));
  slots.forEach(([holder, key], i) => {
    holder[key] = out[i];
  });
  return result;
}

async function crawl(request) {
  const req = normalizeCrawl(request);
  const session = await openBrowser(req, { viewport: { width: 1280, height: 800 } });
  try {
    const state = new Crawl(req, session.context);
    await state.guardBrowser(session.browser);
    await session.context.routeWebSocket(/.*/, (ws) => {
      state.websockets += 1;
      ws.close();
    });
    await session.context.addInitScript(connectPolicy, CONNECT_POLICY);
    await session.context.route('**/*', (route) => state.guard(route));
    const start = await state.run();
    return redactResult(state.result(start));
  } finally {
    await session.close();
  }
}

// --------------------------------------------------------------------------- the walk

function stepText(step) {
  return step.shown.replace(/^Step [0-9]+, /, '');
}

function stepError(step, error) {
  const message = String(error.message || '');
  for (const prefix of [`${step.shown}, `, `${step.shown}: `]) {
    if (message.startsWith(prefix)) return message.slice(prefix.length).replace(/\.$/, '');
  }
  return message.replace(/\.$/, '');
}

// Whether each kept value is on the page: its text, its form values, and its URL, in every frame. Only true or false
// leaves this function.
async function present(page, keeps) {
  const parts = [page.url()];
  try {
    parts.push(decodeURIComponent(page.url()));
  } catch (_) {
    // a URL that does not decode is compared as it is
  }
  for (const frame of page.frames()) {
    try {
      parts.push(await frame.evaluate(() => [document.body ? document.body.innerText : '',
        ...[...document.querySelectorAll('input, textarea, select')].map((el) => el.value)].join('\n')));
    } catch (_) {
      // a frame that went away holds nothing
    }
  }
  const text = parts.join('\n');
  const out = {};
  for (const [name, value] of Object.entries(keeps)) out[name] = value === null ? null : text.includes(value);
  return out;
}

// The screen a walk step reached: its route and its heading.
async function where(page, screens) {
  const title = await page.evaluate(() => {
    const h1 = document.querySelector('h1');
    const text = h1 ? (h1.innerText || h1.textContent || '').replace(/\s+/g, ' ').trim() : '';
    return text || document.title.trim();
  }).catch(() => '');
  return { route: routeOf(page.url(), screens), title: title || null };
}

async function settle(page) {
  await page.waitForLoadState('load', { timeout: LOAD_TIMEOUT_MS }).catch(() => {});
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))))
    .catch(() => {});
}

function navigation(responses) {
  const last = [...responses].reverse().find((r) => r.status() < 300 || r.status() >= 400) || responses[responses.length - 1];
  if (!last) return { status: null, redirects: 0 };
  let redirects = 0;
  for (let r = last.request().redirectedFrom(); r; r = r.redirectedFrom()) redirects += 1;
  return { status: last.status(), redirects };
}

async function walk(request) {
  const start = startUrl(request);
  const remote = request.remote === true;
  if (!Array.isArray(request.steps) || !request.steps.every(Array.isArray)) {
    throw bad('The walk takes its steps as one list of recipe steps for each edge of the journey.');
  }
  const edges = request.steps.map((steps) => parseSteps(start.href, stringList(steps, 'The steps'), remote));
  const keeps = request.keeps && typeof request.keeps === 'object' ? request.keeps : {};
  const screens = request.screens && typeof request.screens === 'object' ? request.screens : {};
  if (!Object.values(keeps).every((v) => v === null || typeof v === 'string')) {
    throw bad('Each kept value is a string, or null when the journey does not say it.');
  }
  const viewport = Array.isArray(request.viewport) ? request.viewport : [1280, 800];
  const req = { core: typeof request.core === 'string' && request.core ? request.core : null,
    storageState: typeof request.storage_state === 'string' && request.storage_state ? request.storage_state : null };
  const session = await openBrowser(req, { viewport: { width: viewport[0], height: viewport[1] },
    colorScheme: request.theme === 'dark' ? 'dark' : 'light' });
  try {
    const page = await session.context.newPage();
    const responses = [];
    page.on('response', (r) => {
      const q = r.request();
      if (q.isNavigationRequest() && q.frame() === page.mainFrame()) responses.push(r);
    });
    const steps = [];
    let response;
    try {
      response = await page.goto(start.href, { waitUntil: 'load', timeout: LOAD_TIMEOUT_MS });
    } catch (error) {
      throw new CrawlError('start', `${redact.redactUrl(start.href).url} did not answer (${firstLine(error)}). Start ` +
        'the app, then walk again.');
    }
    await settle(page);
    steps.push({ step: 1, ...(await where(page, screens)), ...navigation(response ? [response] : []),
      keeps: await present(page, keeps) });
    let broken = null;
    for (let i = 0; i < edges.length && !broken; i += 1) {
      responses.length = 0;
      for (const step of edges[i]) {
        try {
          await capture.runStep(page, step);
        } catch (error) {
          if (!(error instanceof capture.CaptureError)) throw error;
          broken = { step: i + 2, do: stepText(step), error: stepError(step, error) };
          break;
        }
      }
      if (broken) break;
      await settle(page);
      const moved = responses.length ? navigation(responses) : { status: null, redirects: 0 };
      steps.push({ step: i + 2, ...(await where(page, screens)), ...moved, keeps: await present(page, keeps) });
    }
    return redactResult({ steps, broken });
  } finally {
    await session.close();
  }
}

// --------------------------------------------------------------------------- command line

async function main(argv) {
  if (argv.length !== 1 || argv[0] !== '--json') {
    process.stderr.write('Usage: node crawl.cjs --json < request   (see the header of crawl.cjs)\n');
    return 2;
  }
  let request = null;
  try {
    request = JSON.parse(fs.readFileSync(0, 'utf8'));
  } catch (_) {
    request = null;
  }
  if (!request || typeof request !== 'object' || !['crawl', 'walk'].includes(request.mode)) {
    process.stderr.write('✗ crawl.cjs --json takes a JSON request whose mode is crawl or walk.\n');
    return 2;
  }
  try {
    const out = request.mode === 'crawl' ? await crawl(request) : await walk(request);
    process.stdout.write(`${JSON.stringify(out)}\n`);
    return 0;
  } catch (error) {
    if (error instanceof CrawlError || error instanceof redact.RedactionError) {
      process.stderr.write(`✗ ${error.message}\n`);
      return error.code === 'bad-request' ? 2 : 1;
    }
    process.stderr.write(`✗ The ${request.mode} failed: ${firstLine(error)}\n`);
    return 1;
  }
}

module.exports = { crawl, walk, avoided, avoidWords, excluded, routeOf, CrawlError };

if (require.main === module) main(process.argv.slice(2)).then((code) => { process.exitCode = code; });
