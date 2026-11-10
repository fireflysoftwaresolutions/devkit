
(() => {
"use strict";
window.addEventListener("error", e => { const b = document.createElement("div"); b.className = "err"; b.id = "dk-error";
  b.textContent = "Dashboard error: " + (e.message || e.error); document.body.append(b); });
window.addEventListener("unhandledrejection", e => { const b = document.createElement("div"); b.className = "err"; b.id = "dk-error";
  b.textContent = "Dashboard error: " + (e.reason && e.reason.message || e.reason); document.body.append(b); });
let D = JSON.parse(document.getElementById("devkit-data").textContent);   /* live mode swaps it for a newer snapshot */
/* the views, by how often a day of work needs them, then the reference views (the first of those starts the second group) */
const VIEWS = [["now", "Now", "n"], ["tasks", "Tasks", "t"], ["background", "Background", "b"], ["team", "Team", "m"], ["projects", "Projects", "p"], ["initiatives", "Initiatives", "i"], ["activity", "Activity", "a"], ["explorer", "Explorer", "e"], ["health", "Health", "h"]];
const REFVIEW = "activity";
const ROUTES = VIEWS.map(v => v[0]).concat(["search"]);
const DEFAULT = {view: "now", id: "", tab: "", task: "", q: "", status: [], priority: [], owner: [], stack: [], tag: [], ci: [], health: [],
  live: false, prs: false, nc: false, loose: false, sort: "attention", group: "none", layout: "cards", dense: false, f: "", fq: "",
  range: 7, types: [], tp: "", tmode: "board", who: "", tst: "", ist: "", psort: "priority", ilayout: "cards", mq: "", mg: "", src: "all", kind: "",
  cq: "", rq: "", rm: "", rall: false, modal: "", all: false};
const PAGEKEYS = {tab: "", task: "", modal: "", mq: "", mg: "", src: "all", tst: "", cq: "", rq: "", rm: "", rall: false, all: false};
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const SVGNS = "http://www.w3.org/2000/svg";
const store = {get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
               set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }};

/* ---------------------------------------------------------------- DOM helpers (textContent only) */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) for (const k in props) {
    const v = props[k];
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
    else if (k === "href") el.setAttribute("href", safeUrl(v));
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  return append(el, kids);
}
function append(el, kids) {
  for (const c of kids.flat(Infinity)) { if (c == null || c === false) continue; el.append(c.nodeType ? c : document.createTextNode(String(c))); }
  return el;
}
function sv(tag, attrs, ...kids) { const el = document.createElementNS(SVGNS, tag); for (const k in attrs || {}) if (attrs[k] != null) el.setAttribute(k, attrs[k]); return append(el, kids); }
function safeUrl(u) { u = String(u || "").trim(); return /^(https?:|mailto:|#)/i.test(u) ? u : "#"; }
// a URL from the data opens in a new tab only when safeUrl lets it through; anything else (javascript:, data:) opens nothing
function openUrl(u) { const s = safeUrl(u); if (s !== "#") window.open(s, "_blank", "noopener"); }
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function frag(...kids) { return append(document.createDocumentFragment(), kids); }

/* ---------------------------------------------------------------- time & format */
const nowS = () => Date.now() / 1000;
function rel(ts) {
  if (!ts) return "—"; const d = Math.max(0, nowS() - ts);
  if (d < 60) return "just now"; if (d < 3600) return Math.floor(d / 60) + " min ago";
  if (d < 86400) return Math.floor(d / 3600) + " h ago"; if (d < 86400 * 14) return Math.floor(d / 86400) + " d ago";
  return Math.floor(d / 604800) + " w ago";
}
function dur(s) { s = Math.max(0, s); if (s < 3600) return Math.max(1, Math.floor(s / 60)) + " min"; if (s < 86400) return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min"; return Math.floor(s / 86400) + " d"; }
function fmt(ts) { return ts ? new Date(ts * 1000).toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "—"; }
function fmtDay(ts) { return ts ? new Date(ts * 1000).toLocaleDateString(undefined, {weekday: "short", month: "short", day: "numeric"}) : "—"; }
function tm(ts) { const d = new Date(ts * 1000); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
function lkey(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
function days(n) { const out = [], b = new Date(D.generated * 1000); b.setHours(12, 0, 0, 0); for (let i = n - 1; i >= 0; i--) { const d = new Date(b); d.setDate(b.getDate() - i); out.push(lkey(d)); } return out; }
function sessTs(line) { const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})/.exec(line || ""); return m ? new Date(m[1] + "T" + m[2] + ":" + m[3] + ":00").getTime() / 1000 : 0; }
function sessText(s) { return String(s || "").slice(16).replace(/^\s*·\s*/, ""); }
function shortDay(k) { return new Date(k + "T12:00:00").toLocaleDateString(undefined, {month: "short", day: "numeric"}); }
function compact(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? Math.round(n / 1e3) + "K" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n); }
function kb(k) { if (k == null) return "—"; return k >= 1048576 ? (k / 1048576).toFixed(1) + " GB" : k >= 1024 ? Math.round(k / 1024) + " MB" : k + " KB"; }
function plural(n, w, p) { return n + " " + (n === 1 ? w : (p || w + "s")); }
function stripMarker(line) { return String(line || "").replace(/^[\W_]*(?:\[\s*firefly\s*>\s*devkit\s*\]|devkit(?=[\s·:—*`]))[\s·:—*`]*/i, "").replace(/(status:\s*)(?:\*\*|`)?(?:\[\s*firefly\s*>\s*devkit\s*\]|devkit\s*·)(?:\*\*|`)?\s*/gi, "$1"); }
function iso(s) { const t = Date.parse(s || ""); return isNaN(t) ? 0 : t / 1000; }
/* the window behind a count: the guard log keeps the last N decisions, the session log two months, commits 30 days */
function guardWindow() { return (W.guard_capped ? ", the last " + W.guard_events : "") + (W.guard_since ? ", since " + shortDay(String(W.guard_since).slice(0, 10)) : ""); }
function fmtMin(m) { m = Math.max(0, Math.round(m || 0)); return m >= 60 ? Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") + "m" : m + "m"; }
/* an estimate or a time left: to 5 minutes, under 10 minutes to the minute, so "about" claims no more precision than an
   estimate has; measured time (active, wall, elapsed, over the estimate) keeps fmtMin */
function fmtAbout(m) { const r = Math.max(0, Math.round(m || 0)); return fmtMin(r < 10 ? r : Math.round(m / 5) * 5); }

/* ---------------------------------------------------------------- data model: work items, projects, initiatives, people */
const OPEN = ["todo", "doing", "blocked"];
const ME = D.who || "";
/* [unused, label]: status is drawn as a dot (projects, initiatives) or an SVG icon (tasks), never as a glyph */
const STATUS = {active: ["", "Active"], paused: ["", "Paused"], blocked: ["", "Blocked"], done: ["", "Done"], archived: ["", "Archived"], untracked: ["", "No note"], unknown: ["", "No note"]};
const PRIO = {high: ["", "High"], normal: ["", "Normal"], low: ["", "Low"]};
const PRANK = {high: 0, normal: 1, low: 2}, HRANK = {risk: 0, warn: 1, ok: 2};
const KIND = {"ci-failing": "CI", "changes-requested": "Review", "conflicts": "Conflicts", "review-waiting": "Review", "review-requested": "Review",
  "blocked": "Blocked", "blocked-task": "Blocked", "overdue": "Overdue", "idle-work": "Unsaved work", "reclaim": "Reclaim", "layout": "Layout", "stale-note": "Handoff",
  "drift": "Config", "sign-in": "Sign in", "collision": "Collision", "expired-lock": "Lock", "decision": "Decision", "ci-watch": "CI watch", "job-failed": "Job", "setup": "Setup"};
const CHECKS = {passing: "CI passing", failing: "CI failing", pending: "CI running", none: "no CI", unknown: "CI unknown"};
const ISTATUS = {todo: ["", "To do"], doing: ["", "Doing"], blocked: ["", "Blocked"], done: ["", "Done"], cancelled: ["", "Cancelled"]};
const SDOT = {active: "d-ok", paused: "d-warn", blocked: "d-risk", done: "d-info", archived: "d-info", untracked: "d-info", unknown: "d-info"};
const SORDER = {blocked: 0, active: 1, paused: 2, untracked: 3, unknown: 3, done: 4, archived: 5};
function whoOf(s) { return String(s || "").split(" ")[0]; }
function agentOf(s) { const m = /\(([^:)]+)/.exec(String(s || "")); return m ? m[1] : ""; }
function isMe(w) { return !!w && w === ME; }
function isInit(scope) { return String(scope).startsWith("initiatives/"); }
function flagOf(scope) { return isInit(scope) ? "-i " + scope.slice(12) : "-p " + scope; }
function cnt(items) { const c = {open: 0, todo: 0, doing: 0, blocked: 0, done: 0, cancelled: 0, total: items.length}; for (const x of items) { if (x.open) c.open++; if (c[x.status] != null) c[x.status]++; } return c; }

let ITEMS, ITEMBY, BYSCOPE, P, BYID, PEOPLE, BYWHO, ONPROJ, RUNS, INITS, BYINIT, MEMBEROF, FILES, FILEMAP, ACTIONS, W;
let CREW, CREWBY, RACI, JOBS, CIW, RRUNS, CACHE = {};   /* CACHE: values computed once per snapshot (needs, contradictions) */
/* scopes without work items still show their note checkboxes (legacy) as read-only items */
function legacy(scope, t, tidx) {
  const out = []; let k = 0;
  for (const [bucket, list, status] of [["now", t.now, "doing"], ["next", t.next, "todo"], ["later", t.later, "todo"], ["later", t.done_recent, "done"]]) for (const raw of list || []) {
    const it = {id: "note:" + scope + "#" + (++k), legacy: true, n: status === "done" ? 0 : (tidx[raw] || 0), type: "task", scope, status, bucket, priority: "normal",
      due: (/\bdue:(\d{4}-\d{2}-\d{2})\b/.exec(raw) || [])[1] || "", title: String(raw).replace(/\s*\bdue:\S+/, "").replace(/\s*✓\s*\d{4}-\d{2}-\d{2}/, ""),
      claimed_by: "", lock: "", lock_expires: "", changelog: [], version: 0, holder: "", agent: "", open: status !== "done", links: [], body: ""};
    out.push(it); ITEMBY.set(it.id, it);
  }
  return out;
}
function runOf(s) { return RUNS.find(r => r.session && r.session === s.session && r.agent === s.agent) || null; }
function peopleOn(scopes, items) { const out = new Set(); for (const sc of scopes) for (const s of ONPROJ.get(sc) || []) out.add(s.who); for (const it of items) if (it.open && it.holder && it.lock !== "expired") out.add(it.holder); return Array.from(out); }
function allItems() { const out = ITEMS.slice(); for (const p of P) if (p.items.length && p.items[0].legacy) out.push(...p.items); for (const x of INITS) if (x.items.length && x.items[0].legacy) out.push(...x.items); return out; }
function progOf(x) {
  const t = x.t || {}, done = t.done || 0, total = (t.open || 0) + done;
  if (x.progress != null && x.progress !== "" && !isNaN(+x.progress)) {
    const src = x.progress_source === "manual" ? "manual" : "tasks", pct = Math.max(0, Math.min(100, Math.round(+x.progress)));
    /* only quote counts that actually produce the percentage */
    return {pct, src, why: src === "manual" ? "Set manually in the note" : total && Math.round(100 * done / total) === pct ? done + " of " + total + " tasks done" : "Computed from the tasks"};
  }
  if (total) return {pct: Math.round(100 * done / total), src: "tasks", why: done + " of " + total + " tasks done"};
  return {pct: null, src: "none", why: "No tasks and no manual progress yet"};
}
function progressBar(x, wide) {
  const pr = progOf(x);
  if (pr.pct == null) return h("span", {class: "prog none muted", title: pr.why, text: "no tasks"});
  return h("span", {class: "prog" + (wide ? " wide" : ""), title: pr.why},
    h("span", {class: "pbar", role: "progressbar", "aria-valuenow": pr.pct, "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Progress " + pr.pct + "%, " + (pr.src === "manual" ? "set manually" : "from tasks")}, h("i", {style: "width:" + pr.pct + "%"})),
    h("span", {class: "num pv", text: pr.pct + "%"}));
}
function dueBadge(due, done) { if (!due) return null; const over = !done && due < lkey(new Date());
  return h("span", {class: "due" + (over ? " over" : ""), title: over ? "Past its due date (" + due + ")" : "Due " + due, text: over ? "overdue since " + shortDay(due) : "due " + shortDay(due)}); }
/* everything derived from the data, in one place: live mode re-runs it when a newer snapshot arrives */
function derive() {
  W = D.windows || {};
  ITEMS = (D.items || []).map(x => Object.assign({}, x, {holder: whoOf(x.claimed_by), agent: agentOf(x.claimed_by), open: OPEN.includes(x.status)}));
  ITEMBY = new Map(ITEMS.map(x => [x.id, x]));
  BYSCOPE = new Map();
  for (const x of ITEMS) { if (!BYSCOPE.has(x.scope)) BYSCOPE.set(x.scope, []); BYSCOPE.get(x.scope).push(x); }
  P = (D.projects || []).map((p, i) => {
    const ns = p.note_summary || null;
    const t = (ns && ns.tasks) || {now: [], next: [], later: [], done_recent: [], open: 0, done: 0, overdue: []};
    const id = p.id || p.name || "project-" + i;
    const owner = id.includes("/") ? id.slice(0, id.lastIndexOf("/")) : "(local)";
    const wts = p.worktrees || [], prsOpen = p.prs_open || [];
    const live = (p.main && p.main.active ? 1 : 0) + wts.filter(w => w.active).length;
    const o = Object.assign({}, p, {id, ns, t, owner, wts, live, status: ns ? (ns.status || "active") : "untracked", priority: (ns && ns.priority) || "normal", tags: (ns && ns.tags) || [],
      stack: p.stack || [], prsOpen, prs: prsOpen.length, act: p.activity || {}, sessions: (ns && ns.sessions) || [], health: p.health || {level: "ok", reasons: []},
      ci: Array.from(new Set(prsOpen.map(r => r.checks || "unknown"))), tidx: p.task_index || {}, local: p.local !== false, inits: p.initiatives || []});
    o.items = BYSCOPE.get(id) || legacy(id, t, o.tidx);
    o.cnt = cnt(o.items);
    o.hay = [id, p.name, o.stack.join(" "), (p.main || {}).branch, o.tags.join(" "), ns && ns.purpose, ns && ns.state, o.items.filter(x => x.open).map(x => x.id + " " + x.title).join(" "),
      p.status_line && p.status_line.line, prsOpen.map(r => "#" + r.number + " " + r.title).join(" ")].filter(Boolean).join(" \n ").toLowerCase();
    return o;
  });
  BYID = new Map(P.map(p => [p.id, p]));
  PEOPLE = (D.people || []).map(x => Object.assign({}, x, {sessions: x.sessions || [], locks: x.locks || [], claimed: x.claimed || [], recent: x.recent || []}));
  if (!PEOPLE.length) { const live = (D.sessions || []).filter(s => s.active); if (live.length) PEOPLE.push({who: ME || D.host, user: ME.split("@")[0], host: D.host, updated: D.generated, sessions: live, locks: [], claimed: [], recent: []}); }
  BYWHO = new Map(PEOPLE.map(x => [x.who, x]));
  ONPROJ = new Map();
  for (const x of PEOPLE) for (const s of x.sessions) if (s.project) { if (!ONPROJ.has(s.project)) ONPROJ.set(s.project, []); ONPROJ.get(s.project).push(Object.assign({who: x.who, asOf: x.updated}, s)); }
  RUNS = (Array.isArray(D.runs) ? D.runs : []).filter(r => !r.machine);
  for (const p of P) p.people = peopleOn([p.id], p.items);
  INITS = (D.initiatives || []).map(x => {
    const t = x.tasks || {};
    const tk = {now: t.now || [], next: t.next || [], later: t.later || [], done_recent: t.done_recent || [], open: t.open || 0, done: t.done || 0, overdue: t.overdue || []};
    const scope = "initiatives/" + x.id, linked = (x.linked_items || []).map(id => ITEMBY.get(id)).filter(Boolean);
    const items = linked.length || BYSCOPE.has(scope) ? linked : legacy(scope, tk, x.task_index || {});
    const projects = x.projects || [], members = x.members || [];
    const last = Math.max(x.last_activity || 0, ...projects.map(id => (BYID.get(id) || {}).last_activity || 0));
    return Object.assign({}, x, {t: tk, scope, scopeText: x.scope || "", items, cnt: cnt(items), members, projects, tidx: x.task_index || {}, status: x.status || "active", priority: x.priority || "normal",
      title: x.title || x.id, tags: x.tags || [], sessions: x.sessions || [], last, people: peopleOn(projects.concat([scope]), items),
      hay: [x.id, x.title, x.goal, x.scope, x.state, projects.join(" "), items.filter(i => i.open).map(i => i.id + " " + i.title).join(" "), (x.tags || []).join(" ")].filter(Boolean).join(" \n ").toLowerCase()});
  });
  BYINIT = new Map(INITS.map(x => [x.id, x]));
  MEMBEROF = new Map();
  for (const x of INITS) for (const id of x.projects) { if (!MEMBEROF.has(id)) MEMBEROF.set(id, []); MEMBEROF.get(id).push(x.id); }
  FILES = D.files || [];
  FILEMAP = new Map(FILES.map(f => [f.path, f]));
  ACTIONS = D.actions || [];
  CREW = (D.crew || {}).members || [];
  RACI = (D.crew || {}).activities || [];
  CREWBY = new Map();
  for (const m of CREW) for (const k of [m.id, m.name]) if (k) CREWBY.set(String(k).toLowerCase(), m);
  JOBS = D.jobs || [];
  CIW = Object.assign({watches: [], watching: [], complete: true, total: 0}, D.ci || {});
  /* runs on other machines reach this Mac only as their presence summary */
  const host = (ME.split("@")[1] || D.host || "").toLowerCase(), local = new Set(RUNS.map(r => r.agent + "~" + r.session));
  RRUNS = [];
  for (const x of PEOPLE) for (const s of x.sessions) if (s.progress && String(x.host || "").toLowerCase() !== host && !local.has(s.agent + "~" + s.session))
    RRUNS.push(Object.assign({}, s.progress, {id: "remote~" + x.who + "~" + s.agent + "~" + s.session, remote: true, host: x.host, who: x.who, agent: s.agent, session: s.session, project: s.project, branch: s.branch, asOf: x.updated, decisions: [], questions: []}));
  CACHE = {};
}
derive();
/* ---------------------------------------------------------------- crew, runs, jobs and CI: one vocabulary (role first; a word next to every color) */
const AGENTS = {claude: "Claude Code", codex: "Codex"};
function agentName(id) { return AGENTS[String(id || "").toLowerCase()] || String(id || "agent"); }
function mainMember() { return CREW.find(m => m.main) || {id: "morpheus", name: "Morpheus", title: "Tech lead", main: true}; }
/* the roster member a persona, id, name or lane label ("Kid: curl installer") names; null when none matches */
function member(x) {
  const s = String(x || "").trim(); if (!s) return null;
  if (s === "main") return mainMember();
  const m = CREWBY.get(s.toLowerCase()); if (m) return m;
  return s.includes(":") ? CREWBY.get(s.slice(0, s.indexOf(":")).trim().toLowerCase()) || null : null;
}
function roleName(m) { return m.title ? m.title + " (" + m.name + ")" : String(m.name || m.id); }
/* role first: "Backend engineer (Tank)"; a lane with no member is "Subagent: <label>"; a label naming someone else, "<name> (not in the crew)" */
function who(x, persona) {
  const m = member(persona) || member(x); if (m) return roleName(m);
  const s = String(x || "").trim();
  if (!CREW.length) return s || String(persona || "") || "Subagent";   /* the roster could not be read: labels as they are */
  if (persona && !member(persona)) return String(persona).trim() + " (not in the crew)";
  if (!s) return "Subagent";
  return s.includes(":") ? s.slice(0, s.indexOf(":")).trim() + " (not in the crew)" : "Subagent: " + s;
}
/* a step's owner: a lane (its persona or label), or the main session; a pending step of the main session may be assigned to a member */
function stepWho(s) {
  if (!s.owner || s.owner === "main") return s.label ? who(s.label) : roleName(mainMember());
  return who(s.owner_label && s.owner_label !== "main" ? s.owner_label : s.owner, s.persona);
}
/* what a step does, for lists: its title, else its name without a leading code ("F4 caller identity" → "caller identity").
   Codes such as F4, R2-3, LC-F6 or "F7 and F8" stay with the crew: the run drawer shows them dim */
const STEPCODE = /^[A-Z][A-Z0-9-]*\d(?:\s*(?:,|\+|&|and)\s*[A-Z][A-Z0-9-]*\d)*[:.]?\s+(?=\S)/;
function plainStep(name) { const s = String(name || "").trim(), m = STEPCODE.exec(s); return m ? s.slice(m[0].length) : s; }
function stepTitle(s) { return String((s && s.title) || "").trim() || plainStep(s && s.name); }
function stepCode(s) {
  const n = String((s && s.name) || "").trim(); if (String((s && s.title) || "").trim()) return n === stepTitle(s) ? "" : n;
  const p = plainStep(n); return p === n ? "" : n.slice(0, n.length - p.length).trim().replace(/[:.]$/, "");
}
function laneStep(label) { const l = String(label || ""); return l.includes(":") ? plainStep(l.slice(l.indexOf(":") + 1).trim()) : ""; }
/* a step whose member stopped without evidence is unverified (contract C3): amber, the tech lead checks it, the developer is not asked */
function stepWord(st) { return st === "unverified" ? ["!", roleName(mainMember()) + " checks it"] : STEPW[st] || STEPW.pending; }
function when(ts) { return ts ? h("time", {datetime: new Date(ts * 1000).toISOString(), title: fmt(ts), text: rel(ts)}) : h("span", {text: "—"}); }
function clock(ts) { if (!ts) return "—"; const d = new Date(ts * 1000), age = nowS() - ts; return (lkey(d) === lkey(new Date()) && age >= 0 ? "" : age > 6 * 86400 || age < -6 * 86400 ? shortDay(lkey(d)) + " " : d.toLocaleDateString(undefined, {weekday: "short"}) + " ") + tm(ts); }
function jobDur(s) { return s == null ? "—" : s < 90 ? Math.max(0, Math.round(s)) + " s" : fmtMin(s / 60); }
function sentence(s) { s = String(s || "").trim(); if (!s) return ""; s = s.charAt(0).toUpperCase() + s.slice(1); return /[.!?]$/.test(s) ? s : s + "."; }
function repoName(r) { return String(r || "").split("/").pop(); }
function provName(p) { return {github: "GitHub", gitlab: "GitLab", bitbucket: "Bitbucket", azure: "Azure"}[String(p || "").toLowerCase()] || "the provider"; }
function taskIds(text) { return Array.from(new Set(String(text || "").match(/\b[A-Z]-[0-9a-z]{2,}\b/g) || [])).filter(id => ITEMBY.has(id)); }
/* schedules as people say them; an interval's next run is an upper bound (launchd keeps its own clock) */
const WEEKDAYS = {mon: "Mondays", tue: "Tuesdays", wed: "Wednesdays", thu: "Thursdays", fri: "Fridays", sat: "Saturdays", sun: "Sundays"};
function schedText(s) {
  s = String(s || "");
  if (s === "manual") return "Only when run by hand";
  const m = /^weekly,\s*([a-z,\s]+?)\s+(\d{1,2}:\d{2})$/i.exec(s); if (!m) return s;
  const ds = m[1].split(/[,\s]+/).filter(Boolean).map(d => WEEKDAYS[d.toLowerCase().slice(0, 3)] || d);
  return (ds.length > 1 ? ds.slice(0, -1).join(", ") + " and " + ds[ds.length - 1] : ds[0]) + " at " + m[2];
}
/* scheduled jobs: [color, word]; the order is the order of the table */
const JOBST = {failed: ["risk", "Failed"], timeout: ["risk", "Timed out"], unscheduled: ["warn", "Not scheduled"], overdue: ["warn", "Overdue"],
  skipped: ["warn", "Skipped"], ok: ["ok", "OK"], never: ["hol", "No runs yet"], off: ["hol", "Off"]};
const JORDER = Object.keys(JOBST);
let LOADED = null;   /* launchd's own view of each job (GET /api/v1/jobs, once per visit to Background work) */
function jobOverdue(j, now) {
  const last = j.last && j.last.start, s = String(j.schedule || "");
  if (!last || !j.enabled) return false;
  if (/^every/.test(s)) return !!j.next && now > j.next + (j.next - last) + 300;   /* two intervals without a start */
  if (/^daily/.test(s)) return now - last > 26 * 3600;
  if (/^weekly/.test(s)) return now - last > 8 * 86400;
  return false;
}
function jobState(j, now) {
  now = now || nowS();
  if (!j.enabled || j.state === "off") return "off";
  if (j.state === "failed") return jobFailure(j).rc === 124 ? "timeout" : "failed";
  const loaded = j.loaded != null ? j.loaded : LOADED && LOADED[j.name] != null ? LOADED[j.name] : null;
  if (!j.installed || loaded === false) return "unscheduled";
  if (jobOverdue(j, now)) return "overdue";
  if (j.state === "skipped") return "skipped";
  return j.state === "never" ? "never" : "ok";
}
/* the failed run that still stands: a run skipped after it is the last run, but says nothing about the failure */
function jobFailure(j) { return j.failure || j.last || {}; }
function jobWord(j, st) { st = st || jobState(j); const f = jobFailure(j); return st === "failed" ? "Failed (exit " + (f.rc != null ? f.rc : "?") + ")" : JOBST[st][1]; }
function stateDot(tone, label) { return h("span", {class: "dot k-" + tone, role: label ? "img" : null, "aria-label": label || null, "aria-hidden": label ? null : "true"}); }
function stateWord(tone, word) { return h("span", {class: "stat"}, stateDot(tone), word); }
/* CI watches: the latest result per branch in the window; failures first */
const CIST = {failed: ["risk", "Failed"], watching: ["mark", "Watching"], timeout: ["warn", "Still running when the watch ended"], none: ["hol", "No CI run appeared"], passed: ["ok", "Passed"]};
const CIORDER = Object.keys(CIST);
const CICLASS = {test: "Test failure", lint: "Lint, format or types", build: "Build failure", dependencies: "Dependency resolution", pipeline: "Pipeline definition",
  infrastructure: "Infrastructure (may be flaky)", unknown: "Not classified"};
const CI_DAYS = 14;
function ciState(w) { return CIST[w.state] && w.state !== "watching" ? w.state : "none"; }
function ciKey(w) { return (w.repo || "") + "@" + (w.branch || ""); }
function ciLatest() {
  if (CACHE.ci) return CACHE.ci;
  const seen = new Set(), out = [];
  for (const w of CIW.watches || []) { const k = ciKey(w); if (!seen.has(k)) { seen.add(k); out.push(w); } }
  return CACHE.ci = out;
}
function ciClass(w, f) { return CICLASS[(f && f.class) || w.class || "unknown"] || CICLASS.unknown; }
/* a live watcher's key is the checkout folder and branch, with unsafe characters replaced: find the project it belongs to */
function ckey(path, branch) { return ((String(path || "").split("/").filter(Boolean).pop() || "") + "-" + String(branch || "")).replace(/[^A-Za-z0-9._-]/g, "_"); }
function watchWhere(wt) {
  for (const p of P) for (const [path, br] of [[p.path, (p.main || {}).branch]].concat(p.wts.map(w => [w.path, w.branch])))
    if (path && br && ckey(path, br) === wt.key) return {project: p, branch: br};
  return null;
}
/* agent runs: waiting for you, blocked, stopped reporting, running, done */
const RUNTONE = {waiting: "risk", blocked: "risk", lost: "warn", running: "mark", done: "ok"};
function pending(r) { return (r.decisions || []).filter(d => !d.state || d.state === "pending"); }
function runLive(r) { return PEOPLE.some(x => x.sessions.some(s => s.session === r.session && s.agent === r.agent)); }
/* a run works while the run or any of its lanes reported in the last 30 minutes (`seen`: steps, notes, crew messages, lane
   starts and stops), or while its session is live; the lead's own last update alone misses a crew at work */
function runSeen(r) { return Math.max(r.updated || 0, r.seen || 0, r.started || 0); }
function runQuiet(r) { return Math.max(0, (nowS() - runSeen(r)) / 60); }
function runState(r) {
  if (pending(r).length) return "waiting";
  if (r.state === "blocked") return "blocked";
  if (r.state === "done") return "done";
  if (!r.remote && runQuiet(r) >= 30 && !runLive(r)) return "lost";
  return "running";
}
function etaText(r) {
  if (r.state !== "running") return "";
  if (r.eta_lo != null && r.eta_hi != null) return fmtAbout(r.eta_lo) === fmtAbout(r.eta_hi) ? "about " + fmtAbout(r.eta_hi) + " left" : fmtAbout(r.eta_lo) + "–" + fmtAbout(r.eta_hi) + " left";
  return r.eta_min != null ? "about " + fmtAbout(r.eta_min) + " left" : "";
}
function runStateText(r, st) {
  st = st || runState(r);
  if (st === "waiting") return "Blocked · waiting for you";
  if (st === "blocked") return "Blocked";
  if (st === "lost") return "Stopped reporting · last update " + rel(runSeen(r));
  if (st === "done") return "Done · " + rel(r.finished || r.updated);
  const e = etaText(r); return "Running" + (e ? " · " + e : "");
}
/* "of about": the plan's current total (active so far plus the time left), the number beside "left"; a run without a
   plan, or a finished one, keeps its estimate */
function runEst(r) { return r.total_min != null ? r.total_min : r.estimate_cal || r.estimate || 0; }
function runFirstEst(r) { return r.estimate_cal || r.estimate || 0; }
function runTime(r) {
  const act = r.active_min || 0, wall = r.wall_min || 0, est = runEst(r);
  const plan = r.plan || r.steps || [], done = plan.filter(s => s.state === "done" || s.state === "skipped").length;
  return {steps: plan.length ? done + " of " + plan.length + " steps" : "No steps planned", act, wall, est, over: est && act > est && r.state === "running" ? act - est : 0,
    text: fmtMin(act) + " active" + (wall - act >= 5 ? " (" + fmtMin(wall) + " wall)" : "") + (est ? " of about " + fmtAbout(est) : " · no estimate"), done, total: plan.length};
}
/* contradictions: shown where they occur, each with its fix (catalog C1 to C11) */
function flagStr(f) { return f.parts.map(p => typeof p === "string" ? p : p.code).join(""); }
function flagNodes(f) { return f.parts.map(p => typeof p === "string" ? p : h("code", {text: p.code})); }
function runFlags(r, det) {
  const out = [], plan = det ? det.steps || [] : r.plan || [], by = new Map(plan.map(s => [s.name, s]));
  for (const s of plan) if (s.state === "running") for (const a of s.after || []) { const d = by.get(a);
    if (d && d.state !== "done" && d.state !== "skipped") out.push({code: "C4", parts: ["“" + stepTitle(s) + "” is running before “" + stepTitle(d) + "” finished."], fix: "Check the plan's order with the run's owner."}); }
  const lanes = det ? (det.lanes || []).filter(l => l.state === "running").map(l => [l.persona, l.label]) : (r.agents || []).filter(a => a.state === "running").map(a => ["", a.label]);
  const groups = new Map();
  for (const [persona, label] of lanes) { const m = member(persona) || member(label), l = String(label || ""), step = laneStep(l) || plainStep(l);
    const k = (m ? m.id : persona || l) + "|" + step.toLowerCase(); groups.set(k, (groups.get(k) || []).concat([[m, step]])); }
  for (const g of groups.values()) if (g.length > 1 && g[0][1]) out.push({code: "C5", parts: ["Two " + (g[0][0] ? roleName(g[0][0]) : "subagent") + " lanes run “" + g[0][1] + "”. One may be a stale launch."]});
  if (runState(r) === "lost") out.push({code: "C6", parts: ["No update for " + Math.round(runQuiet(r)) + " min and its session is not live."], fix: "It may have ended without finishing; the step stays open until someone closes it."});
  if (r.state === "done") { const open = plan.filter(s => s.state !== "done" && s.state !== "skipped");
    if (open.length) out.push({code: "C7", parts: ["Finished with " + plural(open.length, "step") + " not done: " + open.map(stepTitle).join(", ") + "."]}); }
  for (const id of taskIds(r.goal)) if (ITEMBY.get(id).status === "done") out.push({code: "C11", parts: ["The goal names " + id + ", which is done."]});
  return out;
}
function doneWhen(body) {
  const src = String(body || ""), m = /^#{1,6}\s*Done when\b.*$/im.exec(src); if (!m) return {total: 0, open: 0};
  const rest = src.slice(m.index + m[0].length), end = rest.search(/^#{1,6}\s/m), sec = end < 0 ? rest : rest.slice(0, end);
  return {total: (sec.match(/^\s*[-*+]\s+\[[ xX~]\]/gm) || []).length, open: (sec.match(/^\s*[-*+]\s+\[ \]/gm) || []).length};
}
function itemFlags(it, body) {
  const out = [], f = "dev task " + flagOf(it.scope) + " ";
  if (it.legacy) return out;
  if (it.status === "done" && it.claimed_by) out.push({code: "C1", parts: ["Done, but still claimed by " + it.holder + "."], cmd: f + "release " + it.id + ' --why "done"'});
  if (it.open && it.claimed_by && it.lock === "held") { const x = BYWHO.get(it.holder);
    if (x && !x.sessions.length && x.updated && nowS() - x.updated > 7200) out.push({code: "C8", parts: ["Claimed by " + it.holder + ", who has had no live session for " + dur(nowS() - x.updated) + "."], cmd: f + "release " + it.id + ' --why "…"'}); }
  if (it.status === "done") { const dw = doneWhen(body != null ? body : it.body); if (dw.open) out.push({code: "C10", parts: ["Marked done, but " + dw.open + " of " + dw.total + " “Done when” checks are not ticked."]}); }
  if (it.status === "done") for (const r of RUNS) if (r.state !== "done" && taskIds(r.goal).includes(it.id)) out.push({code: "C11", parts: ["The goal of the run “" + r.goal + "” names " + it.id + ", which is done."]});
  return out;
}
function projFlags(p) {
  const out = [], sl = p.status_line, gb = (p.main || {}).branch;
  if ((p.status === "done" || p.status === "archived") && p.cnt && p.cnt.open) out.push({code: "C2", parts: ["Marked done, with " + plural(p.cnt.open, "open task") + "."], fix: "Close or move the tasks, or reopen the project."});
  if (sl && sl.branch && gb && sl.branch !== gb) out.push({code: "C3", parts: ["The latest status was saved on ", {code: sl.branch}, "; git is on ", {code: gb}, " now. It may be out of date."], fix: "Save a new status with dev handoff.", cmd: "dev handoff " + p.id});
  return out;
}
function initFlags(x) { return (x.status === "done" || x.status === "archived") && x.cnt.open ? [{code: "C2", parts: ["Marked done, with " + plural(x.cnt.open, "open task") + "."], fix: "Close or move the tasks, or reopen the initiative."}] : []; }
function allFlags() {
  if (CACHE.flags) return CACHE.flags;
  const out = [];
  for (const p of P) for (const f of projFlags(p)) out.push(Object.assign({where: p.name || p.id, open: () => openModal("project", p.id)}, f));
  for (const x of INITS) for (const f of initFlags(x)) out.push(Object.assign({where: x.title, open: () => openModal("initiative", x.id)}, f));
  for (const it of ITEMS) for (const f of itemFlags(it)) out.push(Object.assign({where: it.id + " " + it.title, open: () => openTask(it.id)}, f));
  for (const r of RUNS) for (const f of runFlags(r)) out.push(Object.assign({where: "Run “" + (r.goal || r.id) + "”", open: () => openModal("run", r.id)}, f));
  return CACHE.flags = out;
}
function flagLine(f) { return h("div", {class: "warnline"}, stateDot("warn"), h("span", {style: "min-width:0"}, flagNodes(f), f.fix ? h("span", {class: "t2", text: " " + f.fix}) : null), f.cmd ? icopy(f.cmd, "Copy the fix") : null); }
function flagCallout(f) { return callout("warn", flagStr(f), f.fix || null, f.cmd ? cmdBtn(f.cmd, false, "Copy the fix") : null); }
/* today's actions in one list (the full list by kind, the palette, the dots before Now's queue arrives): decisions and CI first, then by severity */
function needs() {
  if (CACHE.needs) return CACHE.needs;
  const lead = [], mid = [], now = nowS();
  for (const r of RUNS) for (const d of pending(r)) lead.push({sev: "risk", kind: "decision", title: "Decision waiting: " + d.question, detail: who(d.from_name) + " in “" + (r.goal || "a run") + "”",
    project: r.project, cmd: "", ts: d.ts || 0, modal: ["run", r.id], at: "waiting", go: "Open the run"});
  lead.sort((a, b) => a.ts - b.ts);
  for (const w of ciLatest()) if (w.state === "failed" && now - (w.ts || 0) < 86400) { const f = (w.failures || [])[0] || {};
    lead.push({sev: "risk", kind: "ci-watch", title: "CI failed on " + w.branch + ": " + ciClass(w, f), detail: repoName(w.repo) + (w.sha ? " @ " + w.sha : ""), project: BYID.has(w.repo) ? w.repo : null, cmd: f.id ? "dev run view " + f.id + " --failed" : "dev ci status --watch", url: f.url || null, ts: w.ts || 0, modal: ["ci", ciKey(w)]}); }
  for (const j of JOBS) { const st = jobState(j, now);
    if (st === "failed" || st === "timeout") { const f = jobFailure(j);
      mid.push({sev: "warn", kind: "job-failed", title: st === "timeout" ? "Job " + j.name + " timed out" : "Job " + j.name + " failed (exit " + f.rc + ")", detail: f.summary || j.about || "", cmd: "dev jobs run " + j.name, ts: f.start || 0, modal: ["job", j.name]}); } }
  for (const s of D.issues || []) mid.push({sev: "warn", kind: "setup", title: s.text, detail: "Setup issue: " + s.kind, cmd: "dev doctor", ts: 0, view: "health"});
  return CACHE.needs = lead.concat(ACTIONS.filter(a => a.sev === "risk"), mid, ACTIONS.filter(a => a.sev !== "risk"));
}
/* the Background tab's dot: red when something failed or waits for you, amber when a job is late or a run stopped reporting */
function bgAttention() {
  const now = nowS(), dec = RUNS.reduce((a, r) => a + pending(r).length, 0), ci = ciLatest().filter(w => w.state === "failed" && now - (w.ts || 0) < 86400).length;
  const jf = JOBS.filter(j => ["failed", "timeout"].includes(jobState(j, now))).length;
  if (dec || ci || jf) return ["risk", [dec ? plural(dec, "decision") + " waiting for you" : "", ci ? plural(ci, "CI failure") + " today" : "", jf ? plural(jf, "job") + " failed" : ""].filter(Boolean).join(", ")];
  const late = JOBS.filter(j => ["overdue", "unscheduled"].includes(jobState(j, now))).length, lost = RUNS.filter(r => runState(r) === "lost").length;
  if (late || lost) return ["warn", [late ? plural(late, "job") + " late or not scheduled" : "", lost ? plural(lost, "run") + " stopped reporting" : ""].filter(Boolean).join(", ")];
  return null;
}
function initFor(path) { const m = /^initiatives\/([^/]+)\//.exec(path); return m ? BYINIT.get(m[1]) : null; }
function projectFor(path) { let best = null; for (const p of P) if ((path === "projects/" + p.id + ".md" || path.startsWith("plans/" + p.id + "/")) && (!best || p.id.length > best.id.length)) best = p; return best; }

/* ---------------------------------------------------------------- state + URL: #/<view>[/<id>][?k=v]  (ids keep their slashes and dots) */
let S = Object.assign({}, DEFAULT);
function encode(st) {
  const parts = [];
  for (const k of Object.keys(DEFAULT)) {
    if (k === "view" || k === "id") continue; const v = st[k], d = DEFAULT[k];
    if (JSON.stringify(v) === JSON.stringify(d)) continue;
    parts.push(k + "=" + encodeURIComponent(Array.isArray(v) ? v.join(",") : v === true ? "1" : String(v)));
  }
  const path = st.id && (st.view === "initiatives" || st.view === "projects") ? "/" + String(st.id).split("/").map(encodeURIComponent).join("/") : "";
  return "#/" + st.view + path + (parts.length ? "?" + parts.join("&") : "");
}
function dec(s) { try { return decodeURIComponent(s); } catch (e) { return s; } }
function decode(hash) {
  const m = /^#\/([a-z]+)(\/[^?]*)?(?:\?(.*))?$/.exec(hash || ""); if (!m) return null;
  const st = Object.assign({}, DEFAULT);
  const v = m[1] === "portfolio" ? "initiatives" : m[1] === "overview" || m[1] === "actions" ? "now" : m[1];
  st.view = ROUTES.includes(v) ? v : "now";
  if (m[1] === "actions") st.all = true;
  if (m[2] && (st.view === "initiatives" || st.view === "projects")) st.id = m[2].slice(1).split("/").filter(Boolean).map(dec).join("/");
  for (const kv of (m[3] || "").split("&")) {
    if (!kv) continue; const i = kv.indexOf("="); const k = i < 0 ? kv : kv.slice(0, i); const val = i < 0 ? "" : dec(kv.slice(i + 1));
    if (k === "p" && val) { st.view = "projects"; st.id = val; continue; }      /* v2 links: ?p=<project>, ?i=<initiative> */
    if (k === "i" && val) { st.view = "initiatives"; st.id = val; continue; }
    if (!(k in DEFAULT) || k === "view" || k === "id") continue; const d = DEFAULT[k];
    st[k] = Array.isArray(d) ? (val ? val.split(",") : []) : typeof d === "boolean" ? val === "1" : typeof d === "number" ? (+val || d) : val;
  }
  return st;
}
function link(patch) { const keep = !("view" in patch) || patch.view === S.view; return encode(Object.assign({}, DEFAULT, {view: S.view, id: keep ? S.id : ""}, patch)); }
let RS = null;   /* the state of the last full render: a change of the modal alone redraws only the modal */
function onlyModal(a, b) { if (!a) return false; for (const k of Object.keys(DEFAULT)) if (k !== "modal" && k !== "task" && JSON.stringify(a[k]) !== JSON.stringify(b[k])) return false; return true; }
function set(patch, push) {
  Object.assign(S, patch);
  const hs = encode(S);
  if (push && location.hash !== hs) { location.hash = hs; return; }
  if (location.hash !== hs) history.replaceState(null, "", hs);
  store.set("devkit.state", S); if (onlyModal(RS, S)) renderModal(); else render();
}
let lastView = "";
window.addEventListener("hashchange", () => { const st = decode(location.hash); if (st) { S = st; store.set("devkit.state", S); if (encode(S) !== location.hash) history.replaceState(null, "", encode(S)); if (onlyModal(RS, S)) renderModal(); else render(); } });

/* ---------------------------------------------------------------- filtering (project facets) */
function tokens(q) { return String(q || "").toLowerCase().split(/\s+/).filter(Boolean); }
function matchFacets(p) {
  if (S.status.length && !S.status.includes(p.status)) return false;
  if (S.priority.length && !S.priority.includes(p.priority)) return false;
  if (S.owner.length && !S.owner.includes(p.owner)) return false;
  if (S.stack.length && !p.stack.some(x => S.stack.includes(x))) return false;
  if (S.tag.length && !p.tags.some(x => S.tag.includes(x))) return false;
  if (S.ci.length && !p.ci.some(x => S.ci.includes(x))) return false;
  if (S.health.length && !S.health.includes(p.health.level)) return false;
  if (S.live && !p.live && !ONPROJ.has(p.id)) return false; if (S.prs && !p.prs) return false; if (S.nc && p.local) return false;
  if (S.loose && MEMBEROF.has(p.id)) return false;
  return true;
}
function filtered(useQ) { const tk = useQ ? tokens(S.q) : []; return P.filter(p => matchFacets(p) && tk.every(t => p.hay.includes(t))); }
function facetCount() { return ["status", "priority", "owner", "stack", "tag", "ci", "health"].reduce((a, k) => a + S[k].length, 0) + (S.live ? 1 : 0) + (S.prs ? 1 : 0) + (S.nc ? 1 : 0) + (S.loose ? 1 : 0); }
const CLEAR = {status: [], priority: [], owner: [], stack: [], tag: [], ci: [], health: [], live: false, prs: false, nc: false, loose: false};

/* ---------------------------------------------------------------- markdown (safe subset, DOM only) */
function inline(text) {
  const out = document.createDocumentFragment();
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[([^\]]+)\]\(([^)\s]+)\))|(<(https?:\/\/[^>\s]+)>)|(https?:\/\/[^\s)]+)|((?:^|(?<=\s))_[^_\s][^_]*_(?=\s|$|[.,;:]))/g;
  let last = 0, m; const src = String(text == null ? "" : text);
  while ((m = re.exec(src))) {
    if (m.index > last) out.append(document.createTextNode(src.slice(last, m.index)));
    if (m[1]) out.append(h("code", {text: m[1].slice(1, -1)}));
    else if (m[2]) out.append(h("strong", null, inline(m[2].slice(2, -2))));
    else if (m[3]) out.append(mdLink(m[4], m[5]));
    else if (m[6]) out.append(mdLink(m[7], m[7]));
    else if (m[8]) out.append(mdLink(m[8], m[8]));
    else if (m[9]) out.append(h("em", null, inline(m[9].trim().replace(/^_|_$/g, ""))));
    last = re.lastIndex;
  }
  if (last < src.length) out.append(document.createTextNode(src.slice(last)));
  return out;
}
function mdLink(label, url) {
  const kids = label === url ? [label] : [inline(label)];
  if (/^https?:|^mailto:/i.test(url)) return h("a", {href: url, target: "_blank", rel: "noopener noreferrer"}, kids);
  const path = String(url).replace(/^\.\//, "").replace(/#.*$/, "");
  if (FILEMAP.has(path)) return h("a", {href: link({view: "explorer", f: path})}, kids);
  return h("span", null, kids);
}
function md(text) {
  const root = h("div", {class: "md"});
  let src = String(text || "").replace(/<!--[\s\S]*?-->/g, "");
  const fm = /^---\n([\s\S]*?)\n---\n/.exec(src);
  if (fm) {
    const box = h("div", {class: "fm"});
    for (const ln of fm[1].split("\n")) { const i = ln.indexOf(":"); const v = i > 0 ? ln.slice(i + 1).trim() : ""; if (v && v !== "[]") box.append(h("span", {class: "badge"}, h("span", {class: "t2", text: ln.slice(0, i) + ":"}), " " + v)); }
    if (box.childNodes.length) root.append(box);
    src = src.slice(fm[0].length);
  }
  const lines = src.split("\n"); let i = 0, para = [];
  const flush = () => { if (para.length) { root.append(h("p", null, inline(para.join(" ")))); para = []; } };
  while (i < lines.length) {
    const ln = lines[i];
    if (/^```/.test(ln)) { flush(); const buf = []; i++; while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]); i++; root.append(h("pre", null, h("code", {text: buf.join("\n")}))); continue; }
    let m = /^(#{1,6})\s+(.*)$/.exec(ln);
    if (m) { flush(); root.append(h("h" + Math.min(4, m[1].length), null, inline(m[2]))); i++; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(ln)) { flush(); root.append(h("hr")); i++; continue; }
    if (/^\s*\|/.test(ln) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flush(); const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = cells(ln); i += 2; const tb = h("tbody");
      while (i < lines.length && /^\s*\|/.test(lines[i])) { tb.append(h("tr", null, cells(lines[i]).map(c => h("td", null, inline(c))))); i++; }
      root.append(h("table", null, h("thead", null, h("tr", null, head.map(c => h("th", null, inline(c))))), tb)); continue;
    }
    if (/^>\s?/.test(ln)) { flush(); const buf = []; while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, "")); root.append(h("blockquote", null, inline(buf.join(" ")))); continue; }
    m = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(ln);
    if (m) {
      flush(); const list = h(/\d/.test(m[2]) ? "ol" : "ul");
      while (i < lines.length && (m = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(lines[i]))) {
        const depth = Math.min(3, Math.floor(m[1].length / 2)); const tmk = /^\[( |x|X|~)\]\s+(.*)$/.exec(m[3]);
        const style = depth ? "margin-left:" + depth * 1.2 + "em" : null;
        if (tmk) { const done = tmk[1].toLowerCase() === "x"; list.append(h("li", {class: "task" + (done ? " done" : ""), style}, h("input", {type: "checkbox", disabled: true, checked: done, "aria-label": done ? "done" : "open"}), h("span", null, inline(tmk[2])))); }
        else list.append(h("li", {style}, inline(m[3])));
        i++;
      }
      root.append(list); continue;
    }
    if (!ln.trim()) { flush(); i++; continue; }
    para.push(ln.trim()); i++;
  }
  flush();
  return root;
}
function plain(t) { return String(t || "").replace(/<!--[\s\S]*?-->/g, " ").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[*`]+/g, "").replace(/(^|\s)[-+]\s(\[[ xX~]\]\s)?/g, "$1").replace(/^#+\s*/gm, "").replace(/\s+/g, " ").trim(); }
function highlight(el, q, cls) {
  const tk = tokens(q).filter(t => t.length > 1); if (!tk.length) return 0;
  const src = "(" + tk.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")";
  const re = new RegExp(src, "gi"), probe = new RegExp(src, "i");
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {acceptNode: n => n.parentNode && !/^(SCRIPT|STYLE|MARK)$/.test(n.parentNode.nodeName) && probe.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT});
  const nodes = []; let n; while ((n = walker.nextNode())) nodes.push(n);
  let count = 0;
  for (const node of nodes) {
    const f = document.createDocumentFragment(); let last = 0; const s = node.nodeValue; re.lastIndex = 0; let m;
    while ((m = re.exec(s))) { if (m.index > last) f.append(s.slice(last, m.index)); f.append(h("mark", {class: cls || null, text: m[0]})); last = re.lastIndex; count++; }
    if (last < s.length) f.append(s.slice(last)); node.parentNode.replaceChild(f, node);
  }
  return count;
}

/* ---------------------------------------------------------------- widgets */
/* ---------------------------------------------------------------- icons: authored SVG, one 1.5 px stroke (no Unicode glyphs) */
const ICON = {
  search: [["circle", {cx: 7, cy: 7, r: 4.5}], ["path", {d: "M10.5 10.5 14 14"}]],
  copy: [["rect", {x: 5.5, y: 5.5, width: 8, height: 8, rx: 1.5}], ["path", {d: "M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"}]],
  close: [["path", {d: "M4 4l8 8M12 4l-8 8"}]],
  theme: [["circle", {cx: 8, cy: 8, r: 5.5}], ["path", {d: "M8 2.5a5.5 5.5 0 0 1 0 11z", fill: "currentColor"}]],
  lock: [["rect", {x: 3.5, y: 7, width: 9, height: 6.5, rx: 1.5}], ["path", {d: "M5.5 7V5a2.5 2.5 0 0 1 5 0v2"}]],
  branch: [["circle", {cx: 5, cy: 3.5, r: 1.5}], ["circle", {cx: 5, cy: 12.5, r: 1.5}], ["circle", {cx: 11, cy: 5.5, r: 1.5}], ["path", {d: "M5 5v6M11 7c0 2.5-3 2.5-5.5 4"}]],
  chevron: [["path", {d: "M4.5 6.5 8 10l3.5-3.5"}]],
  todo: [["circle", {cx: 8, cy: 8, r: 5}]],
  doing: [["circle", {cx: 8, cy: 8, r: 5}], ["path", {d: "M8 3a5 5 0 0 1 0 10z", fill: "currentColor"}]],
  blocked: [["circle", {cx: 8, cy: 8, r: 5}], ["path", {d: "M4.6 11.4l6.8-6.8"}]],
  done: [["circle", {cx: 8, cy: 8, r: 5}], ["path", {d: "M5.6 8.2l1.7 1.7 3.2-3.5"}]],
  cancelled: [["circle", {cx: 8, cy: 8, r: 5}], ["path", {d: "M6.2 6.2l3.6 3.6M9.8 6.2l-3.6 3.6"}]],
  session: [["circle", {cx: 8, cy: 8, r: 5}], ["circle", {cx: 8, cy: 8, r: 1.5, fill: "currentColor"}]],
  merge: [["circle", {cx: 4.5, cy: 3.5, r: 1.5}], ["circle", {cx: 4.5, cy: 12.5, r: 1.5}], ["circle", {cx: 11.5, cy: 12.5, r: 1.5}], ["path", {d: "M4.5 5v6M4.5 6c0 3 7 2 7 5"}]],
  gear: [["circle", {cx: 8, cy: 8, r: 2}], ["path", {d: "M8 2v2M8 12v2M2 8h2M12 8h2M3.8 3.8l1.4 1.4M10.8 10.8l1.4 1.4M3.8 12.2l1.4-1.4M10.8 5.2l1.4-1.4"}]],
  miss: [["circle", {cx: 8, cy: 8, r: 5}], ["path", {d: "M8 5.5v3M8 10.5v.1"}]],
  doc: [["path", {d: "M4.5 2.5h4.5l2.5 2.5v8.5h-7z"}], ["path", {d: "M9 2.5V5h2.5M6.5 8h3M6.5 10.5h3"}]],
  book: [["path", {d: "M3 3.5c1.8-.6 3.5-.6 5 .5v9c-1.5-1.1-3.2-1.1-5-.5zM13 3.5c-1.8-.6-3.5-.6-5 .5v9c1.5-1.1 3.2-1.1 5-.5z"}]],
  pulse: [["path", {d: "M2 8.5h2.5l1.5-4 2.5 7 1.5-3.5H14"}]]
};
function icon(name, label) {
  return sv("svg", {class: "i i-" + name, viewBox: "0 0 16 16", width: 16, height: 16, fill: "none", stroke: "currentColor", "stroke-width": 1.5, "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": label ? null : "true", role: label ? "img" : null, "aria-label": label || null, focusable: "false"}, (ICON[name] || []).map(([t, a]) => sv(t, a)));
}
/* a row is clickable with the mouse; its title is a real link (keyboard, screen readers, j/k) */
function rowClick(fn) { return e => { if (e.target.closest && e.target.closest("a,button,summary,input,select,label")) return; fn(); }; }
function taskHref(id) { return encode(Object.assign({}, S, {task: id})); }
function linkTo(href, text, cls, title) { return h("a", {class: "rl" + (cls ? " " + cls : ""), href, "data-nav": "", title: title || null, text}); }
function meta(...parts) { return h("span", {class: "meta"}, parts.flat().filter(x => x != null && x !== false && x !== "").map(x => x.nodeType ? x : h("span", {text: String(x)}))); }
function cmdCode(cmd) { return h("code", null, String(cmd).split(" ").map((t, i) => [i ? " " : null, h("span", {class: "tok", text: t})])); }
function cmdRow(label, cmd) { return h("div", {class: "cmdr"}, h("span", {class: "t2", text: label}), cmdCode(cmd), icopy(cmd, "Copy command to " + label.toLowerCase())); }
function cmdMenu(label, rows) {
  const det = h("details", {class: "cmenu"}, h("summary", {class: "btn"}, h("span", {class: "lbl", text: label}), icon("chevron")),
    h("div", {class: "menu", role: "group", "aria-label": label}, h("div", {class: "cmds"}, rows.filter(Boolean).map(([l, c]) => cmdRow(l, c)))));
  return det;
}
/* ---------------------------------------------------------------- visuals: inline SVG and CSS only. Every visual has an accessible name
   (role=img + aria-label) and every mark a tooltip with its exact value (data-tip: hover, and focus where the visual is focusable).
   Colors follow the dataviz rules: the fixed status palette (always with a label, legend or tooltip), neutral ink for progress,
   a one-hue warm-grey ramp for magnitude, text never in a data color. */
/* honest states: "On track" needs evidence (git state or the PR list) and activity in the last 14 days; no evidence is "No data" */
const HS = {ok: "On track", warn: "At risk", risk: "Off track", quiet: "Quiet", done: "Done", none: "No data"};
const HWHY = {quiet: "Quiet: no activity in 14 days", none: "No data: nothing to check it against"};
const HORDER = ["risk", "warn", "ok", "quiet", "done", "none"];
const QUIET_DAYS = 14;
function hstate(p) {
  if (!p) return "none";
  const st = p.status, lv = p.health ? p.health.level : "none";
  if (st === "done" || st === "archived") return "done";
  if (st === "blocked" || lv === "risk") return "risk";
  if (st === "paused" || lv === "warn") return "warn";
  if (st === "untracked" || st === "unknown" || lv === "none") return "none";
  if (p.health && p.health.known === false) return "none";   /* no git state and no PR list: nothing backs "on track" */
  if (!p.last_activity || nowS() - p.last_activity > QUIET_DAYS * 86400) return "quiet";
  return "ok";
}
function memberState(m) { const p = BYID.get(m.id); return p ? hstate(p) : hstate({status: m.status || "unknown"}); }
/* an initiative is off track when blocked or overdue, at risk when paused, due within a week or holding blocked tasks; it follows its
   members and tasks: nothing to judge is "No data", and no activity in 14 days is "Quiet" */
function istate(x) {
  if (x.status === "done" || x.status === "archived") return "done";
  const today = lkey(new Date()), week = lkey(new Date(Date.now() + 7 * 864e5));
  if (x.status === "blocked" || (x.due && x.due < today)) return "risk";
  if (x.status === "paused" || (x.due && x.due <= week) || x.cnt.blocked) return "warn";
  const ms = x.members.map(memberState), cut = nowS() - QUIET_DAYS * 86400;
  if (!ms.length && !x.items.length) return "none";
  const recent = (x.last || 0) > cut || x.items.some(i => iso(i.updated) > cut);
  if (!recent && ms.every(s => s === "none" || s === "quiet" || s === "done")) return ms.length && ms.every(s => s === "none") ? "none" : "quiet";
  return "ok";
}
function tally(keys) { const c = {}; for (const k of keys) c[k] = (c[k] || 0) + 1; return c; }
function tallyText(c) { return HORDER.filter(k => c[k]).map(k => c[k] + " " + HS[k].toLowerCase()).join(", ") || "none"; }
function hparts(c) { return HORDER.map(k => [k, c[k] || 0, HS[k]]); }
/* a thin segmented bar (one whole split into parts): 2px surface gaps, rounded ends, no text; a legend or label names the parts */
function segBar(parts, label) {
  const tot = parts.reduce((a, p) => a + p[1], 0);
  const el = h("div", {class: "segbar" + (tot ? "" : " empty"), role: "img", "aria-label": label + ": " + (parts.filter(p => p[1]).map(p => p[1] + " " + p[2].toLowerCase()).join(", ") || "none")});
  for (const [k, n, name] of parts) if (n) el.append(h("i", {class: "k-" + k, style: "flex:" + n + " 1 0", "data-tip": name + ": " + n + " of " + tot + " (" + Math.round(100 * n / tot) + "%)"}));
  return el;
}
function legend(parts) { return h("ul", {class: "lgd", "aria-hidden": "true"}, parts.filter(p => p[1]).map(([k, n, name]) => h("li", null, h("i", {class: "k-" + k}), h("b", {class: "num", text: String(n)}), name.toLowerCase()))); }
/* a progress ring with the number inside */
function ring(pct, size, label, focusable) {
  size = size || 44; const sw = size >= 56 ? 6 : 4, r = (size - sw) / 2, c = 2 * Math.PI * r, m = size / 2, v = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  return sv("svg", {class: "ring" + (size >= 56 ? " lg" : size < 40 ? " sm" : ""), width: size, height: size, viewBox: "0 0 " + size + " " + size, role: "img", "aria-label": label, "data-tip": label, tabindex: focusable ? "0" : null},
    sv("circle", {cx: m, cy: m, r, class: "r-t", "stroke-width": sw}),
    v > 0 ? sv("circle", {cx: m, cy: m, r, class: "r-a", "stroke-width": sw, "stroke-dasharray": (c * v / 100).toFixed(2) + " " + c.toFixed(2), transform: "rotate(-90 " + m + " " + m + ")"}) : null,
    sv("text", {x: m, y: m, class: "r-v", "text-anchor": "middle", dy: "0.35em"}, pct == null ? "–" : Math.round(v) + (size >= 56 ? "%" : "")));
}
function progRing(x, size, focusable) { const pr = progOf(x); return ring(pr.pct, size, pr.pct == null ? "No progress yet (no tasks)" : "Progress " + pr.pct + "%: " + pr.why, focusable); }
/* a waffle: one square per member project, colored by state, grouped worst first */
function waffle(items, label, cell, cols, focusable) {
  cell = cell || 8; const g = 2, n = Math.max(1, items.length); cols = Math.max(1, Math.min(n, cols || 24));
  const rows = Math.ceil(n / cols), w = cols * (cell + g) - g, hh = rows * (cell + g) - g;
  const sorted = items.slice().sort((a, b) => HORDER.indexOf(a[0]) - HORDER.indexOf(b[0]) || String(a[1]).localeCompare(String(b[1])));
  return sv("svg", {class: "waffle", width: w, height: hh, viewBox: "0 0 " + w + " " + hh, role: "img", "aria-label": label, tabindex: focusable ? "0" : null, "data-tip": focusable ? label : null},
    sorted.map(([s, name], i) => sv("rect", {x: (i % cols) * (cell + g), y: Math.floor(i / cols) * (cell + g), width: cell, height: cell, rx: cell > 8 ? 2 : 1.5, class: "k-" + s, "data-tip": name + ": " + HS[s].toLowerCase()})));
}
function memberWaffle(x, cell, cols, focusable) {
  const items = x.members.map(m => [memberState(m), String(m.id).split("/").pop()]);
  return items.length ? waffle(items, plural(items.length, "member project") + ": " + tallyText(tally(items.map(i => i[0]))), cell, cols, focusable) : h("span", {class: "t2", text: "No member projects"});
}
/* a sparkline: one column per day (the de-emphasis hue), today in ink */
function spark(series, label, unit, focusable) {
  const n = series.length, bw = 5, g = 2, H = 24, w = n * (bw + g) - g, max = Math.max(1, ...series.map(s => s[1])), tot = series.reduce((a, s) => a + s[1], 0);
  const name = label + ": " + plural(tot, unit) + " in " + n + " days, " + series[n - 1][1] + " today";
  return sv("svg", {class: "spark", width: w, height: H, viewBox: "0 0 " + w + " " + H, role: "img", "aria-label": name, tabindex: focusable ? "0" : null, "data-tip": focusable ? name : null},
    series.map(([k, v], i) => { const bh = v ? Math.max(3, Math.round(v / max * H)) : 2;
      return sv("rect", {x: i * (bw + g), y: H - bh, width: bw, height: bh, rx: 1, class: i === n - 1 ? "cur" : v ? "" : "zero", "data-tip": shortDay(k) + ": " + plural(v, unit)}); }));
}
function commitSeries(ps, n) { return days(n).map(k => [k, ps.reduce((a, p) => a + (p.act[k] || 0), 0)]); }
function sessionSeries(scopes, n) { const c = {}; for (const s of D.session_log || []) if (!scopes || scopes.has(s.scope)) { const k = lkey(new Date(s.ts * 1000)); c[k] = (c[k] || 0) + 1; } return days(n).map(k => [k, c[k] || 0]); }
function doneSeries(items, n) { const c = {}; for (const it of items) if (it.status === "done" && it.updated) { const k = lkey(new Date(iso(it.updated) * 1000)); c[k] = (c[k] || 0) + 1; } return days(n).map(k => [k, c[k] || 0]); }
function mergedSeries(n) { const c = {}; for (const m of D.merged || []) { const k = lkey(new Date(m.ts * 1000)); c[k] = (c[k] || 0) + 1; } return days(n).map(k => [k, c[k] || 0]); }
/* a CI strip: one dot per open PR, failing first */
const CIK = {passing: "ok", failing: "risk", pending: "warn"};
function ciStrip(p, max) {
  const prs = p.prsOpen || []; max = max || 10;
  if (!prs.length) return h("span", {class: "ci none", role: "img", "aria-label": "No open PRs"});
  const rank = {failing: 0, pending: 1, passing: 2}, sorted = prs.slice().sort((a, b) => (rank[a.checks] ?? 3) - (rank[b.checks] ?? 3));
  const c = tally(sorted.map(r => r.checks || "unknown"));
  return h("span", {class: "ci", role: "img", "aria-label": plural(prs.length, "open PR") + ": " + Object.entries(c).map(([k, n]) => n + " " + (CHECKS[k] || k).replace(/^CI /, "")).join(", ")},
    sorted.slice(0, max).map(r => h("i", {class: "k-" + (CIK[r.checks] || "none"), "data-tip": "#" + r.number + " " + (CHECKS[r.checks] || "CI unknown") + (r.title ? ": " + r.title : "")})),
    sorted.length > max ? h("b", {class: "t2", text: "+" + (sorted.length - max)}) : null);
}
/* a lock with a countdown ring: the share of the lock's time that is left */
function lockRing(start, until, who) {
  const now = nowS(), t1 = iso(until), t0 = iso(start) || (t1 ? t1 - 8 * 3600 : now), left = t1 ? t1 - now : 0, f = t1 ? Math.max(0, Math.min(1, left / Math.max(1, t1 - t0))) : 0, exp = !!t1 && left <= 0;
  const r = 8.5, c = 2 * Math.PI * r, text = (who ? who + ": " : "") + (exp ? "lock expired " + rel(t1) : t1 ? "lock " + dur(left) + " left (until " + fmt(t1) + ")" : "locked");
  return h("span", {class: "lockr" + (exp ? " exp" : ""), role: "img", "aria-label": text, "data-tip": text},
    sv("svg", {width: 20, height: 20, viewBox: "0 0 20 20", "aria-hidden": "true", focusable: "false"},
      sv("circle", {cx: 10, cy: 10, r, class: "r-t", "stroke-width": 2}),
      f > 0 ? sv("circle", {cx: 10, cy: 10, r, class: "r-a", "stroke-width": 2, "stroke-dasharray": (c * f).toFixed(2) + " " + c.toFixed(2), transform: "rotate(-90 10 10)"}) : null,
      sv("rect", {x: 6.5, y: 9, width: 7, height: 5.5, rx: 1.2, class: "lk-b"}), sv("path", {d: "M8 9V7.6a2 2 0 0 1 4 0V9", class: "lk-s"})));
}
/* a due-date pill: overdue in red, due within a week marked */
function duePill(due, done, none) {
  if (!due) return none ? h("span", {class: "pill none", text: none}) : null;
  const today = lkey(new Date()), dd = Math.round((new Date(due + "T12:00:00") - new Date(today + "T12:00:00")) / 864e5), over = !done && dd < 0;
  const text = over ? "Overdue " + (-dd) + " d" : !done && dd === 0 ? "Due today" : shortDay(due);
  return h("span", {class: "pill" + (over ? " over" : !done && dd <= 7 ? " soon" : ""), "data-tip": (over ? "Overdue since " : done ? "Was due " : "Due ") + fmtDay(new Date(due + "T12:00:00").getTime() / 1000), text});
}
/* a running session: what it works on, a thin progress bar, steps; opens the session in a modal */
function sessBar(s, who, i) {
  const p = s.progress || null, blocked = !!(p && p.state === "blocked"), pct = p ? (p.steps ? Math.min(100, 100 * (p.step || 0) / p.steps) : 0) : null;
  const what = (p && p.goal) || (s.status_parts && s.status_parts.task) || scopeName(s.project || "") || "Session";
  const text = what + ", " + scopeName(s.project || "") + (s.branch ? " on " + s.branch : "") + ", " + (s.agent || "agent") + (p ? (blocked ? ": blocked" : p.steps ? ": " + (p.step || 0) + " of " + p.steps + " steps" + (p.eta_min != null ? ", about " + fmtAbout(p.eta_min) + " left" : "") : "") : "");
  return h("button", {class: "sbar" + (blocked ? " blocked" : ""), type: "button", "aria-label": text, "data-tip": text, onclick: e => { e.stopPropagation(); openModal("session", who + "~" + i); }},
    h("span", {class: "sb-l"}, h("span", {class: "ell", text: scopeName(s.project || "") || s.agent || "session"}), h("span", {class: "num t2", text: p ? (blocked ? "blocked" : p.steps ? (p.step || 0) + "/" + p.steps : "") : ""})),
    h("span", {class: "sb-b"}, pct != null ? h("i", {style: "width:" + pct.toFixed(0) + "%"}) : null));
}
/* a stat tile: label, value, one visual, its legend */
function statTile(o) {
  return h(o.href ? "a" : "div", {class: "stile" + (o.cls ? " " + o.cls : ""), href: o.href || null, "data-nav": o.href ? "" : null, onclick: o.onclick || null},
    h("span", {class: "s-l", text: o.label}), h("span", {class: "s-v"}, h("b", {class: "num", text: String(o.value)}), o.unit ? h("span", {class: "t2", text: o.unit}) : null),
    o.viz || null, o.legend || null);
}
function modalHref(kind, id) { return encode(Object.assign({}, S, {modal: kind + ":" + id, task: ""})); }
function modalLink(kind, id, text, cls) { return h("a", {class: "rl" + (cls ? " " + cls : ""), href: modalHref(kind, id), "data-nav": "", onclick: e => { e.preventDefault(); openModal(kind, id); }, text}); }
/* the tooltip: hover over any [data-tip], or focus one; it lives in the top layer while a modal is open */
function showTip(el) {
  const t = el.getAttribute("data-tip"); if (!t) return;
  const box = $("#tip"), host = layerHost(); if (box.parentNode !== host) host.append(box);
  const r = el.getBoundingClientRect(); tip(r.left + r.width / 2 - 14, r.bottom - 8, document.createTextNode(t));
}
document.addEventListener("pointerover", e => { const el = e.target.closest && e.target.closest("[data-tip]"); if (el) showTip(el); });
document.addEventListener("pointerout", e => { const el = e.target.closest && e.target.closest("[data-tip]"); if (el && !(e.relatedTarget && el.contains(e.relatedTarget))) untip(); });
document.addEventListener("focusin", e => { const el = e.target.closest && e.target.closest("[data-tip]"); if (el) showTip(el); else untip(); });
document.addEventListener("scroll", () => untip(), true);
function toast(msg) { const t = $("#toast"), host = layerHost(); if (t.parentNode !== host) host.append(t); t.textContent = msg; t.classList.add("on"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), 1800); }
function copy(text) {
  const done = () => toast("Copied · " + text);
  const fallback = () => { const ta = h("textarea", {style: "position:fixed;opacity:0"}); ta.value = text; document.body.append(ta); ta.select(); try { document.execCommand("copy"); done(); } catch (e) { toast("Copy failed — select the command instead"); } ta.remove(); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
}
function cmdBtn(cmd, primary, label) {
  const short = !label && String(cmd).length <= 30;
  return h("button", {class: "btn" + (primary ? " primary" : ""), type: "button", title: cmd, "aria-label": (label ? label + " (copies " : "Copy command (") + cmd + ")", onclick: e => { e.stopPropagation(); copy(cmd); }},
    icon("copy"), short ? h("code", {class: "lbl", text: cmd}) : h("span", {class: "lbl", text: label || "Copy command"}));
}
function icopy(cmd, label) { return h("button", {class: "icopy", type: "button", title: cmd, "aria-label": (label || "Copy command") + ": " + cmd, onclick: e => { e.stopPropagation(); copy(cmd); }}, icon("copy")); }
function empty(title, ...body) { return h("div", {class: "empty"}, h("strong", {text: title}), ...body); }
function tip(x, y, node) { const t = $("#tip"); clear(t).append(node); t.hidden = false; const r = t.getBoundingClientRect(); let l = x + 14, tp = y + 14; if (l + r.width > innerWidth - 8) l = x - r.width - 14; if (tp + r.height > innerHeight - 8) tp = y - r.height - 14; t.style.left = Math.max(8, l) + "px"; t.style.top = Math.max(8, tp) + "px"; }
function untip() { $("#tip").hidden = true; }
function statusBadge(s, plainText) { const l = (STATUS[s] || STATUS.active)[1]; return h("span", {class: "stat", title: "Status: " + l}, plainText ? null : h("span", {class: "dot " + (SDOT[s] || "d-info"), "aria-hidden": "true"}), l); }
function prioBadge(p) { return p === "high" ? h("span", {text: "High priority"}) : p === "low" ? h("span", {text: "Low priority"}) : null; }
function prioIcon(p) { return p === "high" ? h("span", {class: "pico", title: "Priority: High", text: "high"}) : null; }
function healthDot(p) { const lv = p.health.level, why = p.health.reasons.length ? p.health.reasons.join(" · ") : "Nothing needs attention";
  return h("span", {class: "dot hd d-" + lv, role: "img", title: (lv === "ok" ? "Healthy" : lv === "warn" ? "Needs a look" : "At risk") + " — " + why, "aria-label": "Health " + lv + ": " + why}); }
function prPill(p, r) {
  const st = r.checks || "unknown", rv = r.review === "CHANGES_REQUESTED" ? "changes requested" : r.review === "APPROVED" ? "approved" : r.review === "REVIEW_REQUIRED" ? "review required" : "";
  const title = "#" + r.number + " " + (r.title || "") + ": " + CHECKS[st] + (rv ? ", " + rv : "") + (r.mergeable === "CONFLICTING" ? ", merge conflicts" : "") + (r.draft ? ", draft" : "");
  return h("a", {class: "pr", href: r.url || (p.github ? "https://github.com/" + p.github + "/pull/" + r.number : "#"), target: "_blank", rel: "noopener noreferrer", title, "aria-label": title, onclick: e => e.stopPropagation()},
    h("span", {class: "dot d-" + st, "aria-hidden": "true"}), "#" + r.number);
}
function prPills(p, max) {
  if (!p.prsOpen.length) return h("span", {class: "muted", text: "—"});
  const sorted = p.prsOpen.slice().sort((a, b) => ({failing: 0, pending: 1, passing: 2}[a.checks] ?? 3) - ({failing: 0, pending: 1, passing: 2}[b.checks] ?? 3));
  const n = max || 3;
  return h("div", {class: "prs"}, sorted.slice(0, n).map(r => prPill(p, r)), sorted.length > n ? h("span", {class: "t2", style: "font-size:var(--fs-1);align-self:center", text: "+" + (sorted.length - n)}) : null);
}
function sparkline(p, n, w, hh) {
  const ks = days(n || 30), vals = ks.map(k => p.act[k] || 0), max = Math.max(1, ...vals); w = w || 72; hh = hh || 18; const bw = w / ks.length;
  const total = vals.reduce((a, b) => a + b, 0);
  const svg = sv("svg", {class: "spk", width: w, height: hh, viewBox: "0 0 " + w + " " + hh, role: "img", "aria-label": total + " commits in " + ks.length + " days"});
  vals.forEach((v, i) => { const bh = v ? Math.max(2, (v / max) * (hh - 1)) : 1; svg.append(sv("rect", {x: (i * bw + 0.3).toFixed(1), y: (hh - bh).toFixed(1), width: Math.max(1, bw - 1.2).toFixed(1), height: bh.toFixed(1), class: i >= ks.length - 7 ? "cur" : null})); });
  return svg;
}
function sum7(p) { return days(7).reduce((a, k) => a + (p.act[k] || 0), 0); }
function niceMax(v) { if (v <= 4) return 4; const pw = Math.pow(10, Math.floor(Math.log10(v))); const m = v / pw; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * pw; }
function panel(title, count, right, body, opts) {
  opts = opts || {};
  return h("section", {class: "panel" + (opts.cls ? " " + opts.cls : ""), id: opts.id || null, "aria-label": opts.label || title},
    h("div", {class: "ph"}, h("h2", {text: title}), count != null ? h("span", {class: "count num", text: String(count)}) : null, right ? h("div", {class: "right"}, right) : null),
    h("div", {class: "pb" + (opts.flush ? " flush" : "")}, body));
}
function seg(label, key, opts, cur) {
  const v0 = cur === undefined ? S[key] : cur;
  return h("div", {class: "seg", role: "group", "aria-label": label}, opts.map(([v, l, n]) => h("button", {type: "button", "aria-pressed": String(v0 === v), onclick: () => set({[key]: v}, true)},
    l, n != null ? h("span", {class: "sn num", text: String(n)}) : null)));
}
function selectBox(label, key, opts) { const s = h("select", {"aria-label": label}); for (const [v, l] of opts) s.append(h("option", {value: v, selected: S[key] === v}, l)); s.addEventListener("change", () => set({[key]: s.value}, true)); return s; }
function crumbs(list) {
  return h("nav", {class: "crumbs", "aria-label": "Breadcrumb"}, h("ol", null, list.map(([label, href], i) => h("li", null,
    href && i < list.length - 1 ? h("a", {href, text: label}) : h("span", {"aria-current": i === list.length - 1 ? "page" : null, text: label})))));
}
/* a few figures with context: "28 urgent / of 46 items that need you"; rows are [value, label, tone, context, href] */
function stats(rows, sm) {
  rows = rows.filter(Boolean);
  return h("div", {class: "figs" + (sm ? " sm" : ""), style: "--n:" + rows.length, role: "list"}, rows.map(([v, l, tone, sub, href]) => h(href ? "a" : "div", {class: "fig", href: href || null, role: "listitem"},
    h("span", {class: "fv"}, tone ? h("span", {class: "dot d-" + (tone === "risk" ? "risk" : "warn"), "aria-hidden": "true"}) : null, h("b", {class: "num", text: String(v)}), h("span", {class: "fl", text: l})),
    sub ? h("span", {class: "fs", text: sub}) : null)));
}
function callout(tone, title, body, act) {
  return h("div", {class: "callout " + tone, role: tone === "risk" ? "alert" : null}, tone === "info" ? null : h("span", {class: "dot d-" + (tone === "risk" ? "risk" : "warn"), "aria-hidden": "true"}),
    h("div", {class: "cbody"}, h("strong", {text: title}), body ? h("div", null, body) : null), act ? h("div", {class: "cact"}, act) : null);
}
function pageTabs(tabs, cur) {
  const bar = h("div", {class: "ptabs", role: "tablist", "aria-label": "Sections"});
  for (const [key, label, n] of tabs) bar.append(h("button", {class: "ptab", role: "tab", type: "button", id: "tab-" + key, "aria-selected": String(cur === key), "aria-controls": "tabpanel", tabindex: cur === key ? "0" : "-1",
    onclick: () => set({tab: key === tabs[0][0] ? "" : key, src: "all", tst: ""}, true)}, label, n != null ? h("span", {class: "n num", text: String(n)}) : null));
  bar.addEventListener("keydown", e => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return; e.preventDefault();
    const bs = $$(".ptab", bar), i = bs.indexOf(document.activeElement);
    const nx = e.key === "Home" ? bs[0] : e.key === "End" ? bs[bs.length - 1] : bs[(i + (e.key === "ArrowRight" ? 1 : -1) + bs.length) % bs.length]; nx.focus(); nx.click();
  });
  return bar;
}
function avatar(w, lg) {
  const user = String(w || "?").split("@")[0], ini = (user.replace(/[^a-z0-9]/gi, "").slice(0, 2) || "?").toUpperCase();
  const live = ((BYWHO.get(w) || {}).sessions || []).length > 0;
  return h("span", {class: "av" + (lg ? " lg" : "") + (live ? " live" : ""), role: "img", title: w + (isMe(w) ? " (you)" : "") + (live ? ", live session" : ""), "aria-label": w + (isMe(w) ? " (you)" : "") + (live ? ", live" : "")}, ini);
}
function avatars(list, max) {
  max = max || 4; if (!list.length) return h("span", {class: "muted", text: "—"});
  return h("span", {class: "avs"}, list.slice(0, max).map(w => avatar(w)), list.length > max ? h("span", {class: "av more", title: list.slice(max).join(", "), text: "+" + (list.length - max)}) : null);
}
function lockIcon() { const s = icon("lock"); s.classList.add("lk"); return s; }
function untilText(s) { const t = iso(s); if (!t) return ""; const d = t - nowS(); return d <= 0 ? "expired " + rel(t) : "for " + dur(d); }
function claimChip(it, short) {
  if (!it.claimed_by) return null;
  const exp = it.lock === "expired", w = it.holder, who = isMe(w) ? "you" : String(w).split("@")[0];
  const left = String(untilText(it.lock_expires)).replace(/^for /, "");
  return h("span", {class: "claim" + (exp ? " exp" : "") + (isMe(w) ? " mine" : ""), title: "Claimed by " + it.claimed_by + (it.lock_expires ? (exp ? ", lock expired " : ", lock until ") + fmt(iso(it.lock_expires)) : "")},
    lockIcon(), h("span", {class: "cw", text: who + (exp ? ", lock expired" : short || !left ? "" : ", " + left + " left")}));
}
function istatusIcon(st) { const l = (ISTATUS[st] || ISTATUS.todo)[1]; return h("span", {class: "ist i-" + st, role: "img", title: l, "aria-label": l}, icon(ICON[st] ? st : "todo")); }
function istatusBadge(st) { const l = (ISTATUS[st] || ISTATUS.todo)[1]; return h("span", {class: "stat"}, h("span", {class: "ist i-" + st, "aria-hidden": "true"}, icon(ICON[st] ? st : "todo")), l); }
function typeTag(t) { return t && t !== "task" ? h("span", {text: t.charAt(0).toUpperCase() + t.slice(1)}) : null; }
function scopeName(scope) { if (isInit(scope)) { const x = BYINIT.get(scope.slice(12)); return x ? x.title : scope.slice(12); } const p = BYID.get(scope); return p ? (p.name || p.id) : String(scope || "").split("/").pop(); }
function scopeChip(scope) {
  const ini = isInit(scope);
  return h("button", {class: "sc", type: "button", title: (ini ? "Initiative " : "Project ") + scope, onclick: e => { e.stopPropagation(); ini ? openInitiative(scope.slice(12)) : openProject(scope); }}, scopeName(scope));
}
function taskCmds(it) {
  if (it.legacy && !it.n) return [];
  const sel = it.legacy ? String(it.n) : it.id, f = "dev task " + flagOf(it.scope) + " ", c = [];
  if (it.status === "todo") c.push(["Start: claim the lock", f + "start " + sel]);
  if (it.status === "doing" || it.status === "blocked") c.push(["Mark done", f + "done " + sel]);
  if (it.claimed_by && it.open) c.push([it.lock === "expired" ? "Release the expired lock" : "Release the lock", f + "release " + sel + ' --why "…"']);
  if (it.open) {
    c.push(["Move to " + (it.bucket === "now" ? "Next" : "Now"), f + "move " + sel + " --to " + (it.bucket === "now" ? "next" : "now") + ' --why "…"']);
    if (it.status !== "blocked") c.push(["Block", f + "block " + sel + ' --why "…"']);
    c.push(["Cancel", f + "cancel " + sel + ' --why "…"']);
  }
  if (!it.legacy) c.push(["Show the history", f + "history " + sel]);
  return c;
}
function itemRow(it, opts) {
  opts = opts || {};
  const first = taskCmds(it)[0];
  const sub = [opts.scope && it.scope !== opts.home ? scopeChip(it.scope) : null, it.due ? dueBadge(it.due, !it.open) : null, it.legacy && it.n ? h("span", {text: "#" + it.n + " in the note"}) : null].filter(Boolean);
  return h("li", {class: "irow" + (it.open ? "" : " closed"), "data-key": "task~" + it.id, "data-sig": [it.status, it.bucket, it.claimed_by, it.lock].join("|"), "data-open": "task:" + it.id, onclick: rowClick(() => openTask(it.id))},
    istatusIcon(it.status),
    h("div", {class: "im"}, h("a", {class: "itl", href: taskHref(it.id), "data-nav": "", title: it.title, onclick: e => { e.preventDefault(); openTask(it.id); }, text: it.title}),
      sub.length ? h("div", {class: "imeta"}, sub) : null),
    h("div", {class: "icl"}, claimChip(it)),
    first && it.open ? icopy(first[1], first[0]) : h("span"));
}
function sortItems(items) {
  const b = {now: 0, next: 1, later: 2}, s = {blocked: 0, doing: 1, todo: 2, done: 3, cancelled: 4};
  return items.slice().sort((a, c) => (s[a.status] ?? 5) - (s[c.status] ?? 5) || (b[a.bucket] ?? 3) - (b[c.bucket] ?? 3) || (PRANK[a.priority] ?? 1) - (PRANK[c.priority] ?? 1) || String(c.updated || "").localeCompare(String(a.updated || "")));
}
function parseLog(line) {
  const parts = String(line).split(" · ");
  if (parts.length < 4 || !/^v\d+$/.test(parts[0])) return {v: "", ts: 0, who: "", agent: "", op: String(line), detail: "", why: ""};
  const rest = parts.slice(3).join(" · "), wi = rest.indexOf(" — why: "), what = wi >= 0 ? rest.slice(0, wi) : rest, ci = what.indexOf(": ");
  return {v: parts[0], ts: iso(parts[1]), who: whoOf(parts[2]), agent: agentOf(parts[2]), op: ci > 0 ? what.slice(0, ci) : what, detail: ci > 0 ? what.slice(ci + 2) : "", why: wi >= 0 ? rest.slice(wi + 8) : ""};
}
function changelog(lines) {
  if (!lines || !lines.length) return empty("No history yet");
  return h("ol", {class: "clog"}, lines.map(l => { const e = parseLog(l);
    return h("li", null, h("span", {class: "cv num", text: e.v || "·"}),
      h("div", {class: "cb"}, h("div", {class: "ch"}, h("strong", {text: e.op}), e.detail ? h("span", {class: "t2", text: " " + e.detail.replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z/g, m => fmt(iso(m)))}) : null),
        h("div", {class: "cm"}, e.who.includes("@") ? avatar(e.who) : null, meta(e.who + (e.agent ? " with " + e.agent : ""), e.ts ? h("span", {title: fmt(e.ts), text: fmt(e.ts) + " (" + rel(e.ts) + ")"}) : null)),
        e.why ? h("div", {class: "cwhy"}, h("span", {class: "label", text: "Why:"}), inline(e.why)) : null)); }));
}
/* ---------------------------------------------------------------- column chart (single series) with tooltip, keyboard, table twin */
function rankedBars(rows, fmtv, onclick) {
  const max = Math.max(1, ...rows.map(r => r[1]));
  return h("div", {class: "hbar"}, rows.map(([label, v, id]) => [
    onclick ? h("button", {type: "button", onclick: () => onclick(id || label), title: label, text: label}) : h("span", {class: "ell", text: label, title: label}),
    h("div", {class: "trk", role: "img", "aria-label": label + ": " + fmtv(v)}, h("i", {style: "width:" + Math.max(0.5, (v / max) * 100).toFixed(1) + "%"})),
    h("span", {class: "num t2", text: fmtv(v)})]));
}
function facetMenu(key, label, values, fmtv) {
  const sel = S[key];
  const det = h("details", {class: "facet"}, h("summary", null, h("span", {class: "btn sm"}, label + (sel.length ? " · " + sel.length : ""), h("span", {"aria-hidden": "true", text: "▾"}))));
  const menu = h("div", {class: "menu", role: "group", "aria-label": label});
  if (!values.length) menu.append(h("div", {class: "t2", style: "padding:8px", text: "No values"}));
  for (const [v, n] of values) {
    const cb = h("input", {type: "checkbox", checked: sel.includes(v)});
    cb.addEventListener("change", () => set({[key]: cb.checked ? sel.concat([v]) : sel.filter(x => x !== v)}, true));
    menu.append(h("label", null, cb, h("span", {text: fmtv ? fmtv(v) : v}), h("span", {class: "count num", text: String(n)})));
  }
  det.append(menu); return det;
}
function counts(fn) { const m = new Map(); for (const p of P) for (const v of [].concat(fn(p))) if (v) m.set(v, (m.get(v) || 0) + 1); return Array.from(m.entries()).sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(b[0])); }

/* ---------------------------------------------------------------- live runs: progress, ETA, subagents, current turn */
/* a live run: goal, a thin bar, steps and time; blocked runs say so in red text */
function liveRun(s, asOf) {
  const p = s.progress || null, run = runOf(s), now = nowS();
  if (!p) return null;
  const el = p.started ? (now - p.started) / 60 : 0, est = run ? runEst(run) : p.estimate || 0;
  const left = p.eta_min != null && p.state === "running" ? Math.max(0, p.eta_min - (now - (asOf || D.rendered || now)) / 60) : null;
  const pct = p.steps ? Math.min(100, 100 * (p.step || 0) / p.steps) : est ? Math.min(100, 100 * el / est) : null;
  const over = est && (p.active_min != null ? p.active_min : el) > est && p.state === "running";
  const state = p.state === "blocked" ? "blocked" : p.state === "done" ? "done" : left != null ? "about " + fmtAbout(left) + " left" : "running";
  const steps = p.steps ? (p.step || 0) + " of " + p.steps + " steps" : "step " + (p.step || 0);
  // active time (turns and subagents), with wall-clock time beside it only when they differ
  const act = p.active_min != null ? p.active_min : el, wall = p.wall_min != null ? p.wall_min : el;
  const timeText = fmtMin(act) + " active" + (wall - act >= 5 ? " (" + fmtMin(wall) + " wall)" : "") + (est ? " of about " + fmtAbout(est) : "");
  const range = p.eta_lo != null && p.eta_hi != null && p.state === "running"
    ? (fmtAbout(p.eta_lo) === fmtAbout(p.eta_hi) ? "about " + fmtAbout(p.eta_hi) : fmtAbout(p.eta_lo) + "–" + fmtAbout(p.eta_hi)) + " left" : null;
  const state2 = p.state === "blocked" ? "blocked" : p.state === "done" ? "done" : range || state;
  const plan = (run && run.plan) || p.plan || [], glyph = {done: "✓", running: "▸", blocked: "■", skipped: "–", pending: "○", unverified: "!"};
  const shown = plan.filter(x => ["running", "blocked", "unverified"].includes(x.state))
    .concat(plan.filter(x => x.state === "pending").slice(0, 2));
  const doneN = plan.filter(x => x.state === "done" || x.state === "skipped").length;
  const stepList = shown.length ? h("ul", {class: "rsteps", "aria-label": "Plan steps"},
    shown.map(x => h("li", {class: x.state},
      h("span", {class: "g", "aria-hidden": "true", text: glyph[x.state] || "○"}),
      h("span", {class: "ell", title: stepTitle(x) + " · " + stepWho(x)}, stepTitle(x), x.owner && x.owner !== "main" ? h("span", {class: "who", text: " · " + stepWho(x)}) : null),
      h("span", {class: "m num", text: x.state === "running" ? fmtMin(x.min || 0) + " of ~" + fmtAbout(x.est || 0) : x.state === "unverified" ? stepWord(x.state)[1] : "~" + fmtAbout(x.est || 0)}))),
    doneN ? h("li", {class: "more"}, h("span", {class: "g", text: "✓"}), h("span", {text: doneN + " done"}), h("span")) : null) : null;
  const lanes = (p.agents || []).length ? h("div", {class: "lanes", "aria-label": "Crew"},
    p.agents.map(a => h("span", {class: a.state === "running" ? "on" : a.state === "lost" || a.state === "unverified" ? "lost" : "",
      title: a.state === "lost" ? "stopped reporting" : a.state === "unverified" ? stepWord(a.state)[1] : a.state},
      (a.state === "running" ? "▸ " : a.state === "lost" || a.state === "unverified" ? "! " : "✓ ") + who(a.label) + " " + fmtMin(a.min || 0)))) : null;
  return h("div", {class: "run " + (p.state || "running")},
    h("div", {class: "rh"}, h("strong", {class: "ell", title: p.goal || "", text: p.goal || "Agent run"}), h("span", {class: "rs num", text: state2})),
    pct != null ? h("div", {class: "rbar" + (over ? " over" : ""), role: "progressbar", "aria-valuenow": Math.round(pct), "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Run progress: " + steps + ", " + state}, h("i", {style: "width:" + pct.toFixed(1) + "%"})) : null,
    h("div", {class: "rmeta num" + (over ? " warn" : "")}, meta(steps, timeText, over ? "over the estimate by " + fmtMin(act - est) : null)),
    stepList, lanes);
}
/* saved status lines: parsed in Python (parts); legacy one-line forms are shown as written */
function ckClass(c) { const g = String(c || "").charAt(0); return g === "✓" ? "ok" : g === "⚠" ? "warn" : g === "✗" ? "bad" : "na"; }
/* the saved status block (Done, Check, Progress, Next), as one quiet list */
function statusCard(raw, parts, meta_) {
  if (!parts) return h("div", {class: "statusline"}, meta_ ? h("span", {class: "who", text: meta_}) : null, inline(stripMarker(raw)));
  const row = (k, v, cls) => v ? [h("dt", {text: k}), h("dd", {class: cls || null}, inline(v))] : null;
  return h("div", {class: "sblock"}, parts.task ? h("div", {class: "strong", text: parts.task}) : null,
    h("dl", {class: "sb-rows"}, row("Done", parts.done), row("Check", parts.check, "ck " + ckClass(parts.check)), row("Progress", parts.progress), row("Next", parts.next)),
    meta_ ? h("div", {class: "label", text: meta_}) : null);
}
function statusLine(raw, parts) {
  if (!parts) return inline(stripMarker(raw));
  return frag(parts.check ? [h("span", {class: "ck " + ckClass(parts.check), title: "Check: " + parts.check, text: parts.check.charAt(0)}), " "] : null, inline(parts.done || parts.task || ""),
    parts.next ? h("span", {class: "t2", text: ". Next: " + parts.next}) : null);
}
function logText(x) { return x.parts ? frag(x.pre ? inline(stripMarker(x.pre) + " · ") : null, statusLine(x.text, x.parts)) : inline(stripMarker(x.text)); }
/* one live session, each fact once: where, the run (goal, progress, ETA), then Latest / Check / Next from a single source */
function sessionBlock(s, who, asOf, hideProject) {
  const p = s.progress || null, sp = s.status_parts || null, now = nowS();
  const turn = s.turn_started ? now - s.turn_started : 0, tp90 = ((D.timing || {}).turns || {}).p90, slow = !!(tp90 && turn / 60 > tp90);
  const blocked = !!(p && p.state === "blocked");
  const latest = (p && p.last) || (sp && sp.done) || (!sp && s.status ? stripMarker(s.status) : "");
  const next = (p && p.next) || (sp && sp.next) || "", check = sp && sp.check;
  const goal = liveRun(s, asOf) || (sp && sp.task ? h("div", {class: "run"}, h("div", {class: "rh"}, h("strong", {class: "ell", text: sp.task}))) : null);
  return h("div", {class: "sess"},
    h("div", {class: "sh"}, hideProject ? null : s.project ? h("button", {class: "sp", type: "button", onclick: () => openProject(s.project), text: scopeName(s.project)}) : h("span", {text: "no project"}),
      s.branch ? h("span", {class: "br", title: "Branch " + s.branch}, icon("branch"), s.branch) : null, h("span", {text: s.agent || "agent"}), who ? h("span", {text: who}) : null,
      h("span", {class: "ss num", title: s.started ? "Started " + fmt(s.started) : null, text: s.started ? dur(now - s.started) : ""})),
    goal,
    latest || check || next ? h("dl", {class: "srows"}, latest ? [h("dt", {text: blocked ? "Blocked" : "Latest"}), h("dd", {class: blocked ? "bad" : null}, inline(latest), p && p.updated ? h("span", {class: "t2", text: " " + rel(p.updated)}) : null)] : null,
      check ? [h("dt", {text: "Check"}), h("dd", {class: "ck " + ckClass(check)}, inline(check))] : null,
      next ? [h("dt", {text: "Next"}), h("dd", null, inline(next))] : null) : null,
    turn || s.subagents ? h("div", {class: "rmeta" + (slow ? " warn" : "")}, meta(turn ? "turn running for " + dur(turn) + (slow ? ", longer than 90% of turns here" : "") : null, s.subagents ? plural(s.subagents, "subagent") + " running" : null)) : null);
}
/* ---------------------------------------------------------------- navbar + filters */
/* the views are navigation (links with aria-current), not tabs that control a panel. Priority+: every view renders in one row; after layout,
   on resize and when fonts load, the views that do not fit move from the end into More, so the row never scrolls sideways */
const NAV = {open: false, ro: null, frame: 0};
function navDots() {
  const urgent = needs().filter(a => a.sev === "risk").length, colls = (D.collisions || []).length, bg = bgAttention();
  return {now: D.attention ? nowDot() : urgent ? ["risk", plural(urgent, "urgent item")] : null, background: bg, team: colls ? ["warn", plural(colls, "collision")] : null};
}
function curView() { return S.view; }
function viewHref(v) { return encode(Object.assign({}, S, PAGEKEYS, {view: v, id: "", task: "", modal: ""})); }
function navDot(d) { return d ? h("span", {class: "n" + (d[0] === "warn" ? " warn" : d[0] === "you" ? " you" : ""), role: "img", title: d[1], "aria-label": d[1]}) : null; }
function renderNav() {
  const tabs = $("#tabs"), dots = navDots(), cur = curView(); clear(tabs); clear($("#navmore"));
  for (const [v, label, key] of VIEWS) tabs.append(h("a", {class: "tab" + (v === REFVIEW ? " sec" : ""), href: viewHref(v), "data-view": v, "aria-current": v === cur ? "page" : null, title: label + " (g " + key + ")",
    onclick: e => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return; e.preventDefault(); closeMore(); go(v); }}, label, navDot(dots[v])));
  watchNav(); fitNav();
  const q = $("#q"); if (document.activeElement !== q) q.value = S.q;
}
/* the fit depends on widths that can change after it with no resize: a web font that lands while another is still loading
   (document.fonts.ready and loadingdone wait for the slowest font, so the nav's own font can arrive long before them), the wordmark's font,
   a scrollbar. A ResizeObserver on the space the row has, the row and each view asks for a new fit when one of them changes size */
function watchNav() {
  if (typeof ResizeObserver !== "function") return;
  if (!NAV.ro) NAV.ro = new ResizeObserver(fitSoon);
  NAV.ro.disconnect();
  for (const el of [$("#nav .views"), $("#tabs")].concat($$(".tab", $("#tabs")))) if (el) NAV.ro.observe(el);
}
/* at most one fit per animation frame, and never inside the observer's callback, where moving views would make it loop */
function fitSoon() { if (!NAV.frame) NAV.frame = requestAnimationFrame(() => { NAV.frame = 0; fitNav(); }); }
function fitNav() {
  const tabs = $("#tabs"), wrap = $("#navmore"); if (!tabs || !wrap) return;
  const focusView = document.activeElement && document.activeElement.closest && document.activeElement.closest("#navmore .menu") ? document.activeElement.getAttribute("data-view") : "";
  const all = $$(".tab", tabs).concat($$(".menu a", wrap).map(a => a._tab).filter(Boolean));
  for (const t of all) tabs.append(t);   /* back in the row, in order */
  clear(wrap); wrap.hidden = true;
  if (tabs.scrollWidth <= tabs.clientWidth + 1) { NAV.open = false; return; }
  const dots = navDots(), cur = curView(), moved = [];
  const btn = h("button", {type: "button", id: "morebtn", "aria-haspopup": "menu", "aria-expanded": "false", "aria-controls": "moremenu"});
  const menu = h("div", {class: "menu", id: "moremenu", role: "menu", "aria-label": "More views", hidden: true});
  wrap.append(btn, menu); wrap.hidden = false;
  const label = () => { const c = moved.find(t => t.getAttribute("data-view") === cur), dot = moved.map(t => dots[t.getAttribute("data-view")]).filter(Boolean).sort((a, b) => (a[0] === "risk" ? 0 : 1) - (b[0] === "risk" ? 0 : 1))[0];
    btn.className = c ? "cur" : ""; append(clear(btn), [c ? c.firstChild.textContent : "More", icon("chevron"), navDot(dot || null)]);
    btn.setAttribute("aria-label", (c ? c.firstChild.textContent + ", current view. " : "") + "More views" + (dot ? ", " + dot[1] : "")); };
  label();
  const row = $$(".tab", tabs);
  while (row.length > 1 && tabs.scrollWidth > tabs.clientWidth + 1) { const t = row.pop(); moved.unshift(t); t.remove(); label(); }
  /* the moved views become menu items: name, attention dot, the shortcut on screens with a fine pointer; the current one is checked */
  moved.forEach((t, i) => {
    const v = t.getAttribute("data-view"), d = dots[v], spec = VIEWS.find(x => x[0] === v);
    if (i && v === REFVIEW) menu.append(h("hr", {role: "separator"}));
    const a = h("a", {role: "menuitem", href: t.getAttribute("href"), "data-view": v, tabindex: "-1", "aria-current": v === cur ? "page" : null,
      onclick: e => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); closeMore(true); go(v); }},
      h("span", {class: "mck", "aria-hidden": "true"}, v === cur ? icon("done") : null), spec[1], navDot(d), h("kbd", {"aria-hidden": "true", text: "g " + spec[2]}));
    a._tab = t; menu.append(a);
  });
  btn.addEventListener("click", () => NAV.open ? closeMore(true) : openMore(0));
  btn.addEventListener("keydown", e => { if (e.key === "ArrowDown") { e.preventDefault(); openMore(0); } else if (e.key === "ArrowUp") { e.preventDefault(); openMore(-1); } });
  menu.addEventListener("keydown", e => {
    const items = $$("a", menu), i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus(); }
    else if (e.key === "Home" || e.key === "End") { e.preventDefault(); items[e.key === "Home" ? 0 : items.length - 1].focus(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMore(true); }
    else if (e.key === "Tab") closeMore(false);
  });
  if (NAV.open) { menu.hidden = false; btn.setAttribute("aria-expanded", "true"); const f = focusView && $('a[data-view="' + focusView + '"]', menu); if (f) f.focus({preventScroll: true}); }
}
function openMore(at) {
  const btn = $("#morebtn"), menu = $("#moremenu"); if (!btn) return;
  NAV.open = true; menu.hidden = false; btn.setAttribute("aria-expanded", "true");
  const items = $$("a", menu); if (items.length) items[at < 0 ? items.length - 1 : at].focus();
}
function closeMore(refocus) {
  const btn = $("#morebtn"), menu = $("#moremenu"); NAV.open = false; if (!btn) return;
  menu.hidden = true; btn.setAttribute("aria-expanded", "false"); if (refocus) btn.focus();
}
function renderFilters() {
  const bar = $("#filters"); clear(bar);
  if (!((S.view === "projects" && !S.id) || S.view === "activity" || S.view === "search")) { bar.hidden = true; return; } bar.hidden = false;
  bar.append(h("span", {class: "label", text: "Filter"}),
    facetMenu("health", "Health", counts(p => p.health.level), v => ({ok: "Healthy", warn: "Needs a look", risk: "At risk"})[v] || v),
    facetMenu("ci", "CI", counts(p => p.ci), v => CHECKS[v] || v),
    facetMenu("status", "Status", counts(p => p.status), v => (STATUS[v] || [0, v])[1]), facetMenu("priority", "Priority", counts(p => p.priority)),
    facetMenu("owner", "Owner / host", counts(p => p.owner)), facetMenu("stack", "Stack", counts(p => p.stack)), facetMenu("tag", "Tag", counts(p => p.tags)));
  for (const [k, l] of [["live", "Live session"], ["prs", "Open PRs"], ["nc", "Not cloned"], ["loose", "Outside initiatives"]]) { const cb = h("input", {type: "checkbox", checked: S[k]}); cb.addEventListener("change", () => set({[k]: cb.checked}, true)); bar.append(h("label", {class: "toggle"}, cb, l)); }
  const chips = [];
  for (const k of ["health", "ci", "status", "priority", "owner", "stack", "tag"]) for (const v of S[k]) chips.push(h("span", {class: "chip"}, k + ": " + v, h("button", {type: "button", "aria-label": "Remove " + v, onclick: () => set({[k]: S[k].filter(x => x !== v)}, true), text: "×"})));
  if (facetCount()) bar.append(...chips, h("button", {class: "btn sm ghost", type: "button", onclick: () => set(CLEAR, true)}, "Clear"));
}

/* ---------------------------------------------------------------- today's actions: where each one opens, its row (Now's full list by kind) */
function actionTarget(a) {
  if (a.modal) return () => { if (a.at) OPENAT = a.at; openModal(a.modal[0], a.modal[1]); };
  if (a.view) return () => go(a.view);
  const tid = (/\b([TFBS]-[0-9a-z]{6,})\b/.exec(a.cmd || "") || [])[1];
  if (tid && ITEMBY.has(tid)) return () => openTask(tid);
  if (a.project && (BYID.has(a.project) || MEMBEROF.has(a.project))) return () => openProject(a.project);
  const slug = (/(?:initiative show|-i) ([\w.-]+)/.exec(a.cmd || "") || [])[1];
  if (slug && BYINIT.has(slug)) return () => openInitiative(slug);
  if (a.kind === "collision") return () => go("team");
  return null;
}
function actionHref(a) {
  if (a.modal) return modalHref(a.modal[0], a.modal[1]);
  if (a.view) return link({view: a.view});
  const tid = (/\b([TFBS]-[0-9a-z]{6,})\b/.exec(a.cmd || "") || [])[1];
  if (tid && ITEMBY.has(tid)) return taskHref(tid);
  if (a.project && (BYID.has(a.project) || MEMBEROF.has(a.project))) return link({view: "projects", id: a.project});
  const slug = (/(?:initiative show|-i) ([\w.-]+)/.exec(a.cmd || "") || [])[1];
  if (slug && BYINIT.has(slug)) return link({view: "initiatives", id: slug});
  if (a.kind === "collision") return link({view: "team"});
  return null;
}
const SEVL = {risk: "Urgent", warn: "Needs a look", info: "For information"};
const GROUP = {"ci-failing": n => n + " of your PRs are failing CI", "changes-requested": n => "Changes requested on " + n + " of your PRs", "conflicts": n => n + " of your PRs have merge conflicts",
  "review-waiting": n => n + " of your PRs are waiting for review", "review-requested": n => n + " PRs are waiting for your review", "blocked": n => n + " projects or initiatives are blocked",
  "blocked-task": n => n + " of your tasks are blocked", "overdue": n => n + " tasks or initiatives are overdue", "idle-work": n => n + " checkouts have unsaved work left idle",
  "layout": n => n + " repositories are outside the workspace layout", "stale-note": n => n + " handoff notes are behind the code", "expired-lock": n => n + " expired locks are still held", "collision": n => n + " collisions",
  "decision": n => n + " decisions wait for you", "ci-watch": n => "CI failed on " + n + " branches", "job-failed": n => n + " scheduled jobs failed", "setup": n => n + " setup issues"};
function actionWhere(a) {
  const p = a.project && BYID.get(a.project), n = (/#\d+/.exec(a.title || "") || [])[0], name = p ? p.name || p.id : a.project ? String(a.project).split("/").pop() : "";
  return n && name ? n + " in " + name : name || (a.modal ? a.detail : "") || a.title;
}
function actionGroups(list) {
  const out = [], by = new Map();
  for (const a of list) { if (!by.has(a.kind)) { const g = {kind: a.kind, items: []}; by.set(a.kind, g); out.push(g); } by.get(a.kind).items.push(a); }
  return out;
}
/* one thing that needs you: what, where, and a quiet copy icon (the first row of a screen gets the one amber button) */
function actionRow(a, primary) {
  const p = a.project && BYID.get(a.project), href = actionHref(a), go_ = actionTarget(a);
  const title = a.url ? h("a", {class: "at", href: a.url, target: "_blank", rel: "noopener noreferrer", "data-nav": "", title: "Open on GitHub", text: a.title})
    : href ? h("a", {class: "at", href, "data-nav": "", text: a.title}) : h("span", {class: "at", tabindex: "0", "data-nav": "", text: a.title});
  return h("li", {class: "act", onclick: rowClick(() => { if (go_) go_(); })},
    h("span", {class: "dot d-" + a.sev, role: "img", title: SEVL[a.sev], "aria-label": SEVL[a.sev]}),
    h("div", {style: "min-width:0"}, title, h("div", {class: "ad"}, h("span", {text: KIND[a.kind] || a.kind}), p ? h("span", {class: "proj", text: p.name || p.id}) : null, a.detail ? h("span", {class: "ell"}, a.modal || a.view ? a.detail : inline(a.detail)) : null)),
    h("div", {class: "ar"}, !a.cmd ? (href ? h("a", {class: "btn sm", href, onclick: e => { e.preventDefault(); e.stopPropagation(); if (go_) go_(); }}, a.go || "Open") : null)
      : primary ? cmdBtn(a.cmd, true, "Copy command") : icopy(a.cmd, "Copy command")));
}
function groupRow(g, primary) {
  const a = g.items[0], n = g.items.length;
  if (n === 1) return actionRow(a, primary);
  const label = KIND[a.kind] || a.kind, href = link({view: "now", all: true, kind: label}), num = (/#\d+/.exec(a.title || "") || [])[0];
  const where = g.items.slice(0, 3).map(actionWhere).join(", ") + (n > 3 ? " and " + (n - 3) + " more" : "");
  return h("li", {class: "act", onclick: rowClick(() => set({view: "now", id: "", task: "", all: true, kind: label}, true))},
    h("span", {class: "dot d-" + a.sev, role: "img", title: SEVL[a.sev], "aria-label": SEVL[a.sev]}),
    h("div", {style: "min-width:0"}, h("a", {class: "at", href, "data-nav": "", text: (GROUP[a.kind] || (k => k + " " + label + " items"))(n)}), h("div", {class: "ad"}, h("span", {class: "ell", text: where}))),
    h("div", {class: "ar"}, primary && a.cmd ? cmdBtn(a.cmd, true, "Copy command" + (num ? " for " + num : "")) : null));
}
function collText(c, short) {
  if (c.kind === "expired-lock") { const it = ITEMBY.get(c.id); return (it ? "“" + it.title + "”" : c.id) + " is still claimed by " + whoOf(c.holder) + "; the lock expired " + rel(iso(c.until)); }
  return (short ? "" : "Two sessions on ") + scopeName(c.project) + ", branch " + (c.branch || "?") + ": " + (c.who || []).join(" and ");
}
function notCloned(x) { return x.members.filter(m => { const p = BYID.get(m.id); return p ? !p.local : !m.local; }).length; }
function memberCounts(x) { const c = {blocked: 0, risk: 0}; for (const m of x.members) { const p = BYID.get(m.id), st = (p && p.status) || m.status; if (st === "blocked") c.blocked++; if (p && p.health.level === "risk") c.risk++; } return c; }
function nextDate(x) {
  const over = !!(x.due && x.status !== "done" && x.due < lkey(new Date()));
  const when = x.due ? (over ? "overdue since " : "due ") + shortDay(x.due) : "No due date";
  return h("div", {class: "nd", title: [x.milestone ? "Milestone: " + x.milestone : "", x.due ? (over ? "Overdue since " : "Due ") + x.due : ""].filter(Boolean).join(". ") || null},
    h("span", {class: "due" + (over ? " over" : "") + (x.due ? "" : " none"), text: when}), x.milestone ? h("span", {class: "ms ell", text: x.milestone}) : null);
}
function workText(c) { return h("span", null, h("span", {class: "num", text: c.open + " open"}), c.blocked ? h("span", {class: "bad num", text: ", " + c.blocked + " blocked"}) : null); }
/* one row per initiative (status, progress, next date, open and blocked work) */
/* an initiative card: the progress ring, the member waffle, the due pill and the people */
function initCard(x, compact) {
  const st = istate(x), c = x.cnt;
  return h("article", {class: "icard" + (compact ? " compact" : ""), onclick: rowClick(() => openModal("initiative", x.id))},
    h("div", {class: "ic-h"}, progRing(x, compact ? 44 : 52), h("div", {class: "ic-n"}, modalLink("initiative", x.id, x.title, "strong clamp2"),
      h("span", {class: "stat t2"}, h("span", {class: "dot k-" + st, "aria-hidden": "true"}), HS[st], compact ? null : h("span", {class: "num", text: c.open + " open" + (c.blocked ? ", " + c.blocked + " blocked" : "")})))),
    memberWaffle(x, compact ? 7 : 8, compact ? 26 : 28),
    h("div", {class: "ic-f"}, duePill(x.due, x.status === "done", "No due date"), x.people.length ? avatars(x.people, compact ? 3 : 5) : null));
}
function viewActions(main) {
  const NE = needs(), kinds = new Map(); for (const a of NE) { const l = KIND[a.kind] || a.kind; kinds.set(l, (kinds.get(l) || 0) + 1); }
  const list = NE.filter(a => !S.kind || (KIND[a.kind] || a.kind) === S.kind);
  main.append(crumbs([["Now", link({view: "now", all: false, kind: ""})], ["Needs you"]]));
  main.append(h("div", {class: "head"}, h("h1", {text: "Everything that needs you"}), h("span", {class: "sub", text: plural(NE.length, "item") + ", most urgent first"})));
  if (kinds.size > 1) main.append(h("div", {class: "toolbar"}, seg("Kind", "kind", [["", "All", NE.length]].concat(Array.from(kinds.entries()).map(([l, n]) => [l, l, n])))));
  main.append(list.length ? h("ul", {class: "acts"}, list.map((a, i) => actionRow(a, i === 0))) : h("div", {class: "panel"}, empty("All clear")));
}
/* ---------------------------------------------------------------- Initiatives: list -> #/initiatives/<slug> */
function roadmap(items) {
  if (!items.length) return null;
  const today = new Date(); today.setHours(12, 0, 0, 0); const t0 = today.getTime() / 1000;
  const ts = d => new Date(d + "T12:00:00").getTime() / 1000;
  let start = Math.min(t0 - 14 * 86400, ...items.map(x => ts(x.due))), end = Math.max(t0 + 45 * 86400, ...items.map(x => ts(x.due)));
  const span = end - start; start -= span * 0.02; end += span * 0.06;
  const pct = t => Math.max(0, Math.min(100, (t - start) / (end - start) * 100));
  const ticks = []; const d = new Date(start * 1000); d.setDate(1); d.setHours(12, 0, 0, 0); d.setMonth(d.getMonth() + 1);
  while (d.getTime() / 1000 < end) { ticks.push([d.getTime() / 1000, d.toLocaleDateString(undefined, {month: "short"})]); d.setMonth(d.getMonth() + 1); }
  const track = (kids) => h("div", {class: "road-track"}, ticks.map(([t]) => h("span", {class: "road-tick", style: "left:" + pct(t).toFixed(2) + "%"})), h("span", {class: "road-today", style: "left:" + pct(t0).toFixed(2) + "%"}), kids);
  const tp = pct(t0);
  const rows = [h("div", {class: "road-row road-axis"}, h("span"), h("div", {class: "road-track"}, ticks.filter(([t]) => Math.abs(pct(t) - tp) > 7).map(([t, l]) => h("span", {class: "road-tl", style: "left:" + pct(t).toFixed(2) + "%", text: l})),
    h("span", {class: "road-tl today", style: "left:" + pct(t0).toFixed(2) + "%", text: "Today"})))];
  for (const x of items.sort((a, b) => a.due.localeCompare(b.due))) {
    const p = pct(ts(x.due)), over = x.status !== "done" && x.due < lkey(new Date()), right = p > 72;
    rows.push(h("div", {class: "road-row", "data-nav": "", tabindex: "0", role: "link", "aria-label": x.label + ", due " + x.due, onclick: x.open, onkeydown: e => { if (e.key === "Enter") x.open(); }},
      h("span", {class: "road-l ell", title: x.label}, h("span", {class: "dot " + (SDOT[x.status] || "d-info"), "aria-hidden": "true"}), x.label),
      track([h("span", {class: "road-m" + (over ? " over" : x.status === "done" ? " done" : ""), style: "left:" + p.toFixed(2) + "%", title: (over ? "Overdue · " : "Due ") + x.due + (x.milestone ? " · " + x.milestone : "")}),
        h("span", {class: "road-t" + (right ? " r" : ""), title: x.label + ", " + shortDay(x.due) + (x.milestone ? ": " + x.milestone : "") + (over ? " (overdue)" : ""),
          style: (right ? "right:" + (100 - p).toFixed(2) : "left:" + p.toFixed(2)) + "%;max-width:calc(" + (right ? p : 100 - p).toFixed(2) + "% - 14px)", text: shortDay(x.due) + (x.milestone ? ": " + x.milestone : "") + (over ? " (overdue)" : "")})])));
  }
  return h("div", {class: "road", role: "list", "aria-label": "Milestones and due dates"}, rows);
}
function sortInits(list) {
  const due = x => x.due || "9999";
  const by = {priority: (a, b) => PRANK[a.priority] - PRANK[b.priority] || due(a).localeCompare(due(b)), progress: (a, b) => (progOf(a).pct ?? -1) - (progOf(b).pct ?? -1),
    due: (a, b) => due(a).localeCompare(due(b)), activity: (a, b) => (b.last_activity || 0) - (a.last_activity || 0)};
  return list.slice().sort(by[S.psort] || by.priority);
}
/* Initiatives: one quiet table that carries what the cards did (goal, progress, next date, work, members, people) */
function initTable(list) {
  return h("div", {class: "tw st"}, h("table", {class: "t pt it"},
    h("thead", null, h("tr", null, h("th", {class: "c-health"}, h("span", {class: "sr", text: "Status"})), h("th", {text: "Initiative"}), h("th", {class: "c-prog", text: "Progress"}), h("th", {class: "c-ms", text: "Next date"}), h("th", {class: "c-work", text: "Tasks"}), h("th", {class: "c-st", text: "Projects"}), h("th", {class: "c-who", text: "People"}), h("th", {class: "c-act", text: "Last activity"}))),
    h("tbody", null, list.map(x => { const mc = memberCounts(x), l = (STATUS[x.status] || STATUS.active)[1];
      return h("tr", {onclick: rowClick(() => openModal("initiative", x.id))},
        h("td", {class: "c-health"}, h("span", {class: "dot " + (SDOT[x.status] || "d-info"), role: "img", title: l, "aria-label": l})),
        h("td", {class: "c-name"}, modalLink("initiative", x.id, x.title, "pname"), h("span", {class: "powner", title: plain(x.goal || ""), text: x.goal ? plain(x.goal) : l + (x.priority === "high" ? ", high priority" : "")})),
        h("td", {class: "c-prog"}, progressBar(x)), h("td", {class: "c-ms"}, nextDate(x)), h("td", {class: "c-work"}, workText(x.cnt)),
        h("td", {class: "c-st num"}, String(x.members.length), mc.blocked ? h("span", {class: "bad", text: " (" + mc.blocked + " blocked)"}) : null),
        h("td", {class: "c-who"}, avatars(x.people, 3)), h("td", {class: "c-act num t2", text: rel(x.last)})); }))));
}
function viewInitiatives(main) {
  if (S.id) { viewInitiative(main); return; }
  const tk = tokens(S.q), all = INITS.filter(x => tk.every(t => x.hay.includes(t)));
  const list = sortInits(all.filter(x => !S.ist || x.status === S.ist));
  const inInit = new Set(INITS.flatMap(x => x.projects)), loose = P.filter(p => !MEMBEROF.has(p.id)).length;
  main.append(h("div", {class: "head"}, h("h1", {text: "Initiatives"}), h("span", {class: "sub", text: plural(INITS.length, "initiative") + " across " + plural(inInit.size, "project")})));
  const st = {}; for (const x of all) st[x.status] = (st[x.status] || 0) + 1;
  main.append(h("div", {class: "toolbar"}, seg("Status", "ist", [["", "All", all.length]].concat(Object.keys(st).sort((a, b) => (SORDER[a] ?? 6) - (SORDER[b] ?? 6)).map(k => [k, (STATUS[k] || [0, k])[1], st[k]]))),
    h("span", {class: "spacer"}), legend(hparts(tally(INITS.flatMap(x => x.members.map(memberState))))), selectBox("Sort", "psort", [["priority", "Sort by priority"], ["due", "Sort by due date"], ["progress", "Sort by progress"], ["activity", "Sort by activity"]]),
    seg("Layout", "ilayout", [["cards", "Cards"], ["table", "Table"]])));
  if (!list.length) main.append(h("div", {class: "panel mb"}, empty(INITS.length ? "No initiative matches" : "No initiatives yet", INITS.length ? "Clear the search or the status filter." : ["Group related projects into a programme with goal, milestone and due date: ", h("code", {text: 'dev initiative new <slug> --title "…" --projects owner/a,owner/b'})])));
  else main.append(h("div", {class: "mb"}, S.ilayout === "table" ? initTable(list) : h("div", {class: "icards"}, list.map(x => initCard(x)))));
  const dated = list.filter(x => x.due).map(x => ({label: x.title, due: x.due, milestone: x.milestone, status: x.status, open: () => openModal("initiative", x.id)}));
  if (dated.length) main.append(panel("Milestones and due dates", dated.length, null, roadmap(dated.slice(0, 16)), {cls: "mb"}));
  if (loose) main.append(h("p", {class: "t2", style: "font-size:var(--fs-1)"}, h("a", {href: link({view: "projects", loose: true}), text: plural(loose, "project") + " are outside initiatives."}), " Add one with ", h("code", {text: "dev initiative add <slug> <owner/repo>"}), "."));
}
const ITABS = [["overview", "Overview"], ["projects", "Projects"], ["tasks", "Tasks"], ["plans", "Plans"], ["sessions", "Sessions"], ["docs", "Docs & source"]];
function viewInitiative(main) {
  const x = BYINIT.get(S.id);
  if (!x) { main.append(crumbs([["Initiatives", link({view: "initiatives", id: ""})], [S.id]]), h("div", {class: "panel"}, empty("No initiative “" + S.id + "”", h("a", {href: link({view: "initiatives", id: ""})}, "All initiatives")))); return; }
  const tab = ITABS.some(t => t[0] === S.tab) ? S.tab : "overview";
  const plansL = plansFor([x.scope]), sess = sessionsFor([x.scope].concat(x.projects), x.sessions), st = istate(x), c = x.cnt, mc = tally(x.members.map(memberState));
  main.append(crumbs([["Initiatives", link({view: "initiatives", id: "", tab: ""})], [x.title]]));
  main.append(h("header", {class: "phead"},
    h("div", {class: "pt1"}, h("div", {class: "mh-row"}, progRing(x, 64, true), h("div", {class: "mh-tt"}, h("h1", {text: x.title}),
        h("div", {class: "pline"}, meta(h("span", {class: "stat"}, h("span", {class: "dot k-" + st, "aria-hidden": "true"}), HS[st]), duePill(x.due, x.status === "done", "No due date"),
          x.milestone ? h("span", {class: "msl", title: "Milestone: " + x.milestone, text: x.milestone}) : null, x.owner ? "owner " + x.owner : null)))),
      h("div", {class: "go"}, x.note && FILEMAP.has(x.note) ? h("a", {class: "btn ghost", href: link({view: "explorer", f: x.note, id: ""})}, "README") : null,
        cmdMenu("Commands", [["Show it in the terminal", "dev initiative show " + x.id], ["Add a task", "dev task -i " + x.id + ' add "…" --now'], ["Add a member project", "dev initiative add " + x.id + " <owner/repo>"]])))));
  const taskParts = [["mark", c.doing, "Doing"], ["risk", c.blocked, "Blocked"], ["none", c.todo, "To do"]];
  main.append(h("div", {class: "g12 tiles"},
    statTile({label: "Member projects", value: x.members.length, href: link({tab: "projects"}), viz: memberWaffle(x, 8, 32), legend: legend(hparts(mc)), cls: "sp-4"}),
    statTile({label: "Open tasks", value: c.open, href: link({tab: "tasks"}), viz: segBar(taskParts, "Open tasks"), legend: legend(taskParts), cls: "sp-3"}),
    statTile({label: "Done, 14 days", value: doneSeries(x.items, 14).reduce((a, d) => a + d[1], 0), viz: spark(doneSeries(x.items, 14), "Tasks done per day", "task", true), cls: "sp-3"}),
    statTile({label: "People on it", value: x.people.length, href: link({view: "team", id: ""}), viz: x.people.length ? avatars(x.people, 6) : h("span", {class: "t2", text: "Nobody right now"}), cls: "sp-2"})));
  main.append(pageTabs([["overview", "Overview"], ["projects", "Projects", x.members.length], ["tasks", "Tasks", c.open], ["plans", "Plans", plansL.length], ["sessions", "Sessions", sess.length], ["docs", "Docs & source"]], tab));
  const tp = h("div", {class: "tabpanel", id: "tabpanel", role: "tabpanel", "aria-labelledby": "tab-" + tab});
  main.append(tp);
  const part = (label, text) => text ? [h("h3", {class: "sub3", text: label}), md(text)] : null;
  if (tab === "overview") {
    const doing = sortItems(x.items.filter(i => i.status === "doing" || i.status === "blocked"));
    const stuck = x.members.map(m => BYID.get(m.id) || {id: m.id, name: m.id.split("/").pop(), status: m.status, health: {level: "ok", reasons: []}}).filter(p => hstate(p) === "risk");
    tp.append(h("div", {class: "g12"},
      h("div", {class: "stack-v sp-8"},
        panel("Doing or blocked", doing.length, h("a", {class: "more", href: link({tab: "tasks"})}, "All tasks"), doing.length ? h("ul", {class: "irows"}, doing.slice(0, 8).map(i => itemRow(i, {scope: true, home: x.scope}))) : empty("Nothing in progress", "Claim the next task: ", h("code", {text: "dev task -i " + x.id + " start <id>"})), {flush: !!doing.length}),
        stuck.length ? panel("Off-track projects", stuck.length, h("a", {class: "more", href: link({tab: "projects", mg: "status"})}, "All projects"),
          h("div", {class: "ptiles sm"}, stuck.slice(0, 8).map(p => BYID.has(p.id) ? projectCard(p) : h("article", {class: "ptile", onclick: rowClick(() => openProject(p.id))}, h("div", {class: "pt-h"}, h("span", {class: "dot k-risk", "aria-hidden": "true"}), h("div", {class: "pt-n"}, linkTo(link({view: "projects", id: p.id}), p.name || p.id, "strong ell"))))))) : null,
        (x.goal || x.scopeText || x.state) ? panel("Goal and state", null, null, [part("Goal", x.goal), part("Scope", x.scopeText), part("Current state", x.state)]) : null),
      h("div", {class: "stack-v sp-4"}, whoPanel(x.people, x.projects.concat([x.scope])),
        panel("Risks and decisions", null, null, (x.risks || x.decisions) ? [part("Risks", x.risks), part("Decisions", x.decisions)] : empty("Nothing recorded yet", "Risks and decisions live in the initiative README.")),
        x.links ? panel("Links", null, null, md(x.links)) : null)));
  } else if (tab === "projects") tp.append(membersBlock(x));
  else if (tab === "tasks") tp.append(itemsBlock(x.items, {scope: x.scope, initiative: true}));
  else if (tab === "plans") tp.append(plansBlock(plansL, x.scope));
  else if (tab === "sessions") tp.append(sessionsBlock(sess, true));
  else {
    const docs = (x.docs || []).filter(d => FILEMAP.has(d));
    tp.append(h("div", {class: "cols even"}, panel("Source", null, null, x.source ? md(x.source) : empty("No source archive recorded")),
      panel("Documents", docs.length, x.note && FILEMAP.has(x.note) ? h("a", {class: "more", href: link({view: "explorer", f: x.note, id: ""})}, "README") : null,
        docs.length ? h("ul", {class: "tl-rows"}, docs.map(d => h("li", {class: "trow"}, h("a", {href: link({view: "explorer", f: d, id: ""}), text: d.split("/").slice(2).join("/") || d})))) : empty("No documents", "Add Markdown under ", h("code", {text: "initiatives/" + x.id + "/docs/"}))),
      x.tags.length ? panel("Tags", x.tags.length, null, h("div", null, x.tags.map(t => h("span", {class: "stk", text: t})))) : null));
  }
}
function whoPanel(people, scopes) {
  const set_ = new Set(scopes);
  const rows = people.map(w => { const x = BYWHO.get(w) || {sessions: [], locks: []};
    const here = x.sessions.filter(s => set_.has(s.project)), locks = ITEMS.filter(i => i.holder === w && i.open && i.lock === "held" && set_.has(i.scope));
    return h("li", {class: "prow", onclick: rowClick(() => go("team"))}, avatar(w),
      h("div", {class: "pm"}, h("div", {class: "pw"}, linkTo(link({view: "team", id: ""}), w, "ell"), isMe(w) ? h("span", {class: "tag", text: "you"}) : null),
        h("div", {class: "ps ell", text: [here.length ? here.map(s => (s.agent || "agent") + " on " + scopeName(s.project) + (s.branch ? ", " + s.branch : "")).join("; ") : null, !here.length && locks.length ? plural(locks.length, "claimed task") : null].filter(Boolean).join("") || "No live session here"})),
      h("div", {class: "pr2"}, locks.length ? h("span", {class: "claim", title: plural(locks.length, "lock") + " held here"}, lockIcon(), String(locks.length)) : null)); });
  return panel("Who is on it", people.length || null, h("a", {class: "more", href: link({view: "team", id: ""})}, "Team"), rows.length ? h("ul", {class: "prows"}, rows) : empty("Nobody right now", "People appear here when they hold a task lock or run a session in a member project."), {flush: !!rows.length});
}
function family(ids) {
  const toks = ids.map(id => String(id).split("/").pop().toLowerCase().split(/[-_.]/));
  let k = 0; while (toks.length > 1 && toks.every(t => t.length > k + 1 && t[k] === toks[0][k])) k++;
  return id => { const t = String(id).split("/").pop().toLowerCase().split(/[-_.]/); return t.length > k ? t[k] : t[t.length - 1]; };
}
function miniStatus(list) {
  const c = {}; for (const d of list) c[d.st] = (c[d.st] || 0) + 1;
  return h("span", {class: "mini", "aria-label": Object.entries(c).map(([k, n]) => n + " " + k).join(", ")}, Object.keys(c).sort((a, b) => (SORDER[a] ?? 6) - (SORDER[b] ?? 6)).map(k => h("span", {title: c[k] + " " + (STATUS[k] || [0, k])[1].toLowerCase()}, h("span", {class: "dot " + (SDOT[k] || "d-info"), "aria-hidden": "true"}), String(c[k]))));
}
function memberTable(list, allNc) {
  return h("div", {class: "tw st"}, h("table", {class: "t pt mtab"},
    h("thead", null, h("tr", null, h("th", {class: "c-health"}), h("th", {text: "Project"}), h("th", {class: "c-st", text: "Status"}), h("th", {class: "c-prog", text: "Progress"}), h("th", {text: "In progress or latest status"}), h("th", {class: "c-who", text: "Who"}), h("th", {class: "c-cta"}))),
    h("tbody", null, list.map(d => h("tr", {onclick: rowClick(() => openProject(d.id))},
      h("td", {class: "c-health"}, d.p ? healthDot(d.p) : h("span", {class: "dot d-info", role: "img", title: "No handoff note on this Mac yet", "aria-label": "no note"})),
      h("td", {class: "c-name"}, linkTo(link({view: "projects", id: d.id}), d.name, "pname"), h("span", {class: "powner", title: d.ws || d.id, text: d.ws || d.owner})),
      h("td", {class: "c-st"}, statusBadge(d.st in STATUS ? d.st : "untracked")),
      h("td", {class: "c-prog"}, d.pct != null ? progressBar(d.p || {progress: d.pct, t: {}}) : h("span", {class: "muted", text: "—"})),
      h("td", {class: "c-now"}, d.doing.length ? h("div", {class: "slt"}, h("div", {class: "ell", title: d.doing[0].title}, istatusIcon(d.doing[0].status), " ", d.doing[0].title), d.doing.length > 1 ? h("div", {class: "who", text: "+" + (d.doing.length - 1) + " more in progress"}) : null)
        : d.sl ? h("div", {class: "slt"}, h("div", {class: "ell", title: plain(stripMarker(d.sl.line))}, statusLine(d.sl.line, d.sl.parts)), h("div", {class: "who", text: (d.sl.agent || "agent") + " · " + rel(d.sl.ts)})) : h("span", {class: "muted", text: "—"})),
      h("td", {class: "c-who"}, avatars(d.people, 3)),
      h("td", {class: "c-cta"}, d.local ? null : h("span", {class: "nc"}, allNc ? null : h("span", {class: "tag", title: "Not cloned on this Mac", text: "not cloned"}), icopy("dev clone " + d.id, "Clone"))))))));
}
function membersBlock(x) {
  const mem = x.members.map(m => { const p = BYID.get(m.id), st = (p && p.status) || m.status || "unknown";
    return {id: m.id, p, name: (p && p.name) || m.id.split("/").pop(), owner: m.id.includes("/") ? m.id.slice(0, m.id.lastIndexOf("/")) : "(local)", st: st in STATUS ? st : "unknown", local: p ? p.local : !!m.local,
      pct: p ? progOf(p).pct : (m.progress != null ? m.progress : null), doing: p ? sortItems(p.items.filter(i => i.status === "doing" || i.status === "blocked")) : [],
      sl: m.status_line || (p && p.status_line), ws: plain(m.workstream || ""), people: p ? p.people : []}; });
  for (const d of mem) d.hay = [d.id, d.st, d.ws, d.doing.map(i => i.title).join(" "), d.people.join(" "), d.local ? "cloned" : "not cloned"].join(" ").toLowerCase();
  const fam = family(mem.map(d => d.id)), fc = {};
  for (const d of mem) { d.fam = fam(d.id); fc[d.fam] = (fc[d.fam] || 0) + 1; }
  for (const d of mem) if (fc[d.fam] < 2) d.fam = "other";
  const nFam = new Set(mem.map(d => d.fam)).size, famOk = nFam >= 2 && nFam <= 16, hasWs = mem.some(d => d.ws);
  const groupings = [["status", "Status"]].concat(famOk ? [["family", "Family"]] : []).concat([["owner", "Owner"]]).concat(hasWs ? [["ws", "Workstream"]] : []).concat([["none", "None"]]);
  const g = groupings.some(o => o[0] === S.mg) ? S.mg : mem.length > 12 ? (famOk ? "family" : "status") : "none";
  const input = h("input", {id: "mq", type: "search", class: "fin", placeholder: "Filter " + plural(mem.length, "project") + "…", value: S.mq, "aria-label": "Filter member projects", autocomplete: "off"});
  const count = h("span", {class: "count t2", role: "status"});
  const box = h("div", {class: "stack-v"}), allNc = mem.length > 0 && mem.every(d => !d.local);
  function draw() {
    const tk = tokens(input.value), shown = mem.filter(d => tk.every(t => d.hay.includes(t)));
    count.textContent = shown.length === mem.length ? plural(mem.length, "project") : shown.length + " of " + mem.length;
    clear(box);
    if (!mem.length) { box.append(h("div", {class: "panel"}, empty("No member projects", h("code", {text: "dev initiative add " + x.id + " <owner/repo>"})))); return; }
    if (!shown.length) { box.append(h("div", {class: "panel"}, empty("No project matches “" + input.value + "”", "Search covers names, status, workstreams, tasks in progress and people."))); return; }
    const key = d => g === "status" ? d.st : g === "owner" ? d.owner : g === "family" ? d.fam : g === "ws" ? (d.ws ? d.ws.split(/[.:;—]/)[0].trim().slice(0, 48) : "No workstream") : "";
    const groups = new Map(); for (const d of shown.slice().sort((a, b) => (SORDER[a.st] ?? 6) - (SORDER[b.st] ?? 6) || a.name.localeCompare(b.name))) { const k = key(d); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(d); }
    const order = Array.from(groups.keys()).sort((a, b) => g === "status" ? (SORDER[a] ?? 6) - (SORDER[b] ?? 6) : a === "other" ? 1 : b === "other" ? -1 : groups.get(b).length - groups.get(a).length || a.localeCompare(b));
    if (groups.size === 1) { box.append(h("div", {class: "panel"}, memberTable(shown, allNc))); return; }
    order.forEach((k, gi) => { const list = groups.get(k), ncl = list.filter(d => !d.local).length;
      const det = h("details", {class: "mgrp panel", open: tk.length > 0 || gi < 3 || groups.size <= 4},
        h("summary", null, h("span", {class: "mg-n", text: g === "status" ? (STATUS[k] || [0, k])[1] : k}), h("span", {class: "count num", text: String(list.length)}), miniStatus(list),
          h("span", {class: "mg-c t2", text: ncl ? (ncl === list.length ? "none cloned" : ncl + " not cloned") : ""})), memberTable(list, allNc));
      box.append(det); });
  }
  input.addEventListener("input", debounce(() => { S.mq = input.value; history.replaceState(null, "", encode(S)); store.set("devkit.state", S); draw(); }, 120));
  draw();
  const nc = mem.filter(d => !d.local).length;
  return h("div", {class: "stack-v", style: "gap:var(--s4)"},
    nc ? callout("info", nc === mem.length ? "None of these projects is cloned on this Mac" : nc + " of " + mem.length + " projects are not cloned on this Mac", "Their handoff notes, tasks and sessions come from the handoff repository. Clone the ones you work on; the copy icon on each row gives the command.", cmdBtn("dev clone <owner/repo>")) : null,
    h("div", {class: "toolbar"}, input, count, h("span", {class: "spacer"}), seg("Group by", "mg", groupings, g)), box);
}
function itemsBlock(items, opts) {
  opts = opts || {};
  const src = opts.initiative ? S.src || "all" : "all";
  const list = !opts.initiative || src === "all" ? items : src === "own" ? items.filter(i => i.scope === opts.scope) : items.filter(i => i.scope !== opts.scope);
  const st = S.tst || "open", c = cnt(list), closed = c.done + c.cancelled;
  const shown = sortItems(list.filter(i => st === "all" ? true : st === "open" ? i.open : st === "done" ? !i.open : i.status === st));
  const legacyOnly = items.length && items.every(i => i.legacy);
  const tb = h("div", {class: "toolbar"}, seg("Status", "tst", [["open", "Open", c.open], ["doing", "Doing", c.doing], ["blocked", "Blocked", c.blocked], ["todo", "To do", c.todo], ["done", "Closed", closed], ["all", "All", list.length]], st),
    opts.initiative ? seg("Source", "src", [["all", "All"], ["own", "Initiative"], ["projects", "From projects"]], src) : null, h("span", {class: "spacer"}), cmdBtn("dev task " + flagOf(opts.scope) + ' add "…" --now'));
  const out = h("div", {class: "stack-v"}, tb);
  if (legacyOnly) out.append(callout("info", "These tasks are checkboxes in the handoff note", "They have no id, lock or history yet. The next ", h("code", {text: "dev sync"}), " imports them as work items."));
  if (!shown.length) { out.append(h("div", {class: "panel"}, empty(list.length ? "Nothing " + (st === "open" ? "open" : st) : "No tasks yet", list.length ? "Switch the status filter to see the rest." : ["Plan the work: ", h("code", {text: "dev task " + flagOf(opts.scope) + ' add "…" --now'})]))); return out; }
  if (st === "open" || st === "all") {
    for (const [b, label] of [["now", "Now"], ["next", "Next"], ["later", "Later"]]) { const g = shown.filter(i => i.open && i.bucket === b); if (g.length) out.append(panel(label, g.length, null, h("ul", {class: "irows"}, g.map(i => itemRow(i, {scope: true, home: opts.scope}))), {flush: true})); }
    const done = shown.filter(i => !i.open);
    if (done.length) out.append(panel("Closed", done.length, null, h("ul", {class: "irows"}, done.slice(0, 50).map(i => itemRow(i, {scope: true, home: opts.scope}))), {flush: true}));
  } else out.append(panel(st === "done" ? "Closed" : (ISTATUS[st] || [0, st])[1], shown.length, null, h("ul", {class: "irows"}, shown.slice(0, 200).map(i => itemRow(i, {scope: true, home: opts.scope}))), {flush: true}));
  return out;
}
// a page's plans: those whose home is the page's scope, sub-folders included (home and group come from read_plans)
function plansFor(scopes) { const s = new Set(scopes); return (D.plans || []).filter(p => s.has(p.home || p.scope)).sort((a, b) => String(b.updated || "").localeCompare(String(a.updated || "")) || (b.mtime || 0) - (a.mtime || 0)); }
function planRow(pl) {
  return h("li", {class: "pl"},
    h("div", {class: "pl-h"}, h("span", {class: "badge pl-" + pl.status, title: "Plan status", text: pl.status}),
      FILEMAP.has(pl.path) ? h("a", {class: "pl-t", href: link({view: "explorer", f: pl.path, id: ""}), text: pl.title}) : h("span", {class: "pl-t", text: pl.title}),
      pl.version ? h("span", {class: "muted num", text: "v" + pl.version}) : null, pl.id && pl.id !== "—" ? h("span", {class: "iid mono", text: pl.id}) : null,
      pl.needs_revision ? h("span", {class: "badge warn-b", title: "The body changed without dev plan revise --why", text: "edited · needs a revision"}) : null,
      h("span", {class: "pl-u t2", text: [pl.updated_by, pl.updated ? rel(iso(pl.updated)) : pl.mtime ? rel(pl.mtime) : ""].filter(Boolean).join(" · ")})),
    pl.needs_revision ? h("div", {class: "pl-c"}, cmdBtn("dev plan revise " + D.handoff + "/" + pl.path + ' --why "…"')) : null,
    pl.changelog && pl.changelog.length ? h("details", {class: "pl-log"}, h("summary", null, "History · " + plural(pl.changelog.length, "version")), changelog(pl.changelog)) : null);
}
// the page's own plans first, then one panel per sub-folder of its plans folder, by folder name. Beside the folders the
// page's own plans get a heading and a label too, so heading navigation reaches them; alone, the selected tab names them
function plansBlock(list, scope, legacyList) {
  if (!list.length && legacyList && legacyList.length) list = legacyList.map(pl => ({title: pl.title, path: pl.path, status: "unversioned", version: "", updated: "", updated_by: "", mtime: pl.mtime, changelog: []}));
  if (!list.length) return h("div", {class: "panel"}, empty("No plans yet", "Start one: ", h("code", {text: "dev plan " + flagOf(scope) + ' new "…"'}), " — plans and specs are versioned with who, when and why."));
  const rows = l => h("ul", {class: "plans"}, l.map(planRow)), top = list.filter(pl => !pl.group);
  const groups = [...new Set(list.map(pl => pl.group).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  if (!groups.length) return h("div", {class: "panel"}, rows(list));
  const own = "Plans in " + (isInit(scope) ? scope.slice(12) : scope);
  return h("div", {class: "stack-v"}, top.length ? panel("Plans", top.length, null, rows(top), {flush: true, label: own}) : null,
    groups.map(g => { const l = list.filter(pl => pl.group === g); return panel(g + "/", l.length, null, rows(l), {flush: true, label: "Plans in " + g + "/"}); }));
}
function sessionsFor(scopes, legacyLines) {
  const s = new Set(scopes), out = (D.session_log || []).filter(x => s.has(x.scope));
  if (!out.length) for (const l of legacyLines || []) out.push({ts: sessTs(l), scope: scopes[0], who: "", agent: "", branch: "", text: sessText(l)});
  return out.sort((a, b) => b.ts - a.ts);
}
const SHOWN = {};
function sessionsBlock(list, showScope) {
  if (!list.length) return h("div", {class: "panel"}, empty("No sessions logged", "devkit adds a line after each agent session that commits or changes files."));
  const n = SHOWN.sessions || 60, ul = h("ul", {class: "tl"}); let day = "";
  for (const it of list.slice(0, n)) {
    const k = lkey(new Date(it.ts * 1000));
    if (k !== day) { day = k; ul.append(h("li", {class: "day", text: fmtDay(it.ts)})); }
    ul.append(h("li", {class: "it"}, h("span", {class: "tm", text: tm(it.ts)}), it.who ? avatar(it.who) : h("span", {class: "ki", "aria-hidden": "true", text: "◇"}),
      h("div", {style: "min-width:0"}, h("div", {class: "slw"}, meta(it.who, it.agent, it.branch ? h("span", {class: "meta"}, icon("branch"), it.branch) : null, showScope && it.scope ? scopeChip(it.scope) : null)), h("div", null, logText(it)))));
  }
  return h("div", {class: "panel"}, ul, list.length > n ? h("div", {class: "more"}, h("button", {class: "btn sm", type: "button", onclick: () => { SHOWN.sessions = n + 200; render(); }}, "Show " + Math.min(200, list.length - n) + " more")) : null);
}

/* ---------------------------------------------------------------- Projects: table -> #/projects/<owner>/<repo> */
function sortProjects(ps) {
  const by = {attention: (a, b) => HRANK[a.health.level] - HRANK[b.health.level] || PRANK[a.priority] - PRANK[b.priority] || (b.last_activity || 0) - (a.last_activity || 0),
    activity: (a, b) => (b.last_activity || 0) - (a.last_activity || 0), priority: (a, b) => PRANK[a.priority] - PRANK[b.priority] || (b.last_activity || 0) - (a.last_activity || 0),
    name: (a, b) => a.id.localeCompare(b.id), tasks: (a, b) => b.t.open - a.t.open, size: (a, b) => (b.size_kb || 0) - (a.size_kb || 0)};
  return ps.slice().sort(by[S.sort] || by.attention);
}
function latest(p) { const sl = p.status_line; if (!sl) return h("span", {class: "muted", text: "—"});
  return h("div", {class: "slt"}, h("div", {class: "ell", title: plain(stripMarker(sl.line))}, statusLine(sl.line, sl.parts)), h("div", {class: "who", text: (sl.agent || "agent") + " · " + rel(sl.ts)})); }
function nowCell(p) {
  const d = sortItems(p.items.filter(i => i.status === "doing" || i.status === "blocked"));
  if (!d.length) return h("span", {class: "muted", text: p.cnt.open ? plural(p.cnt.open, "open task") : "—"});
  return h("div", {class: "slt"}, h("div", {class: "ell", title: d[0].title}, istatusIcon(d[0].status), " ", d[0].title), h("div", {class: "who"}, d[0].holder ? (isMe(d[0].holder) ? "you" : d[0].holder) : "unclaimed", d.length > 1 ? " · +" + (d.length - 1) + " more" : ""));
}
/* the project list's count of branches not backed up: a link to the map when there are some; unknown is "—", never 0 */
function riskCell(p) {
  if (!branchesKnown(p)) return h("span", {class: "muted", title: p.local ? "No branch map yet" : "Not cloned on this Mac", text: "—"});
  const c = branchCounts(p.branches);
  if (!c.risk) return h("span", {class: "num t2", title: plural(c.total, "branch", "branches") + ", none only on this Mac", text: "0"});
  return h("a", {class: "stat num", href: link({view: "projects", id: p.id, tab: "branches"}), "data-nav": "", "aria-label": plural(c.risk, "branch", "branches") + " not backed up: open the branch map"}, stateDot("risk"), String(c.risk));
}
function projectTable(ps) {
  const th = (label, key, cls) => h("th", {scope: "col", class: cls || null, "aria-sort": S.sort === key ? "descending" : null}, key ? h("button", {type: "button", onclick: () => set({sort: key}, true), text: label + (S.sort === key ? " ↓" : "")}) : label);
  return h("div", {class: "tw st"}, h("table", {class: "t pt"},
    h("thead", null, h("tr", null, th("", "attention", "c-health"), th("Project", "name"), th("Status", "priority", "c-st"), h("th", {class: "c-prog", text: "Progress"}), th("In progress", "tasks"), h("th", {text: "Latest agent status"}), h("th", {text: "PRs"}), h("th", {class: "c-bk", text: "Not backed up"}), h("th", {class: "c-act", text: "7 days"}), th("Last activity", "activity", "c-last"), h("th", {class: "c-who", text: "Who"}))),
    h("tbody", null, ps.map(p => h("tr", {onclick: rowClick(() => openModal("project", p.id))},
      h("td", {class: "c-health"}, healthDot(p)),
      h("td", {class: "c-name"}, h("span", {class: "pname"}, modalLink("project", p.id, p.name || p.id), ONPROJ.has(p.id) || p.live ? h("span", {class: "dot d-live", style: "margin-left:8px;vertical-align:1px", title: "Live session", "aria-label": "live session"}) : null, p.local ? null : h("span", {class: "tag", style: "margin-left:8px", title: "Handoff note only — clone with dev clone " + p.id, text: "not cloned"})), h("span", {class: "powner", text: p.owner + ((p.main || {}).branch ? " · " + p.main.branch : "")})),
      h("td", {class: "c-st"}, statusBadge(p.status), prioIcon(p.priority)),
      h("td", {class: "c-prog"}, progressBar(p)), h("td", {class: "c-now"}, nowCell(p)), h("td", {class: "c-status"}, latest(p)), h("td", {class: "c-prs"}, prPills(p, 2)), h("td", {class: "c-bk"}, riskCell(p)),
      h("td", {class: "c-act"}, h("div", {style: "display:flex;gap:8px;align-items:center"}, sparkline(p, 30, 64, 16), h("span", {class: "num t2", style: "font-size:var(--fs-1)", text: String(sum7(p))}))),
      h("td", {class: "c-last num t2", text: rel(p.last_activity)}), h("td", {class: "c-who"}, avatars(p.people, 3)))))));
}
/* a project tile: status dot, name, progress ring, CI strip, 14-day commits */
function projectCard(p) {
  const st = hstate(p);
  return h("article", {class: "ptile", onclick: rowClick(() => openModal("project", p.id))},
    h("div", {class: "pt-h"}, h("span", {class: "dot k-" + st, role: "img", title: (HWHY[st] || HS[st]) + (p.health.reasons.length ? ": " + p.health.reasons.join(", ") : ""), "aria-label": HS[st]}),
      h("div", {class: "pt-n"}, modalLink("project", p.id, p.name || p.id, "strong ell"), h("span", {class: "t2 ell"}, p.local ? p.owner : p.owner + ", not cloned", branchesKnown(p) && branchCounts(p.branches).risk ? [" · ", stateDot("risk"), " " + branchCounts(p.branches).risk + " not backed up"] : null)), progRing(p, 36)),
    h("div", {class: "pt-f"}, ciStrip(p, 6), spark(commitSeries([p], 14), "Commits per day", "commit")));
}
function viewProjects(main) {
  if (S.id) { viewProject(main); return; }
  const ps = sortProjects(filtered(true));
  main.append(h("div", {class: "head"}, h("h1", {text: S.loose ? "Projects outside initiatives" : "Projects"}), h("span", {class: "sub", text: ps.length + " of " + P.length + (S.q ? " matching “" + S.q + "”" : "")})));
  main.append(h("div", {class: "toolbar"}, h("span", {class: "count"}, ["risk", "warn"].map(l => { const n = ps.filter(p => p.health.level === l).length; return n ? h("span", {style: "margin-right:12px"}, h("span", {class: "dot d-" + l, style: "margin-right:6px"}), n + (l === "risk" ? " at risk" : " need a look")) : null; })),
    selectBox("Group by", "group", [["none", "No grouping"], ["initiative", "Group: initiative"], ["owner", "Group: owner"], ["status", "Group: status"], ["priority", "Group: priority"]]),
    selectBox("Sort by", "sort", [["attention", "Sort: needs attention"], ["activity", "Sort: last activity"], ["priority", "Sort: priority"], ["tasks", "Sort: open tasks"], ["name", "Sort: name"], ["size", "Sort: size"]]),
    seg("Layout", "layout", [["cards", "Tiles"], ["table", "Table"]]),
    h("button", {class: "btn sm", type: "button", "aria-pressed": String(S.dense), onclick: () => set({dense: !S.dense}, true)}, "Dense")));
  if (!ps.length) { main.append(h("div", {class: "panel"}, empty("No projects match", facetCount() || S.q ? h("button", {class: "btn", type: "button", onclick: () => set(Object.assign({q: ""}, CLEAR), true)}, "Clear search and filters") : ["Clone one with ", h("code", {text: "dev clone owner/repo"})]))); return; }
  const groups = new Map();
  const keyOf = p => S.group === "owner" ? [p.owner] : S.group === "status" ? [(STATUS[p.status] || [0, p.status])[1]] : S.group === "priority" ? [PRIO[p.priority] ? PRIO[p.priority][1] : p.priority] : S.group === "initiative" ? (MEMBEROF.get(p.id) || ["—"]).map(id => id === "—" ? "Outside initiatives" : (BYINIT.get(id) || {}).title || id) : [""];
  for (const p of ps) for (const g of keyOf(p)) { if (!groups.has(g)) groups.set(g, []); groups.get(g).push(p); }
  for (const [g, list] of groups) {
    const body = S.layout === "table" ? h("div", {class: "panel"}, projectTable(list)) : h("div", {class: "ptiles"}, list.map(projectCard));
    main.append(g ? panel(g, list.length, null, body, {cls: "mb", flush: true}) : body);
  }
}
const PTABS = [["overview", "Overview"], ["tasks", "Tasks"], ["prs", "Pull requests"], ["branches", "Branches"], ["worktrees", "Worktrees"], ["plans", "Plans"], ["sessions", "Sessions"], ["note", "Handoff note"]];
function viewUnknownProject(main) {
  const id = S.id, inits = INITS.filter(x => x.projects.includes(id));
  main.append(crumbs([["Projects", link({view: "projects", id: "", tab: ""})], [id]]));
  main.append(h("header", {class: "phead"}, h("div", {class: "pt1"}, h("div", null, h("h1", {text: id.split("/").pop()})), h("div", {class: "go"}, cmdBtn("dev clone " + id, true, "Copy dev clone"))),
    h("div", {class: "pline"}, "Not cloned on this Mac and no handoff note yet. A member of ", inits.length ? inits.map((x, i) => [i ? ", " : "", h("a", {href: link({view: "initiatives", id: x.id, tab: ""}), text: x.title})]) : "no initiative", ". Clone it to work on it; devkit creates its note on the next sync.")));
}
function prTable(p) {
  if (!p.prsOpen.length) return h("div", {class: "panel"}, empty("No open pull requests", !p.local ? "PRs appear once the project is cloned and synced." : p.github ? ["Open one from your branch: ", h("code", {text: "gh pr create --fill"})] : "PR state is tracked for GitHub repositories."));
  const repo = p.github || p.id;
  return h("div", {class: "panel"}, h("div", {class: "tw"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["CI", "Pull request", "Review", "Merge", "Size", "Updated", ""].map(x => h("th", {text: x})))),
    h("tbody", null, p.prsOpen.map(r => h("tr", null,
      h("td", null, h("span", {class: "badge"}, h("span", {class: "dot d-" + (r.checks || "unknown"), "aria-hidden": "true"}), (r.checks || "unknown"))),
      h("td", null, h("a", {href: r.url || "#", target: "_blank", rel: "noopener noreferrer", text: "#" + r.number}), " ", h("span", {text: r.title || ""}), r.draft ? h("span", {class: "tag", style: "margin-left:6px", text: "draft"}) : null, h("div", {class: "t2 mono", style: "font-size:var(--fs-1)", text: r.branch || ""})),
      h("td", {class: "t2", text: r.review === "CHANGES_REQUESTED" ? "changes requested" : r.review === "APPROVED" ? "approved" : r.review === "REVIEW_REQUIRED" ? "review required" : "—"}),
      h("td", {class: r.mergeable === "CONFLICTING" ? "bad" : "t2", text: r.mergeable === "CONFLICTING" ? "⚠ conflicts" : r.mergeable === "MERGEABLE" ? "clean" : "—"}),
      h("td", {class: "num t2", text: r.size != null ? compact(r.size) : "—"}), h("td", {class: "num t2", text: rel(iso(r.updated))}),
      h("td", null, icopy(r.checks === "failing" ? "gh pr checks " + r.number + " -R " + repo + " --watch" : "gh pr view " + r.number + " -R " + repo, "Copy"))))))));
}
function wtTable(p) {
  if (!p.wts.length) return h("div", {class: "panel"}, empty("No worktrees", p.local ? ["Parallel work on this repository: ", h("code", {text: "dev wt new <type>/<topic>"})] : "Worktrees appear once the project is cloned."));
  return h("div", {class: "panel"}, h("div", {class: "tw"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["Branch", "PR", "State", "Session", "Size", ""].map(x => h("th", {text: x})))),
    h("tbody", null, p.wts.map(w => h("tr", null, h("td", {class: "mono", style: "font-size:var(--fs-1)", text: w.branch || "detached"}), h("td", {text: w.pr ? "#" + w.pr.number + " " + String(w.pr.state).toLowerCase() : "—"}),
      h("td", {text: w.missing ? "missing" : ((w.modified || 0) + (w.untracked || 0)) ? ((w.modified || 0) + (w.untracked || 0)) + " changed" : w.unpushed ? w.unpushed + " unpushed" : "clean"}),
      h("td", {text: w.active ? "live" : rel(w.last_activity)}), h("td", {class: "num", text: kb(w.size_kb)}), h("td", null, w.branch ? icopy("dev wt rm " + w.branch, "Remove") : null)))))));
}
/* ---------------------------------------------------------------- the branch map (spec P-fy3bwa0pshk R8): where each branch lives and whether its work
   is safe off this Mac. The snapshot's rows show at once; on the Branches tab the live page also reads the map from git now (GET /api/v1/branches/{id}) */
const BRISK = ["not_backed_up", "no_remote"];
const BSTATE = {backed_up: ["ok", "Backed up"], on_remote: ["ok", "On the remote"], merged: ["done", "Merged"], waiting: ["hol", "Backup waits"],
  embargoed: ["warn", "Embargoed"], not_backed_up: ["risk", "Not backed up"], no_remote: ["risk", "Not backed up"]};
const BR = new Map();   /* project id -> what the live page read: {state: loading|ok|error|stopped|ended, data, problem, at, last} */
const BR_AGE = 60000;   /* the Branches tab reads the map again when its last read is older than this */
const MERGED_SHOWN = 3;
function branchCounts(rows) {
  if (!rows) return null;
  const c = {total: rows.length, risk: 0, embargoed: 0, waiting: 0, merged: 0};
  for (const r of rows) { const st = (r.backup || {}).state; if (BRISK.includes(st)) c.risk++; if (st === "waiting") c.waiting++; if (st === "merged") c.merged++; if (r.embargo) c.embargoed++; }
  return c;
}
/* unknown is not 0: no map in the snapshot, or a map that came back empty for a checkout that is on a branch (git failed) */
function branchesKnown(p) { return p.local && Array.isArray(p.branches) && !(p.branches.length === 0 && (p.main || {}).branch); }
function embargoWho(s) { const m = member(s); return m ? roleName(m) : String(s || ""); }
function branchLive(p) {
  if (!LIVE.on || !p.local) return null;
  const b = BR.get(p.id);
  if (!b || (b.state !== "loading" && Date.now() - b.at > BR_AGE)) loadBranches(p.id);
  return BR.get(p.id);
}
async function loadBranches(id) {
  const prev = BR.get(id), last = prev ? (prev.state === "ok" ? prev : prev.last) : null;
  BR.set(id, {state: "loading", at: Date.now(), last});
  let next;
  try {
    const r = await fetch("/api/v1/branches/" + id.split("/").map(encodeURIComponent).join("/"), {cache: "no-store", credentials: "same-origin", headers: {Accept: "application/json"}});
    if (r.status === 401) { setLive("ended"); next = {state: "ended"}; }
    else if (!r.ok) { let pb = {}; try { pb = await r.json(); } catch (e) { pb = {}; } next = {state: "error", problem: pb, status: r.status}; }
    else next = {state: "ok", data: await r.json()};
  } catch (e) { next = {state: "stopped"}; }
  next.at = Date.now(); if (next.state !== "ok") next.last = last;
  BR.set(id, next);
  if (next.state === "error") announce("git could not read the branches of " + repoName(id) + ".");
  redrawBranches(id);
}
function retryBranches(id) { const b = BR.get(id); if (b && b.state !== "loading") { b.at = 0; } redrawBranches(id, true); }
/* the panel is replaced in place; focus that was inside it stays inside it */
function redrawBranches(id, refocus) {
  const box = $("#branches"), p = BYID.get(id);
  if (!box || !p || box.getAttribute("data-project") !== id) return;
  const had = refocus || box.contains(document.activeElement), fresh = branchesPanel(p, true);
  box.replaceWith(fresh); fitTables(fresh.parentNode || undefined);
  if (had) { const t = $(".brretry", fresh) || $("h2", fresh); if (t) { if (!t.hasAttribute("tabindex") && t.tagName === "H2") t.setAttribute("tabindex", "-1"); t.focus({preventScroll: true}); } }
}
function aheadCell(r) {
  if (r.upstream && !r.upstream_gone) return h("span", {class: "num", title: r.ahead + " ahead of " + r.upstream + ", " + r.behind + " behind", text: "↑" + r.ahead + " ↓" + r.behind});
  if (r.unpushed && !r.merged_into) return h("span", {class: "num", title: plural(r.unpushed, "commit") + " on no remote", text: "↑" + r.unpushed});
  return h("span", {class: "t2", title: "No upstream branch", text: "—"});
}
function baseText(r, p) {
  if (r.merged_into || r.default) return r.default ? "default branch" : "";
  const d = p.default_branch || "main";
  if (r.base_ahead) return r.base_ahead + " ahead of " + d + (r.base_behind ? ", " + r.base_behind + " behind" : "");
  return r.base_behind ? r.base_behind + " behind " + d : "";
}
function backupCell(r) {
  const b = r.backup || {}, st = BSTATE[b.state] || ["none", "Not known"], emb = r.embargo, lines = [];
  let word = st[1];
  if (b.state === "backed_up") word = "Backed up " + rel(r.last_backup);
  else if (b.state === "merged") word = "Merged into " + r.merged_into;
  else if (b.state === "embargoed") lines.push(emb && emb.until ? "Until “" + emb.until + "”" : "Until it is lifted");
  else if (b.reason) lines.push(sentence(b.reason + (b.state === "not_backed_up" && r.oldest_unpushed && /only on this Mac$/.test(b.reason) ? ", the oldest " + rel(r.oldest_unpushed) : "")));
  if (emb && b.state !== "embargoed") lines.push("Embargoed" + (emb.until ? " until “" + emb.until + "”" : ""));
  if (emb && (emb.why || emb.who)) lines.push((emb.who ? embargoWho(emb.who) + ": " : "") + (emb.why || "no reason given"));
  if (r.embargo_via) lines.push("Held by the embargo on " + r.embargo_via);
  return [stateWord(st[0], word), lines.map(t => h("span", {class: "bsub", text: t}))];
}
function prCell(pr, unknown) {
  if (!pr) return unknown ? h("span", {class: "t2", title: "Pull requests unknown: gh did not answer", text: "not known"}) : null;
  const open = pr.state === "open", word = open ? (pr.draft ? "draft" : "ready") : pr.state || "";
  return [h("a", {href: safeUrl(pr.url), target: "_blank", rel: "noopener noreferrer", title: pr.title || null, text: "#" + pr.number}), " ", open ? h("span", {class: "tag", text: word}) : h("span", {class: "t2", text: word}),
    open && pr.checks && pr.checks !== "unknown" ? h("span", {class: "bsub"}, h("span", {class: "badge"}, h("span", {class: "dot d-" + pr.checks, "aria-hidden": "true"}), "checks " + pr.checks)) : null];
}
function wtCell(r) {
  if (r.worktree) return [h("span", {class: "mono", title: r.worktree, text: r.worktree.split("/").pop()}), r.worktree_missing ? h("span", {class: "bsub", text: "Missing on disk"}) : null,
    r.session ? h("span", {class: "bsub"}, h("span", {class: "stat"}, h("span", {class: "dot d-live", "aria-hidden": "true"}), "live session")) : null];
  return r.current ? h("span", {class: "t2", text: "main checkout"}) : null;
}
function branchRow(r, p, prsUnknown) {
  const b = r.backup || {}, base = baseText(r, p), pr = prCell(r.pr, prsUnknown), wt = wtCell(r);
  const cell = (cls, label, body) => h("td", {class: cls + (body == null ? " nil" : "")}, h("span", {class: "cl", text: label + " "}), body == null ? h("span", {class: "t2", text: "—"}) : body);
  return h("tr", {class: b.state === "merged" ? "quiet" : null, "data-key": "branch~" + r.name, "data-sig": [b.state, r.ahead, r.behind, r.unpushed, r.last_backup, (r.pr || {}).number, (r.pr || {}).checks].join("|")},
    h("td", {class: "c-bk"}, backupCell(r)),
    h("th", {scope: "row"}, h("span", {class: "bn mono", title: r.sha ? r.name + " at " + r.sha : null, text: r.name}), r.current ? h("span", {class: "tag", text: "current"}) : null,
      r.task ? h("span", {class: "bsub"}, h("a", {href: taskHref(r.task.id), "data-nav": "", onclick: e => { e.preventDefault(); openTask(r.task.id); }, text: r.task.id}), " ", r.task.title || "") : null),
    cell("c-where", "Where", h("span", {text: r.where || "—"})),
    h("td", {class: "c-ab"}, h("span", {class: "cl", text: "Ahead / behind "}), aheadCell(r), base ? h("span", {class: "bsub", text: base}) : null),
    cell("c-pr", "Pull request", pr), cell("c-wt", "Worktree", wt));
}
function branchTable(rows, p, prsUnknown) {
  const lead = rows.filter(r => (r.backup || {}).state !== "merged"), merged = rows.filter(r => (r.backup || {}).state === "merged");
  const key = "br:" + p.id, all = !!SHOWN[key] || merged.length <= MERGED_SHOWN + 1, shown = lead.concat(all ? merged : merged.slice(0, MERGED_SHOWN));
  return h("div", {class: "tw"}, h("table", {class: "t bt"}, h("thead", null, h("tr", null, ["Backup", "Branch", "Where", "Ahead / behind", "Pull request", "Worktree"].map(x => h("th", {scope: "col", text: x})))),
    h("tbody", null, shown.map(r => branchRow(r, p, prsUnknown)),
      all ? null : h("tr", null, h("td", {class: "c-more", colspan: "6"}, h("button", {class: "btn sm", type: "button", onclick: () => { SHOWN[key] = 1; redrawBranches(p.id, true); }}, "Show " + plural(merged.length - MERGED_SHOWN, "more merged branch", "more merged branches")))))));
}
/* live: whether to read the map now (the Branches tab); the overview's facts use the snapshot */
function branchesPanel(p, live) {
  const lv = live ? branchLive(p) : null, got = lv && (lv.state === "ok" ? lv : lv.last);
  const fresh = got && got.data && (got.data.mapped || 0) >= (D.generated || 0) ? got.data : null;
  const rows = fresh ? fresh.rows : p.branches, c = branchCounts(rows);
  const prs = fresh ? fresh.prs : p.github && p.prs_known === false ? "error" : "ok", prsUnknown = prs === "error" || (prs === "missing" && !(rows || []).some(r => r.pr));
  const when = fresh ? fresh.mapped : D.generated;
  const status = !LIVE.on ? null : lv && lv.state === "loading" ? "Checking with git…" : "Checked " + rel(when);   /* a static copy: the navbar says when it was made */
  const right = [c && c.risk ? h("span", {class: "badge"}, stateDot("risk"), c.risk + " not backed up") : null, c && c.embargoed ? h("span", {class: "badge"}, stateDot("warn"), c.embargoed + " embargoed") : null,
    p.local && status ? h("span", {class: "brst", role: "status", text: status}) : null, p.local ? icopy("dev branch ls", "Copy the command that lists the branches") : null];
  const body = [];
  if (lv && lv.state === "error") { const pb = lv.problem || {};
    body.push(callout("risk", pb.title || "Could not read the branches", [h("span", {text: pb.detail || "The server answered " + (lv.status || "with an error") + "."}), rows && rows.length ? " The rows below are from " + rel(when) + "." : ""],
      h("button", {class: "btn brretry", type: "button", onclick: () => retryBranches(p.id)}, "Try again"))); }
  else if (lv && lv.state === "stopped") body.push(h("div", {class: "notice", style: "margin:0 0 var(--s3)"}, "The dashboard server is not answering, so these rows are from " + rel(when) + ". Start it again with ", h("code", {text: "dev dashboard"}), "."));
  if (!p.local) return panelFor(p, body.concat(empty("No branches on this Mac", "The project is not cloned here. Clone it to see where its branches live: ", h("code", {text: "dev clone " + p.id}))), c, right);
  const noRemote = (rows || []).some(r => (r.backup || {}).state === "no_remote") || (rows && !rows.length && !p.remote);
  if (noRemote) body.push(callout("warn", "No remote", "Every branch exists only on this Mac. devkit backs up branches to the remote, so nothing here can be backed up until the repository has one.", icopy("git remote add origin <url>", "Copy the command that adds a remote")));
  if (rows && rows.length && prsUnknown) body.push(callout("info", "Pull requests not known", prs === "missing" ? "gh is not installed, so the rows cannot show their pull requests. Install it and sign in with gh auth login." : "gh did not answer, so the rows cannot show their pull requests. gh auth status shows why.", icopy(prs === "missing" ? "gh auth login" : "gh auth status", "Copy")));
  if (rows == null) body.push(lv && lv.state === "loading" ? h("p", {class: "empty", role: "status", text: "Reading the branches with git…"})
    : lv && lv.state === "error" ? null : empty("No branch map in this copy", "The snapshot was made before devkit mapped branches. Run ", h("code", {text: "dev sync"}), ", then open the dashboard again."));
  else if (!rows.length) body.push(lv && lv.state === "error" ? null : (p.main || {}).branch ? empty("git listed no branches", "The checkout is on " + p.main.branch + ", but the map came back empty. ", h("code", {text: "dev branch ls"}), " in the repository shows what git says.")
    : empty("No branches yet", "The repository has no commits. Its first commit makes the first branch."));
  else body.push(branchTable(rows, p, prsUnknown), policyLine(rows[0].backup_policy));
  return panelFor(p, body, c, right);
}
/* the project's backup policy (dev backup policy): every row carries the same record, none for the default */
function policyLine(pol) {
  const words = pol ? pol.policy + (pol.who ? ", set by " + embargoWho(pol.who) : "") + (pol.when ? " on " + String(pol.when).slice(0, 10) : "") + (pol.why ? ": " + pol.why : "")
    : "the default, on for a private repository and ask for a public one";
  return h("p", {class: "t2 brpol", style: "margin:var(--s2) 0 0", text: "Backup policy: " + words + "."});
}
function panelFor(p, body, c, right) { const el = panel("Branches", c ? c.total : null, right, body, {id: "branches", flush: false}); el.setAttribute("data-project", p.id); return el; }
function repoKv(p) {
  const m = p.main || {};
  return h("dl", {class: "kv"},
    h("dt", {text: "Path"}), h("dd", {class: "rpath"}, String(p.path || (p.local ? "—" : "not cloned, " + (p.canonical || ""))).split("/").map((x, i) => [i ? ["/", h("wbr")] : null, x])),
    p.remote ? [h("dt", {text: "Remote"}), h("dd", null, p.github ? h("a", {href: "https://github.com/" + p.github, target: "_blank", rel: "noopener noreferrer", text: p.remote}) : p.remote)] : null,
    p.local ? [h("dt", {text: "Branch"}), h("dd", {text: (m.branch || "—") + (m.modified || m.untracked ? " · " + ((m.modified || 0) + (m.untracked || 0)) + " changed" : " · clean") + (m.ahead ? " · ↑" + m.ahead : "") + (m.behind ? " · ↓" + m.behind : "")})] : null,
    m.last_commit ? [h("dt", {text: "Last commit"}), h("dd", {text: rel(m.last_commit_ts) + " · " + m.last_commit})] : null,
    branchesKnown(p) ? (c => [h("dt", {text: "Branches"}), h("dd", null, h("a", {href: link({tab: "branches"}), text: plural(c.total, "branch", "branches")}),
      c.risk ? [", ", h("span", {class: "stat"}, stateDot("risk"), c.risk + " not backed up")] : c.total ? ", none only on this Mac" : null, c.embargoed ? ", " + c.embargoed + " embargoed" : null)])(branchCounts(p.branches)) : null,
    (p.commands || []).length ? [h("dt", {text: "Commands"}), h("dd", null, p.commands.map(c => h("code", {style: "margin-right:8px", text: c})))] : null,
    p.local && !p.canonical_ok && p.canonical ? [h("dt", {text: "Layout"}), h("dd", null, "Not at its canonical path — ", h("code", {text: "dev adopt " + p.path}))] : null,
    p.stack.length ? [h("dt", {text: "Stack"}), h("dd", null, p.stack.map(s => h("span", {class: "stk", text: s})))] : null,
    h("dt", {text: "Activity"}), h("dd", null, sparkline(p, 30, 120, 22)));
}
function timeStats(t) {
  if (!t) return null;
  const tu = t.turns || {};
  return stats([[tu.median != null ? fmtMin(tu.median) : "—", "median turn", "", tu.n ? "over " + plural(tu.n, "turn") + " in 30 days" : "no turns recorded yet"],
    [tu.p90 != null ? fmtMin(tu.p90) : "—", "p90 turn", "", "9 in 10 turns finish within this"],
    [t.ratio != null ? "×" + t.ratio : "—", "actual against estimate", t.ratio != null && t.ratio > 1.5 ? "warn" : "", t.runs ? "median of " + plural(t.runs, "finished run") + " with an estimate" : "no finished runs with an estimate yet"]], true);
}
function fmtSecs(s) { s = Math.max(0, s || 0); return s < 90 ? Math.round(s) + " s" : dur(s); }
function slowTable(rows, showProject) {
  if (!rows || !rows.length) return null;
  return h("div", {class: "tw"}, h("table", {class: "t"}, h("thead", null, h("tr", null, h("th", {text: "Took"}), h("th", {text: "Command"}), showProject ? h("th", {text: "Project"}) : null, h("th", {text: "When"}))),
    h("tbody", null, rows.map(r => h("tr", null, h("td", {class: "num", style: "white-space:nowrap", text: fmtSecs(r.secs)}), h("td", null, h("code", {class: "ell", style: "display:block;max-width:420px", title: r.what, text: r.what || "—"})),
      showProject ? h("td", {class: "t2 ell", style: "max-width:160px", text: r.project ? scopeName(r.project) : "—"}) : null, h("td", {class: "num t2", style: "white-space:nowrap", text: rel(r.ts)}))))));
}
function timePanel(t, showProject, title) {
  if (!t) return null;
  return panel(title || "Time", null, null, timeBody(t, showProject), {id: "time"});
}
function timeBody(t, showProject) {
  const sub = t.subagents || {}, has = (t.turns || {}).n || t.runs || (t.slow_tools || []).length || sub.n;
  if (!has) return empty("No timing data yet", "devkit records turn, command, subagent and run durations as agents work; estimates get calibrated from them.");
  return [timeStats(t),
    t.ratio != null ? h("p", {class: "tnote", text: t.ratio > 1 ? "Runs here take " + t.ratio + "× their estimate, so new estimates are scaled by this ratio before an ETA is shown." : "Runs here finish within their estimate."}) : null,
    (t.slow_tools || []).length ? [h("h3", {class: "sub3", text: "Slowest commands, last 7 days"}), slowTable(t.slow_tools.slice(0, 6), showProject)] : null,
    sub.n ? [h("h3", {class: "sub3", text: "Subagents, last 7 days"}), h("p", {class: "t2", style: "margin:0 0 6px;font-size:var(--fs-1)", text: plural(sub.n, "subagent") + ", median " + fmtMin(sub.median) + ", p90 " + fmtMin(sub.p90)}),
      slowTable((sub.top || []).slice(0, 4), showProject)] : null,
    (t.recent_runs || []).length ? [h("h3", {class: "sub3", text: "Recent runs: actual against estimate"}), h("ul", {class: "tl-rows"}, t.recent_runs.slice(0, 5).map(r => h("li", {class: "trow"},
      h("span", {class: "tx ell", title: r.what, text: r.what || "run"}), h("span", {class: "num t2" + (r.estimate && r.secs / 60 > r.estimate * 1.5 ? " bad" : ""), text: fmtMin(r.secs / 60) + " of about " + fmtAbout(r.estimate)}))))] : null];
}
function viewProject(main) {
  const p = BYID.get(S.id);
  if (!p) { viewUnknownProject(main); return; }
  const tab = PTABS.some(t => t[0] === S.tab) ? S.tab : "overview", ns = p.ns || {}, m = p.main || {};
  const plansL = plansFor([p.id]), sess = sessionsFor([p.id], p.sessions), here = ONPROJ.get(p.id) || [];
  const lastSeen = Math.max(p.last_activity || 0, ...here.map(s => s.last_seen || s.started || 0)), st = hstate(p);
  main.append(crumbs([["Projects", link({view: "projects", id: "", tab: ""})], [p.owner, link({view: "projects", id: "", tab: "", owner: [p.owner]})], [p.name || p.id]]));
  main.append(h("header", {class: "phead"},
    h("div", {class: "pt1"}, h("div", {class: "mh-row"}, progRing(p, 64, true), h("div", {class: "mh-tt"}, h("h1", {text: p.name || p.id}),
        h("div", {class: "pline"}, meta(h("span", {class: "stat", title: p.health.reasons.join(", ") || null}, h("span", {class: "dot k-" + st, "aria-hidden": "true"}), HS[st]),
          p.inits.length ? h("span", null, "in ", p.inits.map((id, i) => [i ? ", " : "", h("a", {href: link({view: "initiatives", id, tab: ""}), text: (BYINIT.get(id) || {}).title || id})])) : null,
          m.branch ? h("span", {class: "meta", title: "Current branch"}, icon("branch"), m.branch) : null, ns.due ? duePill(ns.due, p.status === "done") : null, lastSeen ? "active " + rel(lastSeen) : null, p.local ? null : "not cloned here")))),
      h("div", {class: "go"}, p.local ? cmdBtn("dev claude " + p.id, true, "Copy dev claude") : cmdBtn("dev clone " + p.id, true, "Copy dev clone"),
        cmdMenu("More commands", p.local ? [["Start Codex", "dev codex " + p.id], ["Go to the checkout", "dcd " + p.id], ["Write the handoff", "dev handoff " + p.id]] : [["Write the handoff", "dev handoff " + p.id]]))),
    p.health.reasons.length ? h("ul", {class: "reasons"}, p.health.reasons.map(r => h("li", null, h("span", {class: "dot k-" + (p.health.level === "risk" ? "risk" : "warn"), "aria-hidden": "true"}), r))) : null,
    HWHY[st] ? h("p", {class: "t2", style: "margin:0;font-size:var(--fs-1)", text: HWHY[st] + "."}) : null, projFlags(p).map(flagLine)));
  const taskParts = [["mark", p.cnt.doing, "Doing"], ["risk", p.cnt.blocked, "Blocked"], ["none", p.cnt.todo, "To do"]];
  main.append(h("div", {class: "g12 tiles"},
    statTile({label: "Open tasks", value: p.cnt.open, href: link({tab: "tasks"}), viz: segBar(taskParts, "Open tasks"), legend: legend(taskParts), cls: "sp-3"}),
    statTile({label: "Open PRs", value: p.prs, href: link({tab: "prs"}), viz: ciStrip(p, 14), cls: "sp-3"}),
    statTile({label: "Commits, 14 days", value: commitSeries([p], 14).reduce((a, d) => a + d[1], 0), viz: spark(commitSeries([p], 14), "Commits per day", "commit", true), cls: "sp-3"}),
    statTile({label: "Live sessions", value: here.length, viz: here.length ? h("div", {class: "sbars"}, here.map(s => { const x = BYWHO.get(s.who), i = x ? x.sessions.indexOf(x.sessions.find(z => z.session === s.session && z.agent === s.agent)) : -1; return sessBar(s, s.who, Math.max(0, i)); })) : h("span", {class: "t2", text: "Nobody here now"}), cls: "sp-3"})));
  main.append(pageTabs([["overview", "Overview"], ["tasks", "Tasks", p.cnt.open], ["prs", "Pull requests", p.prs], ["branches", "Branches", branchesKnown(p) ? p.branches.length : null], ["worktrees", "Worktrees", p.wts.length], ["plans", "Plans", plansL.length || (p.plans || []).length], ["sessions", "Sessions", sess.length], ["note", "Handoff note"]], tab));
  const tp = h("div", {class: "tabpanel", id: "tabpanel", role: "tabpanel", "aria-labelledby": "tab-" + tab}); main.append(tp);
  if (tab === "overview") {
    const doing = sortItems(p.items.filter(i => i.status === "doing" || i.status === "blocked")), next = sortItems(p.items.filter(i => i.status === "todo")).slice(0, 4);
    const tt = ((D.timing || {}).by_project || {})[p.id];
    tp.append(h("div", {class: "g12"},
      h("div", {class: "stack-v sp-8"},
        here.length ? panel("Live now", here.length, null, h("div", {class: "sessl"}, here.map(s => sessionBlock(s, s.who, s.asOf, true)))) : null,
        panel("Doing or blocked", doing.length, h("a", {class: "more", href: link({tab: "tasks"})}, "All tasks"),
          doing.length || next.length ? [doing.length ? h("ul", {class: "irows"}, doing.map(i => itemRow(i))) : null, next.length ? [h("h3", {class: "sub3 pad", text: "Up next"}), h("ul", {class: "irows"}, next.map(i => itemRow(i)))] : null]
            : empty("Nothing in progress", "Plan the work: ", h("code", {text: "dev task -p " + p.id + ' add "…" --now'})), {flush: !!(doing.length || next.length)}),
        ns.purpose || ns.state ? panel("Purpose and current state", null, h("a", {class: "more", href: link({tab: "note"})}, "Full note"), [ns.purpose ? md(ns.purpose) : null, ns.state ? [h("h3", {class: "sub3", text: "Current state"}), md(ns.state)] : null]) : null),
      h("div", {class: "stack-v sp-4"},
        p.status_line ? panel("Latest status", null, null, statusCard(p.status_line.line, p.status_line.parts, [p.status_line.agent || "agent", rel(p.status_line.ts), p.status_line.branch ? "on " + p.status_line.branch : ""].filter(Boolean).join(", "))) : null,
        panel("Repository", null, null, repoKv(p)),
        tt ? h("details", {class: "recent"}, h("summary", null, "How long work takes here"), timeBody(tt, false)) : null)));
  } else if (tab === "tasks") tp.append(itemsBlock(p.items, {scope: p.id}));
  else if (tab === "prs") tp.append(prTable(p));
  else if (tab === "branches") tp.append(branchesPanel(p, true));
  else if (tab === "worktrees") tp.append(wtTable(p));
  else if (tab === "plans") tp.append(plansBlock(plansL, p.id, p.plans));
  else if (tab === "sessions") tp.append(sessionsBlock(sess, false));
  else {
    const f = p.note && FILEMAP.get(p.note);
    const part = (label, text) => text ? [h("h3", {class: "sub3", text: label}), md(text)] : null;
    tp.append(panel("Handoff note", null, h("span", {style: "display:flex;gap:8px"}, f ? h("a", {class: "more", href: link({view: "explorer", f: p.note, id: "", tab: ""})}, "Open in Explorer") : null, icopy("dev handoff " + p.id, "Copy the handoff command")),
      f ? md(f.text) : p.ns ? [part("Purpose", ns.purpose), part("Current state", ns.state), part("Decisions and gotchas", ns.decisions), part("Links", ns.links)] : empty("No handoff note yet", h("code", {text: "dev sync"}), " creates it; agents fill it in with the devkit handoff skill.")));
  }
}
/* ---------------------------------------------------------------- Background work: is anything running, failing or waiting for me, and when will it
   be done? Sections in a fixed order (live updates never move a section): what waits for you, runs, CI, scheduled jobs */
function bgRuns() {
  const rank = {waiting: 0, blocked: 1, lost: 2, running: 3, done: 4}, now = nowS();
  return RUNS.concat(RRUNS).map(r => [runState(r), r]).filter(([st, r]) => st !== "done" || now - (r.finished || r.updated || now) < 3600)
    .sort((a, b) => rank[a[0]] - rank[b[0]] || (b[1].started || 0) - (a[1].started || 0)).map(x => x[1]);
}
function sect(id, title, count, right, body) {
  return h("section", {class: "panel mb", id, "aria-labelledby": id + "-h"},
    h("div", {class: "ph"}, h("h2", {id: id + "-h", tabindex: "-1", text: title}), count != null ? h("span", {class: "count num", text: String(count)}) : null, right ? h("div", {class: "right"}, right) : null),
    h("div", {class: "pb"}, body));
}
function toSection(id) { return e => { e.preventDefault(); const el = document.getElementById(id + "-h"); if (el) { el.scrollIntoView({block: "start"}); el.focus({preventScroll: true}); } }; }
function runCard(r) {
  const st = runState(r), t = runTime(r), plan = r.plan || [], pct = t.total ? 100 * t.done / t.total : t.est ? Math.min(100, 100 * t.act / t.est) : 0;
  const pend = plan.filter(s => s.state === "pending"), shown = plan.filter(s => ["running", "blocked", "unverified"].includes(s.state)).concat(pend.slice(0, 2));
  const nDone = plan.filter(s => s.state === "done").length, nSkip = plan.filter(s => s.state === "skipped").length, more = pend.length - Math.min(2, pend.length);
  const by = new Map(plan.map(s => [s.name, s])), dec = pending(r)[0], qs = r.questions || [], qOld = qs.length ? Math.min(...qs.map(q => q.ts || nowS())) : 0;
  const sess = sessOfRun(r), branch = (sess && sess.branch) || r.branch, eta = etaText(r);
  const stepTxt = s => { const wait = (s.after || []).find(a => by.has(a) && !["done", "skipped"].includes(by.get(a).state));
    return s.state === "running" ? fmtMin(s.min || 0) + " of ~" + fmtAbout(s.est || 0) : s.state === "blocked" ? (s.waiting ? "waiting for you" : "blocked") : s.state === "unverified" ? stepWord(s.state)[1]
      : wait ? "waits for “" + stepTitle(by.get(wait)) + "”" : s.est ? "~" + fmtAbout(s.est) : ""; };
  const blockedStep = plan.find(s => s.state === "blocked");
  return h("article", {class: "rcard " + st, "data-key": "run~" + r.id, "data-sig": [st, t.done, r.step, r.updated].join("|"), "data-open": "run:" + r.id, onclick: rowClick(() => openModal("run", r.id))},
    h("div", {class: "rh"}, modalLink("run", r.id, r.goal || "Agent run"), h("span", {class: "rst", text: runStateText(r, st)})),
    h("div", {class: "rw ell"}, [r.project ? scopeName(r.project) : "", branch || "", agentName(r.agent), r.mode === "auto" ? "Auto" : "Guided", r.remote ? "on " + r.host : ""].filter(Boolean).join(" · ")),
    h("div", {class: "rbar" + (t.over ? " over" : ""), role: "progressbar", "aria-valuenow": Math.round(pct), "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Run progress",
      "aria-valuetext": t.steps + (eta ? ", " + eta.replace("–", " to ").replace(/m\b/g, " minutes") : "")}, h("i", {style: "width:" + pct.toFixed(1) + "%"})),
    h("div", {class: "rmeta num" + (t.over ? " warn" : "")}, meta(t.steps, t.text, t.over ? "over the estimate by " + fmtMin(t.over) : null)),
    shown.length ? h("ol", {class: "rsteps", "aria-label": "Steps in progress and next"}, shown.map(s => { const g = stepWord(s.waiting ? "waiting" : s.state);
      return h("li", {class: s.state}, h("span", {class: "g", "aria-hidden": "true", text: g[0]}), h("span", {class: "ell", title: stepTitle(s) + " · " + stepWho(s)}, h("span", {class: "sr", text: g[1] + ": "}), stepTitle(s), h("span", {class: "who"}, h("span", {class: "sep", text: " · "}), stepWho(s))),
        h("span", {class: "m num", text: stepTxt(s)})); })) : null,
    plan.length && (more || nDone || nSkip) ? h("div", {class: "rmeta", text: [more ? "○ " + more + " more to do" : "", nDone ? "✓ " + nDone + " done" : "", nSkip ? "– " + nSkip + " skipped" : ""].filter(Boolean).join(" · ")}) : null,
    qs.length ? h("div", {class: "rmeta" + (nowS() - qOld > 900 ? " warn" : ""), text: plural(qs.length, "open question") + " to " + roleName(mainMember()) + ", oldest " + rel(qOld)}) : null,
    st === "waiting" && dec ? h("div", {class: "rmeta bad", text: who(dec.from_name || "main") + " waits for your answer to " + dec.id + "."}) : null,
    st === "blocked" && blockedStep ? h("div", {class: "rmeta bad", text: "“" + stepTitle(blockedStep) + "” is blocked" + (r.last ? ": " + r.last : "") + "."}) : null,
    runFlags(r).filter(f => ["C4", "C5", "C6", "C7"].includes(f.code)).map(f => h("div", {class: "warnline"}, stateDot("warn"), h("span", {style: "min-width:0"}, flagNodes(f)))),
    r.last || r.next ? h("dl", {class: "srows"}, r.last ? [h("dt", {text: "Latest"}), h("dd", {text: r.last})] : null, r.next ? [h("dt", {text: "Next"}), h("dd", {text: r.next})] : null) : null,
    r.remote ? h("div", {class: "rmeta", text: "Last presence update " + rel(r.asOf)}) : null);
}
function ciRow(w) {
  const st = ciState(w), key = ciKey(w), fails = w.failures || [], where = repoName(w.repo) + " · " + w.branch + (w.sha ? " @ " + w.sha : "");
  const right = h("span", {class: "cr num", text: [w.ts ? rel(w.ts) : "", w.minutes != null ? (+w.minutes).toFixed(1) + "m" : ""].filter(Boolean).join(" · ")});
  const base = {class: "cirow", "data-key": "ci~" + key, "data-sig": st + "|" + (w.ts || 0), "data-open": "ci:" + key, onclick: rowClick(() => openModal("ci", key))};
  if (st !== "failed") return h("li", base, stateDot(CIST[st][0]), h("div", {class: "ct"}, h("strong", {text: CIST[st][1]}), " · ", modalLink("ci", key, where),
      st === "timeout" && w.minutes != null ? " · after " + (+w.minutes).toFixed(1) + "m" : "", st === "none" ? " · within 5 minutes of the push" + (w.error ? ": " + w.error : "") : "",
      st === "passed" && (w.runs || []).length ? " · " + w.runs.map(rn => (rn.name || "run") + " #" + (rn.number || "")).join(", ") : ""),
    st === "timeout" ? icopy("dev ci status --watch", "Copy the watch command") : right);
  const prov = provName(w.provider);
  return h("li", base, stateDot("risk"), h("div", {class: "ct"}, h("strong", {text: "Failed"}), " · ", modalLink("ci", key, where)), right,
    h("div", {class: "cx"}, fails.map((f, i) => { const jb = f.jobs || [], cut = String(f.excerpt || "").length >= 600;
      return h("div", {style: "display:grid;gap:var(--s2);min-width:0"},
        h("div", {text: (f.run || "run") + " #" + (f.number || "?") + " failed" + (jb[0] ? " in “" + (jb[0].name || "job") + "”" + (jb[0].step ? ", step “" + jb[0].step + "”" : "") : "") + " · " + ciClass(w, f) + (jb.length > 1 ? " · and " + plural(jb.length - 1, "more job") : "")}),
        f.excerpt ? [h("div", {class: "logh", text: "From the CI log"}), untrusted(f.excerpt, "First error from the CI log of " + (f.run || "the run") + " #" + (f.number || ""), "cix~" + key + "~" + i),
          cut ? h("button", {class: "linkbtn", type: "button", style: "justify-self:start", onclick: e => { e.stopPropagation(); openModal("ci", key); }}, "… the rest is in the detail") : null] : null,
        f.action ? h("div", null, h("span", {class: "t2", text: "Next: "}), sentence(f.action)) : null,
        h("div", {class: "ca"}, f.url ? h("a", {class: "btn sm", href: f.url, target: "_blank", rel: "noopener noreferrer", onclick: e => e.stopPropagation()}, "Open the log on " + prov) : null,
          f.id ? cmdBtn("dev run view " + f.id + " --failed", false, "Copy dev run view " + f.id + " --failed") : null)); })));
}
function jobRow(j) {
  const st = jobState(j), last = j.last, wnd = W.jobs_days || 30, every = /^every/.test(j.schedule || "");
  return h("tr", {"data-key": "job~" + j.name, "data-sig": st + "|" + ((last || {}).start || 0), "data-open": "job:" + j.name, onclick: rowClick(() => openModal("job", j.name))},
    h("td", {style: "white-space:nowrap"}, stateWord(JOBST[st][0], jobWord(j, st))),
    h("td", {class: "c-job"}, modalLink("job", j.name, j.name, "pname"), h("span", {class: "jab", title: j.about || "", text: j.about || ""})),
    h("td", null, h("span", {class: "cl", text: "Schedule "}), schedText(j.schedule)),
    h("td", {class: "num"}, h("span", {class: "cl", text: "Last run "}), last ? clock(last.start) + " · " + (last.rc !== 0 && last.rc != null ? (last.rc === 124 ? "timed out" : "exit " + last.rc) : jobDur(last.duration)) : "No runs in " + wnd + " days"),
    h("td", {class: "num", title: !j.enabled ? "Off: it does not run" : every ? "An upper bound: launchd keeps its own clock" : null}, h("span", {class: "cl", text: "Next run "}), !j.enabled || !j.next ? "—" : (every ? "by " : "") + clock(j.next)),
    h("td", {class: "num" + (j.failed ? " bad" : "")}, h("span", {class: "cl", text: "Failures, " + wnd + " days "}), (j.failed || 0) + " of " + (j.runs || 0)));
}
let LOADED_AT = 0;
function viewBackground(main) {
  const now = nowS(), runs = bgRuns(), decs = [], wnd = W.jobs_days || 30;
  for (const r of RUNS) for (const d of pending(r)) decs.push([d, r]);
  decs.sort((a, b) => (a[0].ts || 0) - (b[0].ts || 0));
  if (LIVE.on && now - LOADED_AT > 300) { LOADED_AT = now; liveGet("/api/v1/jobs").then(x => { LOADED = {}; for (const j of x.jobs || []) LOADED[j.name] = j.loaded; CACHE = {}; if (S.view === "background") { LIVE.quiet = true; try { render(); } finally { LIVE.quiet = false; } } }, () => {}); }
  main.append(h("div", {class: "head"}, h("h1", {text: "Background work"}), h("span", {class: "sub", text: "Agent runs, CI watches and scheduled jobs on this Mac" + (LIVE.on ? "" : " · a static copy from " + fmt(D.generated))})));
  const running = runs.filter(r => r.state === "running" && !pending(r).length).length, blocked = runs.filter(r => ["waiting", "blocked"].includes(runState(r))).length, fin = runs.filter(r => r.state === "done").length;
  const latest = ciLatest().filter(w => now - (w.ts || 0) <= CI_DAYS * 86400), cf = latest.filter(w => w.state === "failed").length, cp = latest.filter(w => w.state === "passed").length;
  const jworst = JOBS.map(j => [jobState(j, now), j]).sort((a, b) => JORDER.indexOf(a[0]) - JORDER.indexOf(b[0]))[0];
  const oldest = decs.length ? decs[0][0].ts : 0, rp = [["mark", running, "Running"], ["risk", blocked, "Blocked"], ["ok", fin, "Finished in the last hour"]], cpParts = [["ok", cp, "Passed"], ["risk", cf, "Failed"], ["none", latest.length - cp - cf, "Other"]];
  main.append(h("div", {class: "g12 tiles"},
    statTile({label: "Waiting for you", value: decs.length, href: encode(S), onclick: toSection("waiting"), cls: "sp-3", viz: decs.length ? stateWord("risk", "Oldest waits " + rel(oldest).replace(/ ago$/, "")) : stateWord("ok", "Nothing waiting")}),
    statTile({label: "Runs", value: running, href: encode(S), onclick: toSection("runs"), cls: "sp-3", viz: runs.length ? segBar(rp, "Runs") : h("span", {class: "t2", text: "No runs in the last hour"}),
      legend: runs.length ? h("span", {class: "t2", style: "font-size:var(--fs-1)", text: [running + " running", blocked ? blocked + " blocked" : "", fin ? fin + " finished in the last hour" : ""].filter(Boolean).join(" · ")}) : null}),
    statTile({label: "CI failures, " + CI_DAYS + " days", value: cf, href: encode(S), onclick: toSection("ci"), cls: "sp-3", viz: latest.length ? segBar(cpParts, "Latest CI results") : h("span", {class: "t2", text: "Nothing watched in " + CI_DAYS + " days"}),
      legend: latest.length ? h("span", {class: "t2", style: "font-size:var(--fs-1)", text: cp + " passed · " + cf + " failed" + (latest.length - cp - cf ? " · " + (latest.length - cp - cf) + " other" : "")}) : null}),
    statTile({label: "Scheduled jobs", value: JOBS.length, href: encode(S), onclick: toSection("jobs"), cls: "sp-3", viz: !JOBS.length ? h("span", {class: "t2", text: "No scheduled jobs"})
      : ["failed", "timeout"].includes(jworst[0]) ? stateWord("risk", jworst[1].name + (jworst[0] === "timeout" ? " timed out on its last run" : " failed on its last run"))
      : ["overdue", "unscheduled"].includes(jworst[0]) ? stateWord("warn", jworst[1].name + (jworst[0] === "overdue" ? " is overdue" : " is not scheduled"))
      : stateWord("ok", "All " + JOBS.filter(j => j.enabled).length + " ran OK on their last run")})));
  if (decs.length) main.append(sect("waiting", "Waiting for you", decs.length, null, h("ul", {class: "acts"}, decs.map(([d, r]) => h("li", {class: "act", "data-key": "dec~" + r.id + "~" + d.id, "data-open": "run:" + r.id, onclick: rowClick(() => openRunAt(r.id))},
    stateDot("risk", "Waiting for you"), h("div", {style: "min-width:0"}, h("a", {class: "at", href: modalHref("run", r.id), "data-nav": "", onclick: e => { e.preventDefault(); openRunAt(r.id); }, text: d.question || d.id}),
      h("div", {class: "ad", style: "display:block", text: who(d.from_name || "main") + " asks" + ((d.options || []).length ? " · Options: " + d.options.join(", ") : "") + (d.recommend ? " · Recommended: " + d.recommend : "")}),
      d.irreversible ? h("div", {class: "ad", style: "display:block"}, h("span", {class: "tag", text: "Irreversible: " + d.irreversible})) : null,
      h("div", {class: "ad", style: "display:block", text: "Run “" + (r.goal || r.id) + "” · " + (r.project ? scopeName(r.project) : "no project") + " · " + agentName(r.agent) + " · waiting " + rel(d.ts).replace(/ ago$/, "") + " · answer in that session; " + roleName(mainMember()) + " records it"})),
    h("div", {class: "ar"}, h("a", {class: "btn sm", href: modalHref("run", r.id), onclick: e => { e.preventDefault(); e.stopPropagation(); openRunAt(r.id); }}, "Open the run")))))));
  main.append(sect("runs", "Runs", runs.length ? running + " running" : null, RRUNS.length ? h("span", {class: "t2", style: "font-size:var(--fs-1)", text: "On this Mac, and other machines' last update"}) : null,
    runs.length ? h("div", {class: "rgrid"}, runs.map(runCard)) : empty("No agent runs right now.", "Runs appear when an agent starts one with ", h("code", {text: "dev progress start"}), ". Finished runs stay here for an hour.")));
  const old = ciLatest().filter(w => now - (w.ts || 0) > CI_DAYS * 86400), rank = w => CIORDER.indexOf(ciState(w));
  const sorted = latest.slice().sort((a, b) => rank(a) - rank(b) || (b.ts || 0) - (a.ts || 0)), watching = CIW.watching || [];
  const watchRow = wt => { const at = watchWhere(wt), where = at ? scopeName(at.project.id) + " · " + at.branch : wt.key;
    return h("li", {class: "cirow", "data-key": "watch~" + wt.key + "~" + wt.sha}, h("span", {class: "dot k-mark", "aria-hidden": "true"}), h("div", {class: "ct"}, h("strong", {text: "Watching"}), " · " + where + (wt.sha ? " @ " + wt.sha : "")), h("span", {class: "cr num", text: "for " + rel(wt.since).replace(/ ago$/, "")})); };
  const rows = sorted.filter(w => ciState(w) === "failed").map(ciRow).concat(watching.map(watchRow), sorted.filter(w => ciState(w) !== "failed").map(ciRow), SHOWN.ciold ? old.map(ciRow) : []);
  main.append(sect("ci", "CI", latest.length || watching.length ? plural(new Set(latest.map(ciKey)).size + watching.length, "branch", "branches") : null, h("span", {class: "t2", style: "font-size:var(--fs-1)", text: "Latest result per branch, " + CI_DAYS + " days"}),
    rows.length ? [h("ul", {class: "cirows"}, rows), old.length && !SHOWN.ciold ? h("button", {class: "btn sm", type: "button", onclick: () => { SHOWN.ciold = 1; render(); }}, "Show " + plural(old.length, "older result")) : null,
      CIW.complete === false ? h("p", {class: "foot2", text: "Showing the newest " + (CIW.watches || []).length + " of " + CIW.total + " watched branches."}) : null]
      : empty("Nothing watched in the last " + CI_DAYS + " days.", "devkit watches the pipeline after each push or pull request from this Mac and keeps the latest result per branch.")));
  const jobs = JOBS.slice().sort((a, b) => JORDER.indexOf(jobState(a, now)) - JORDER.indexOf(jobState(b, now)) || a.name.localeCompare(b.name));
  main.append(sect("jobs", "Scheduled jobs", JOBS.length || null, null, jobs.length ? [h("div", {class: "tw"}, h("table", {class: "t jt"},
      h("thead", null, h("tr", null, ["State", "Job", "Schedule", "Last run", "Next run", "Failures, " + wnd + " days"].map(t => h("th", {scope: "col", text: t})))), h("tbody", null, jobs.map(jobRow)))),
      h("p", {class: "foot2"}, "Jobs run through launchd even when the dashboard is closed. Change them with ", h("code", {text: "dev jobs"}), ".")]
    : empty("No scheduled jobs.", "Install devkit's jobs with ", h("code", {text: "dev jobs install"}), ".")));
}
/* ---------------------------------------------------------------- the crew: who is working now, the roster, and who does what (the RACI) */
function crewLanes(recent) {
  const out = [];
  for (const r of RUNS.concat(RRUNS)) {
    const plan = r.plan || [], decs = pending(r);
    for (const a of r.agents || []) {
      const step = plan.find(s => s.owner === a.label && ["running", "blocked", "unverified"].includes(s.state)) || plan.find(s => s.owner === a.label);
      const m = member(step && step.persona) || member(a.label), asks = m && decs.find(d => { const dm = member(d.from_name); return dm && dm.id === m.id; });
      const st = a.state === "lost" ? "lost" : (step && step.waiting) || asks ? "waiting" : a.state === "unverified" || (step && step.state === "unverified") ? "unverified"
        : a.state === "running" ? "running" : "done";
      if (st === "done" && !recent) continue;
      if (st === "done" && r.state !== "done" && !recent) continue;
      out.push({m, label: a.label, run: r, step: step ? stepTitle(step) : laneStep(a.label), st, min: a.min || 0, dec: asks ? asks.id : "",
        word: st === "waiting" ? "Waiting for you" + (asks ? " · " + asks.id : "") : st === "lost" ? "Stopped reporting" : st === "unverified" ? stepWord(st)[1] : st === "running" ? "Working" : "Done"});
    }
  }
  const rank = {waiting: 0, unverified: 1, lost: 2, running: 3, done: 4};
  return out.sort((a, b) => rank[a.st] - rank[b.st] || b.min - a.min);
}
function laneWho(l) { return l.m ? roleName(l.m) : who(l.label); }
function workingNow() {
  const lanes = crewLanes(false);
  return panel("Working now", lanes.length, null, lanes.length ? h("ul", {class: "prows"}, lanes.map(l => {
    const name = laneWho(l), text = name + ", " + l.word.toLowerCase() + (l.step ? " on " + l.step : "") + ", " + fmtMin(l.min).replace(/m$/, " minutes");
    return h("li", {class: "prow", style: "grid-template-columns:16px minmax(0,1fr) auto", "data-key": "lane~" + l.run.id + "~" + l.label, "data-sig": l.st + "|" + (l.step || ""), "data-open": "run:" + l.run.id, onclick: rowClick(() => openModal("run", l.run.id))},
      h("span", {class: "g " + (l.st === "waiting" ? "bad" : l.st === "lost" || l.st === "unverified" ? "warn-t" : ""), "aria-hidden": "true", text: (l.st === "unverified" ? stepWord(l.st) : STEPW[l.st] || STEPW.running)[0]}),
      h("div", {class: "pm"}, h("div", {class: "pw"}, h("a", {class: "rl ell", href: modalHref("run", l.run.id), "data-nav": "", "aria-label": text, onclick: e => { e.preventDefault(); openModal("run", l.run.id); }, text: name})),
        h("div", {class: "ps", text: [l.step, "“" + (l.run.goal || "") + "”", l.run.project ? scopeName(l.run.project) : "", l.run.remote ? "on " + l.run.host : ""].filter(Boolean).join(" · ")})),
      h("span", {class: "t2 num" + (l.st === "waiting" ? " bad" : l.st === "lost" || l.st === "unverified" ? " warn-t" : ""), style: "font-size:var(--fs-1);white-space:nowrap", text: l.word + (l.st === "waiting" ? "" : " · " + fmtMin(l.min))}));
  })) : empty("No crew member is working right now.", "Members work on the steps of a run; when one starts, it shows here."), {flush: !!lanes.length, cls: "mb"});
}
function memberNow(m, lanes) {
  if (m.main) { const r = RUNS.find(x => x.state === "running"); return r ? "Leads " + (r.goal || "a run") : "—"; }
  const l = lanes.find(z => z.m && z.m.id === m.id); return l ? (l.st === "waiting" ? "■ " : l.st === "lost" || l.st === "unverified" ? "! " : "● ") + (l.step || l.word) : "—";
}
function crewTab(box) {
  if (!CREW.length) { box.append(h("div", {class: "panel"}, empty("The crew roster could not be read.", "Run ", h("code", {text: "dev crew"}), " to see what is wrong."))); return; }
  box.append(workingNow());
  const lanes = crewLanes(false), parts = m => ({R: RACI.filter(a => (a.R || []).includes(m.id)).map(a => a.name), A: RACI.filter(a => a.A === m.id).map(a => a.name)});
  const input = h("input", {id: "cq", type: "search", class: "fin", placeholder: "Filter members…", value: S.cq, "aria-label": "Filter members", autocomplete: "off"});
  const count = h("span", {class: "count t2", role: "status", "aria-live": "polite"}), body = h("div");
  const hay = m => [m.title, m.name, m.id].concat(RACI.filter(a => (a.R || []).concat([a.A], a.C || [], a.I || []).includes(m.id)).map(a => a.name)).join(" ").toLowerCase();
  function draw() {
    const tk = tokens(input.value), shown = CREW.filter(m => tk.every(t => hay(m).includes(t)));
    count.textContent = shown.length === CREW.length ? plural(CREW.length, "member") : plural(shown.length, "member") + " of " + CREW.length;
    clear(body);
    if (!shown.length) { body.append(empty("No member matches “" + input.value + "”.", h("button", {class: "btn sm", type: "button", onclick: () => { input.value = ""; S.cq = ""; history.replaceState(null, "", encode(S)); draw(); }}, "Clear the filter"))); return; }
    body.append(h("div", {class: "tw"}, h("table", {class: "t crew"}, h("thead", null, h("tr", null, [["Member", "c-mem"], ["What they do", "c-sum"], ["Does", "c-does"], ["Approves", "c-appr"], ["Access", "c-acc"], ["Now", "c-now"]].map(([t, c]) => h("th", {scope: "col", class: c, text: t})))),
      h("tbody", null, shown.map(m => { const pr = parts(m), now = memberNow(m, lanes);
        return h("tr", {"data-key": "member~" + m.id, "data-sig": now, "data-open": "member:" + m.id, onclick: rowClick(() => openModal("member", m.id))},
          h("th", {scope: "row"}, modalLink("member", m.id, roleName(m))), h("td", {class: "c-sum"}, h("div", {class: "clamp2 t2", title: m.summary || "", text: m.summary || "—"})),
          h("td", null, h("span", {class: "cl", text: "Does: "}), pr.R.join(", ") || "—"), h("td", null, h("span", {class: "cl", text: "Approves: "}), pr.A.join(", ") || "—"),
          h("td", {style: "white-space:nowrap"}, h("span", {class: "cl", text: "Access: "}), ACCESS[m.tools] || m.tools || "—"), h("td", null, h("span", {class: "cl", text: "Now: "}), now)); })))));
  }
  input.addEventListener("input", debounce(() => { S.cq = input.value; history.replaceState(null, "", encode(S)); store.set("devkit.state", S); draw(); }, 120));
  draw();
  box.append(panel("The crew", CREW.length, null, [h("div", {class: "toolbar"}, input, count), body]));
}
function raciTab(box) {
  if (!CREW.length || !RACI.length) { box.append(h("div", {class: "panel"}, empty("The crew roster could not be read.", "Run ", h("code", {text: "dev crew"}), " to see what is wrong."))); return; }
  const lanes = crewLanes(false), busy = new Set(lanes.filter(l => l.m && (l.st === "running" || l.st === "waiting")).map(l => l.m.id)), sel = CREW.find(m => m.id === S.rm) || null, only = !!sel && !S.rall;
  const nameOf = id => { const m = CREW.find(z => z.id === id); return m ? roleName(m) : id; };
  const names = ids => { ids = [].concat(ids || []).filter(Boolean); if (!ids.length) return h("span", {class: "t2", title: "Nobody", text: "—"});
    return ids.map((id, i) => [i ? ", " : null, h("span", {class: sel && sel.id === id ? "me" : null, text: nameOf(id)}), busy.has(id) ? h("span", {class: "wdot", role: "img", "aria-label": "working now", title: "Working now"}) : null]); };
  const input = h("input", {id: "rq", type: "search", class: "fin", placeholder: "Filter kinds of work…", value: S.rq, "aria-label": "Filter kinds of work", autocomplete: "off"});
  const msel = h("select", {"aria-label": "Member"}); msel.append(h("option", {value: ""}, "Everyone"));
  for (const m of CREW) msel.append(h("option", {value: m.id, selected: S.rm === m.id}, roleName(m)));
  msel.addEventListener("change", () => set({rm: msel.value, rall: false}, true));
  const onlyBox = sel ? h("label", {class: "toggle"}, (() => { const cb = h("input", {type: "checkbox", checked: only}); cb.addEventListener("change", () => set({rall: !cb.checked}, true)); return cb; })(), "Only their work") : null;
  const count = h("span", {class: "count t2", role: "status", "aria-live": "polite"}), body = h("div");
  const inRow = (a, id) => (a.R || []).includes(id) || a.A === id || (a.C || []).includes(id) || (a.I || []).includes(id);
  const hay = a => [a.name].concat([].concat(a.R || [], [a.A], a.C || [], a.I || []).filter(Boolean).map(nameOf)).join(" ").toLowerCase();
  function draw() {
    const tk = tokens(input.value), rows = RACI.filter(a => tk.every(t => hay(a).includes(t)) && (!only || inRow(a, sel.id)));
    count.textContent = plural(rows.length, "kind of work", "kinds of work"); clear(body);
    if (!rows.length) { body.append(empty("No kind of work matches “" + input.value + "”.", h("button", {class: "btn sm", type: "button", onclick: () => { input.value = ""; S.rq = ""; history.replaceState(null, "", encode(S)); draw(); }}, "Clear the filter"))); return; }
    const col = (t, l) => h("th", {scope: "col"}, t, h("span", {class: "t2", text: " (" + l + ")"}));
    body.append(h("table", {class: "t raci"}, h("thead", null, h("tr", null, h("th", {scope: "col", style: "width:24%", text: "Kind of work"}), col("Does", "R"), col("Approves", "A"), col("Is consulted", "C"), col("Is informed", "I"))),
      h("tbody", null, rows.map(a => h("tr", {"data-key": "raci~" + a.id}, h("th", {scope: "row", text: a.name}), h("td", null, names(a.R)), h("td", null, names(a.A)), h("td", null, names(a.C)), h("td", null, names(a.I)))))),
      h("div", {class: "rblk"}, rows.map(a => h("div", {class: "pl"}, h("h3", {class: "strong", style: "margin:0;font-size:var(--fs-2)", text: a.name}),
        h("dl", {class: "srows", style: "grid-template-columns:96px minmax(0,1fr)"}, h("dt", {text: "Does"}), h("dd", null, names(a.R)), h("dt", {text: "Approves"}), h("dd", null, names(a.A)),
          h("dt", {text: "Is consulted"}), h("dd", null, names(a.C)), h("dt", {text: "Is informed"}), h("dd", null, names(a.I)))))));
  }
  input.addEventListener("input", debounce(() => { S.rq = input.value; history.replaceState(null, "", encode(S)); store.set("devkit.state", S); draw(); }, 120));
  draw();
  const sum = sel ? (() => { const n = k => RACI.filter(a => k === "A" ? a.A === sel.id : (a[k] || []).includes(sel.id)).length;
    return h("p", {class: "lead", style: "margin:0 0 var(--s3)", text: roleName(sel) + " does " + plural(n("R"), "kind of work", "kinds of work") + ", approves " + n("A") + ", is consulted on " + n("C") + " and is informed about " + n("I") + "."}); })() : null;
  box.append(panel("Who does what", plural(RACI.length, "kind of work", "kinds of work"), null, [h("div", {class: "toolbar"}, input, h("label", {class: "toggle", style: "padding:0"}, "Member", msel), onlyBox, count),
    h("p", {class: "t2", style: "margin:0 0 var(--s3);font-size:var(--fs-1)", text: "Does: does the work. Approves: signs it off; one per kind of work. Is consulted: asked before decisions that affect them. Is informed: told when it is done."}), sum, body]));
}
/* ---------------------------------------------------------------- Team: one card per person (user@host) */
function markerChip() { return h("span", {class: "mk", title: "The status-line marker every agent reply starts with"}, "[Firefly", h("span", {class: "chev", text: ">"}), " DevKit]"); }
/* Team: one column per person; each live session once; locks as rows; history behind a disclosure */
/* a person card: avatar with a live dot, one bar per running session, locks; details in the person modal */
function personCard(x) {
  const claimed = x.claimed.map(id => ITEMBY.get(id)).filter(Boolean), exp = claimed.filter(i => i.lock === "expired").length;
  return h("article", {class: "pcard3", onclick: rowClick(() => openModal("person", x.who))},
    h("div", {class: "pc-h"}, avatar(x.who, true), h("div", {class: "pc-n"}, h("h3", null, modalLink("person", x.who, x.who, "ell"), isMe(x.who) ? h("span", {class: "tag", text: "you"}) : null),
      h("div", {class: "t2", text: (x.sessions.length ? plural(x.sessions.length, "live session") : "No live session") + (x.updated ? ", seen " + rel(x.updated) : "")})),
      claimed.length ? h("span", {class: "lockc" + (exp ? " exp" : ""), role: "img", "aria-label": plural(claimed.length, "claimed task") + (exp ? ", " + exp + " with an expired lock" : ""), "data-tip": plural(claimed.length, "claimed task") + (exp ? ", " + exp + " with an expired lock" : "")}, lockIcon(), h("b", {class: "num", text: String(claimed.length)})) : null),
    x.sessions.length ? h("div", {class: "sbars"}, x.sessions.map((s, i) => sessBar(s, x.who, i))) : null);
}
function viewTeam(main_) {
  const colls = D.collisions || [], live = PEOPLE.reduce((a, x) => a + x.sessions.length, 0), locks = ITEMS.filter(i => i.open && i.claimed_by).length;
  const tab = ["crew", "raci"].includes(S.tab) ? S.tab : "people", working = crewLanes(false).length;
  main_.append(h("div", {class: "head"}, h("h1", {text: "Team"}), h("span", {class: "sub", text: plural(PEOPLE.length, "person", "people") + " · " + plural(working, "crew member") + " working now"})));
  main_.append(pageTabs([["people", "People"], ["crew", "Crew", CREW.length], ["raci", "Who does what", RACI.length]], tab));
  const main = h("div", {class: "tabpanel", id: "tabpanel", role: "tabpanel", "aria-labelledby": "tab-" + tab}); main_.append(main);
  if (tab === "crew") { crewTab(main); return; }
  if (tab === "raci") { raciTab(main); return; }
  main.append(h("p", {class: "t2", style: "margin:0 0 var(--s4);font-size:var(--fs-1)", text: plural(live, "live session") + ", " + plural(locks, "claimed task")}));
  if (colls.length) main.append(panel("Collisions", colls.length, null, h("ul", {class: "acts"}, colls.map(c => {
    const cmd = c.kind === "expired-lock" ? "dev task " + flagOf(c.scope) + " release " + c.id + ' --why "lock expired"' : "dev team";
    const open = () => c.id ? openTask(c.id) : openProject(c.project);
    return h("li", {class: "act", onclick: rowClick(open)}, h("span", {class: "dot k-warn", role: "img", title: "Needs a look", "aria-label": "Needs a look"}),
      h("div", {style: "min-width:0"}, h("a", {class: "at", href: c.id ? taskHref(c.id) : link({view: "projects", id: c.project}), "data-nav": "", onclick: e => { e.preventDefault(); open(); }, text: c.kind === "expired-lock" ? "Expired lock still held" : "Two sessions on the same branch"}),
        h("div", {class: "ad"}, h("span", {class: "ell", text: collText(c, true)}))),
      h("div", {class: "ar"}, icopy(cmd, c.kind === "expired-lock" ? "Copy the release command" : "Copy command"))); })), {flush: true, cls: "mb"}));
  if (!PEOPLE.length) { main.append(h("div", {class: "panel"}, empty("No presence yet", "Each machine writes ", h("code", {text: "presence/<user>@<host>.json"}), " to the handoff repo while sessions run; ", h("code", {text: "dev team"}), " shows the same in the terminal."))); return; }
  main.append(h("div", {class: "people"}, PEOPLE.map(personCard)));
}
/* ---------------------------------------------------------------- Tasks: every work item, across scopes */
function taskCard(it) {
  const first = taskCmds(it)[0];
  return h("div", {class: "tc", "data-key": "task~" + it.id, "data-sig": [it.status, it.bucket, it.claimed_by, it.lock].join("|"), "data-open": "task:" + it.id, onclick: rowClick(() => openTask(it.id))},
    h("div", {class: "tx"}, istatusIcon(it.status), h("a", {class: "itl", href: taskHref(it.id), "data-nav": "", onclick: e => { e.preventDefault(); openTask(it.id); }, text: it.title})),
    first && it.open ? icopy(first[1], first[0]) : h("span"),
    h("div", {class: "meta"}, h("span", {class: "iid", text: it.legacy ? "note" : it.id}), scopeChip(it.scope), claimChip(it, true), it.due ? dueBadge(it.due, !it.open) : null, prioIcon(it.priority)));
}
function viewTasks(main) {
  const all = allItems(), tk = tokens(S.q), who = S.who === "me" ? ME : S.who;
  const base = all.filter(it => (!S.tp || it.scope === S.tp) && (!who || it.holder === who) && tk.every(t => (it.id + " " + it.title + " " + it.scope + " " + (it.claimed_by || "")).toLowerCase().includes(t)));
  const items = base.filter(it => !S.tst || (S.tst === "done" ? !it.open : it.status === S.tst));
  const c = cnt(base);
  const scopeSel = h("select", {"aria-label": "Scope"}); scopeSel.append(h("option", {value: ""}, "All projects and initiatives"));
  for (const x of INITS) scopeSel.append(h("option", {value: x.scope, selected: S.tp === x.scope}, "Initiative: " + x.title));
  for (const p of P.filter(p => p.items.length)) scopeSel.append(h("option", {value: p.id, selected: S.tp === p.id}, p.id));
  scopeSel.addEventListener("change", () => set({tp: scopeSel.value}, true));
  const whos = Array.from(new Set(all.filter(i => i.holder).map(i => i.holder))).sort();
  const whoSel = h("select", {"aria-label": "Claimed by"}); append(whoSel, [h("option", {value: ""}, "Anyone"), ME ? h("option", {value: "me", selected: S.who === "me"}, "Claimed by me") : null]);
  for (const w of whos) whoSel.append(h("option", {value: w, selected: S.who === w}, w));
  whoSel.addEventListener("change", () => set({who: whoSel.value}, true));
  main.append(h("div", {class: "head"}, h("h1", {text: "Tasks"}), h("span", {class: "sub", text: c.doing + " doing · " + c.blocked + " blocked · " + c.open + " open · work items with locks and history"})));
  main.append(h("div", {class: "toolbar"}, seg("Status", "tst", [["", "All"], ["doing", "Doing", c.doing], ["blocked", "Blocked", c.blocked], ["todo", "To do", c.todo], ["done", "Closed", c.done + c.cancelled]]), h("span", {class: "spacer"}), scopeSel, whoSel,
    seg("Layout", "tmode", [["board", "Board"], ["list", "By scope"]])));
  if (!all.length) { main.append(h("div", {class: "panel"}, empty("No tasks yet", "Add one from any terminal: ", h("code", {text: 'dev task -p <project> add "…" --now'}), ". Agents claim, finish and release them as they work."))); return; }
  if (!items.length) { main.append(h("div", {class: "panel"}, empty("No task matches", h("button", {class: "btn", type: "button", onclick: () => set({tst: "", tp: "", who: "", q: ""}, true)}, "Clear the task filters")))); return; }
  if (S.tmode === "list") {
    const by = new Map(); for (const it of sortItems(items)) { if (!by.has(it.scope)) by.set(it.scope, []); by.get(it.scope).push(it); }
    const order = Array.from(by.entries()).sort((a, b) => cnt(b[1]).doing - cnt(a[1]).doing || cnt(b[1]).open - cnt(a[1]).open);
    main.append(h("div", {class: "stack-v"}, order.map(([sc, list]) => panel(scopeName(sc), list.length, h("button", {class: "btn sm", type: "button", onclick: () => openScope(sc, "tasks")}, "Open"), h("ul", {class: "irows"}, list.slice(0, 60).map(i => itemRow(i, {home: sc}))), {flush: true}))));
    return;
  }
  const board = h("div", {class: "board"});
  const closed = items.filter(i => !i.open).sort((a, b) => String(b.updated || "").localeCompare(String(a.updated || "")));
  for (const [b, label] of [["now", "Now"], ["next", "Next"], ["later", "Later"], ["done", "Closed"]]) {
    const list = b === "done" ? closed : sortItems(items.filter(i => i.open && i.bucket === b)), n = SHOWN[b] || 60;
    const col = h("section", {class: "col", "aria-label": label}, h("h3", null, h("span", {class: "dot", style: "background:var(--" + (b === "now" ? "mark" : b === "done" ? "good" : "deemph") + ")"}), label, h("span", {class: "n num", text: String(list.length)})));
    if (!list.length) col.append(h("p", {class: "t2", style: "margin:4px 8px;font-size:var(--fs-1)", text: b === "now" ? "Nothing in progress — start one from Next." : "Empty."}));
    for (const it of list.slice(0, n)) col.append(taskCard(it));
    if (list.length > n) col.append(h("button", {class: "btn sm", type: "button", onclick: () => { SHOWN[b] = n + 200; render(); }, text: "Show " + Math.min(200, list.length - n) + " more"}));
    board.append(col);
  }
  main.append(board);
}

/* ---------------------------------------------------------------- Activity: timeline first */
function partsText(parts) { return parts ? [parts.task, parts.done].filter(Boolean).join(": ") + (parts.check ? " (" + parts.check + ")" : "") + (parts.next ? ". Next: " + parts.next : "") : ""; }
function timelineItems(ps, nDays) {
  const cutoff = nowS() - nDays * 86400, ids = new Set(ps.map(p => p.id)), items = [], logged = new Set((D.session_log || []).map(s => s.scope));
  for (const x of PEOPLE) for (const s of x.sessions) { const ts = s.last_seen || s.started || 0; if (ts >= cutoff && (!s.project || ids.has(s.project))) items.push({ts, k: "◆", p: BYID.get(s.project), text: [x.who, s.agent || "agent", s.branch ? "`" + s.branch + "`" : "", s.status_parts ? partsText(s.status_parts) : s.progress && s.progress.goal ? s.progress.goal : "", !s.status_parts && s.status ? stripMarker(s.status) : "", "live"].filter(Boolean).join(" · ")}); }
  for (const s of D.session_log || []) if (s.ts >= cutoff && (isInit(s.scope) || ids.has(s.scope))) items.push({ts: s.ts, k: "◇", p: BYID.get(s.scope), scope: isInit(s.scope) ? s.scope : null, text: [s.who, s.agent, s.branch ? "`" + s.branch + "`" : "", s.parts ? [s.pre ? stripMarker(s.pre) : "", partsText(s.parts)].filter(Boolean).join(". ") : stripMarker(s.text)].filter(Boolean).join(" · ")});
  for (const p of ps) if (!logged.has(p.id)) for (const s of p.sessions) { const ts = sessTs(s); if (ts >= cutoff) items.push({ts, k: "◇", p, text: sessText(s)}); }
  for (const m of D.merged || []) if (m.ts >= cutoff) items.push({ts: m.ts, k: "⑃", text: "Merged [#" + m.number + "](" + m.url + ") " + m.title + " · " + m.repo});
  for (const it of ITEMS) { const ts = iso(it.updated); if (ts >= cutoff && (it.status === "done" || it.status === "blocked") && (isInit(it.scope) || ids.has(it.scope))) items.push({ts, k: it.status === "done" ? "✓" : "⊘", p: BYID.get(it.scope), scope: isInit(it.scope) ? it.scope : null, text: (it.status === "done" ? "Done " : "Blocked ") + it.id + " " + it.title + (it.updated_by ? " · " + it.updated_by : "")}); }
  for (const ln of D.ops || []) { const ts = sessTs(ln); if (ts >= cutoff) items.push({ts, k: "⚙", text: ln.slice(19)}); }
  for (const r of (D.canary || {}).records || []) if (!r.ok && r.ts >= cutoff) items.push({ts: r.ts, k: "✕", p: BYID.get(r.project), text: "Canary miss (" + r.agent + "): reply began “" + (r.excerpt || "") + "”"});
  return items.sort((a, b) => b.ts - a.ts);
}
const KI = {"◆": "session", "◇": "session", "⑃": "merge", "✓": "done", "⊘": "blocked", "⚙": "gear", "✕": "miss"};
function timeline(ps, nDays, limit) {
  const items = timelineItems(ps, nDays).slice(0, limit || 200);
  if (!items.length) return empty("No activity in the last " + nDays + " days", "Agent sessions, merged PRs and operations appear here as they happen.");
  const ul = h("ul", {class: "tl"}); let day = "";
  for (const it of items) {
    const k = lkey(new Date(it.ts * 1000));
    if (k !== day) { day = k; ul.append(h("li", {class: "day", text: fmtDay(it.ts)})); }
    ul.append(h("li", {class: "it"}, h("span", {class: "tm", text: tm(it.ts)}), h("span", {class: "ki", "aria-hidden": "true"}, icon(KI[it.k] || "session")),
      h("div", {style: "min-width:0"}, it.p ? h("a", {href: link({view: "projects", id: it.p.id}), class: "who", onclick: e => { e.preventDefault(); openProject(it.p.id); }, text: it.p.name || it.p.id}) : null, inline(it.text))));
  }
  return ul;
}
/* a calendar heatmap: one square per day (13 weeks), a one-hue ramp, the day's commits, sessions and finished tasks on hover */
function heatmap(nWeeks) {
  const end = new Date(D.generated * 1000); end.setHours(12, 0, 0, 0);
  const start = new Date(end); start.setDate(end.getDate() - ((end.getDay() + 6) % 7) - (nWeeks - 1) * 7);
  const per = {}, add = (k, f) => { const d = per[k] || (per[k] = {c: 0, s: 0, t: 0}); d[f]++; };
  for (const p of P) for (const [k, v] of Object.entries(p.act)) { const d = per[k] || (per[k] = {c: 0, s: 0, t: 0}); d.c += v; }
  for (const s of D.session_log || []) add(lkey(new Date(s.ts * 1000)), "s");
  for (const it of ITEMS) if (it.status === "done" && it.updated) add(lkey(new Date(iso(it.updated) * 1000)), "t");
  const cells = [], d = new Date(start);
  while (d <= end) { const k = lkey(d), v = per[k] || {c: 0, s: 0, t: 0}; cells.push({k, d: new Date(d), v, n: v.c + v.s + v.t}); d.setDate(d.getDate() + 1); }
  const nz = cells.map(c => c.n).filter(Boolean).sort((a, b) => a - b), q = f => nz.length ? nz[Math.min(nz.length - 1, Math.floor(f * nz.length))] : 0;
  const cut = [q(0.25), q(0.5), q(0.75)], lvl = n => !n ? 0 : n <= cut[0] ? 1 : n <= cut[1] ? 2 : n <= cut[2] ? 3 : 4;
  const C = 18, G = 4, L = 32, T = 20, w = L + nWeeks * (C + G), hh = T + 7 * (C + G);
  const tot = cells.reduce((a, c) => ({c: a.c + c.v.c, s: a.s + c.v.s, t: a.t + c.v.t}), {c: 0, s: 0, t: 0});
  const best = cells.reduce((a, c) => c.n > a.n ? c : a, cells[0]);
  const svg = sv("svg", {class: "heat", width: w, height: hh, viewBox: "0 0 " + w + " " + hh, role: "img", tabindex: "0",
    "aria-label": "Activity per day over " + nWeeks + " weeks: " + plural(tot.c, "commit") + ", " + plural(tot.s, "session") + ", " + plural(tot.t, "task") + " done" + (best && best.n ? "; busiest " + fmtDay(best.d.getTime() / 1000) : ""), "data-tip": "Use the table view to read every day"});
  ["Mon", "Wed", "Fri"].forEach((l, i) => svg.append(sv("text", {x: 0, y: T + (i * 2) * (C + G) + C - 3, class: "ax"}, l)));
  let lastM = -1;
  cells.forEach((c, i) => { const col = Math.floor(i / 7), row = i % 7;
    if (row === 0 && c.d.getMonth() !== lastM) { lastM = c.d.getMonth(); svg.append(sv("text", {x: L + col * (C + G), y: 12, class: "ax"}, c.d.toLocaleDateString(undefined, {month: "short"}))); }
    svg.append(sv("rect", {x: L + col * (C + G), y: T + row * (C + G), width: C, height: C, rx: 3, class: "h" + lvl(c.n), "data-tip": fmtDay(c.d.getTime() / 1000) + ": " + [plural(c.v.c, "commit"), plural(c.v.s, "session"), plural(c.v.t, "task") + " done"].join(", ")})); });
  const lg = h("div", {class: "heat-l", "aria-hidden": "true"}, "Less", [0, 1, 2, 3, 4].map(l => h("i", {class: "h" + l})), "More");
  return {svg, lg, cells, tot};
}
function viewActivity(main) {
  const ps = filtered(true), n = S.range === 30 ? 30 : 7, hm = heatmap(26);
  const rows = ps.map(p => [p.name || p.id, days(n).reduce((a, k) => a + (p.act[k] || 0), 0), p.id]).filter(r => r[1] > 0).sort((a, b) => b[1] - a[1]).slice(0, 10);
  let tableOn = false;
  const box = h("div", {class: "heat-box"});
  const tbtn = h("button", {class: "btn sm", type: "button", "aria-pressed": "false", onclick: () => { tableOn = !tableOn; tbtn.setAttribute("aria-pressed", String(tableOn)); paint(); }}, "Table");
  function paint() { append(clear(box), [tableOn ? h("div", {class: "tw"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["Day", "Commits", "Sessions", "Tasks done"].map(x => h("th", {text: x})))),
      h("tbody", null, hm.cells.slice().reverse().filter(c => c.n).map(c => h("tr", null, h("td", {text: fmtDay(c.d.getTime() / 1000)}), h("td", {class: "num", text: String(c.v.c)}), h("td", {class: "num", text: String(c.v.s)}), h("td", {class: "num", text: String(c.v.t)})))))) : [hm.svg, hm.lg]]); }
  paint();
  main.append(h("div", {class: "head"}, h("h1", {text: "Activity"}), h("span", {class: "sub", text: ps.length + " projects"}),
    h("div", {class: "right"}, h("div", {class: "seg", role: "group", "aria-label": "Range"}, [7, 30].map(r => h("button", {type: "button", "aria-pressed": String(n === r), onclick: () => set({range: r}, true)}, "Last " + r + " days"))))));
  main.append(h("div", {class: "g12"},
    panel("Every day, 26 weeks", null, tbtn, [box, h("p", {class: "tnote", text: "Commits are kept for 30 days; sessions and finished tasks come from the handoff repository."})], {cls: "sp-8"}),
    h("div", {class: "sp-4 ctrs col1"},
      statTile({label: "Commits, " + (W.commits_days || 30) + " days", value: hm.tot.c, viz: spark(commitSeries(ps, 14), "Commits per day", "commit", true), cls: "row"}),
      statTile({label: W.sessions_since ? "Sessions since " + shortDay(lkey(new Date(W.sessions_since * 1000))) : "Sessions", value: hm.tot.s, viz: spark(sessionSeries(null, 14), "Sessions per day", "session", true), cls: "row"}),
      statTile({label: "Tasks done, 26 weeks", value: hm.tot.t, viz: spark(doneSeries(ITEMS, 14), "Tasks done per day", "task", true), cls: "row"})),
    panel("Timeline", null, null, timeline(ps, n), {flush: true, cls: "sp-8"}),
    panel("Most active, last " + n + " days", rows.length, null, rows.length ? rankedBars(rows, v => String(v), id => openModal("project", id)) : empty("No commits in this range"), {cls: "sp-4"})));
}
/* ---------------------------------------------------------------- Explorer */
function fileTree() {
  const tree = {workspace: [], log: [], owners: new Map(), inits: new Map()};
  for (const f of FILES) {
    if (!f.path.includes("/")) { tree.workspace.push(f); continue; }
    if (f.path.startsWith("log/")) { tree.log.push(f); continue; }
    if (f.path.startsWith("initiatives/")) { const slug = f.path.split("/")[1]; if (!tree.inits.has(slug)) tree.inits.set(slug, []); tree.inits.get(slug).push(f); continue; }
    const p = projectFor(f.path);
    const pid = p ? p.id : f.path.replace(/^(projects|plans)\//, "").replace(/\.md$/, "").split("/").slice(0, 2).join("/");
    const owner = pid.includes("/") ? pid.slice(0, pid.lastIndexOf("/")) : "(other)";
    if (!tree.owners.has(owner)) tree.owners.set(owner, new Map());
    const pm = tree.owners.get(owner); if (!pm.has(pid)) pm.set(pid, []); pm.get(pid).push(f);
  }
  return tree;
}
function viewExplorer(main) {
  main.append(h("div", {class: "head"}, h("h1", {text: "Explorer"}), h("span", {class: "sub", text: "The handoff repository · " + D.handoff})));
  if (!FILES.length && D.live && !D.files) { main.append(h("div", {class: "panel"}, empty("Loading the files…", "The live page loads the handoff repository's Markdown after the first screen."))); return; }
  if (!FILES.length) { main.append(h("div", {class: "panel"}, empty("The handoff repository has no Markdown files yet", "Run ", h("code", {text: "dev sync"}), " to generate the map and the notes."))); return; }
  const tree = fileTree();
  const pick = S.f && FILEMAP.has(S.f) ? FILEMAP.get(S.f) : (FILEMAP.get("PROJECTS.md") || FILES[0]);
  const nav = h("nav", {class: "tree panel", "aria-label": "Handoff files"});
  const filt = h("input", {type: "search", placeholder: "Filter files…", "aria-label": "Filter files"}); nav.append(filt);
  const listBox = h("div"); nav.append(listBox);
  const fileBtn = f => h("button", {class: "file", type: "button", "data-nav": "", "aria-current": String(f === pick), title: f.path, "data-path": f.path.toLowerCase(), onclick: () => set({f: f.path}, true)}, f.path.split("/").pop());
  const openState = store.get("devkit.tree", {});
  const group = (key, label, kids) => { const d = h("details", {open: openState[key] !== false || kids.some(k => k.querySelector && k.querySelector('[aria-current="true"]'))}, h("summary", null, label), kids);
    d.addEventListener("toggle", () => { openState[key] = d.open; store.set("devkit.tree", openState); }); return d; };
  if (tree.workspace.length) listBox.append(group("ws", "Workspace", tree.workspace.map(fileBtn)));
  if (tree.inits.size) listBox.append(group("inits", "Initiatives", Array.from(tree.inits.entries()).sort().map(([slug, files]) => group("i:" + slug, (BYINIT.get(slug) || {}).title || slug,
    files.sort((a, b) => (a.path.endsWith("README.md") ? 0 : 1) - (b.path.endsWith("README.md") ? 0 : 1) || a.path.localeCompare(b.path)).map(f => { const b = fileBtn(f); b.textContent = f.path.split("/").slice(2).join("/"); return b; })))));
  for (const [owner, projects] of Array.from(tree.owners.entries()).sort()) {
    const kids = []; for (const [pid, files] of Array.from(projects.entries()).sort()) kids.push(group("p:" + pid, pid.slice(pid.lastIndexOf("/") + 1) || pid, files.sort((a, b) => a.path.localeCompare(b.path)).map(fileBtn)));
    listBox.append(group("o:" + owner, owner, kids));
  }
  if (tree.log.length) listBox.append(group("log", "Activity log", tree.log.sort((a, b) => b.path.localeCompare(a.path)).map(fileBtn)));
  filt.addEventListener("input", debounce(() => { const v = filt.value.toLowerCase().trim();
    $$(".file", nav).forEach(b => { const f = FILEMAP.get(b.title); b.hidden = !(!v || b.dataset.path.includes(v) || (f && f.text.toLowerCase().includes(v))); });
    $$("details", nav).forEach(d => { if (v) d.open = !!d.querySelector(".file:not([hidden])"); }); }, 120));
  const viewer = h("article", {class: "viewer panel", "aria-label": "File viewer"});
  const fq = h("input", {type: "search", placeholder: "Find in file…", value: S.fq, "aria-label": "Find in file"});
  const cnt = h("span", {class: "t2 num", style: "font-size:var(--fs-1)"});
  const proj = projectFor(pick.path);
  viewer.append(h("div", {class: "vh"}, h("span", {class: "path", text: pick.path}), h("span", {class: "t2", style: "font-size:var(--fs-1)", text: rel(pick.mtime) + " · " + (pick.size / 1024).toFixed(1) + " KB"}),
    h("span", {class: "spacer"}), fq, cnt, h("button", {class: "btn sm", type: "button", onclick: () => copy(D.handoff + "/" + pick.path)}, "Copy path"),
    proj ? h("button", {class: "btn sm", type: "button", onclick: () => openProject(proj.id)}, "Open project") : null,
    initFor(pick.path) ? h("button", {class: "btn sm", type: "button", onclick: () => openInitiative(initFor(pick.path).id)}, "Open initiative") : null));
  const meta = D.files_meta || {};
  const body = h("div"); viewer.append(body);
  function paint() {
    clear(body);
    if (meta.capped) body.append(h("div", {class: "notice", text: meta.omitted + " older file(s) are not embedded (cap " + Math.round((meta.cap || 0) / 1e6) + " MB, newest first). Open them in " + D.handoff + "."}));
    const doc = md(pick.text); body.append(doc);
    const n = highlight(doc, fq.value); cnt.textContent = fq.value ? plural(n, "match", "matches") : "";
    const first = doc.querySelector("mark"); if (first) { first.classList.add("cur"); first.scrollIntoView({block: "center"}); }
  }
  fq.addEventListener("input", debounce(() => { S.fq = fq.value; history.replaceState(null, "", encode(S)); store.set("devkit.state", S); paint(); }, 150));
  fq.addEventListener("keydown", e => { if (e.key === "Enter") { const ms = $$("mark", body); if (!ms.length) return; const i = ms.findIndex(m => m.classList.contains("cur")); ms.forEach(m => m.classList.remove("cur")); const nx = ms[(i + (e.shiftKey ? -1 : 1) + ms.length) % ms.length]; nx.classList.add("cur"); nx.scrollIntoView({block: "center"}); } });
  paint();
  main.append(h("div", {class: "explorer"}, nav, viewer));
  const cur = $('.file[aria-current="true"]', nav); if (cur) cur.scrollIntoView({block: "nearest"});
}

/* ---------------------------------------------------------------- Health */
function providerChips() {
  const out = []; const pv = D.providers || {};
  for (const key of ["claude", "codex"]) {
    const p = pv[key]; if (!p || !p.installed) continue;
    const name = p.name || key, q = p.quota || {};
    let tone = "good", text = name;
    if (p.logged_in === false) { tone = "warning"; text += " · not signed in"; }
    else if (q.available === false) { tone = "critical"; text += " · quota exhausted" + (q.resets_at ? " until " + fmtDay(q.resets_at) : ""); }
    else { text += " · signed in" + (p.plan ? " · " + p.plan.charAt(0).toUpperCase() + p.plan.slice(1) : ""); if (q.used_percent != null) text += " · " + Math.round(q.used_percent) + "% used"; }
    out.push(h("span", {class: "prov", title: [p.version ? "v" + p.version : "", p.auth_method || "", q.note || ""].filter(Boolean).join(" · ")}, h("span", {class: "dot d-" + tone, "aria-hidden": "true"}), text));
  }
  return out;
}
function providerCard(key, p) {
  if (!p || !p.installed) return h("div", {class: "panel"}, h("div", {class: "ph"}, h("h2", {text: key === "claude" ? "Claude Code" : "Codex"})), empty("Not installed", "Run ", h("code", {text: "dev install"}), "."));
  const q = p.quota || {}, exhausted = q.available === false, used = q.used_percent;
  const tone = p.logged_in === false ? "warning" : exhausted ? "critical" : "good";
  const state = p.logged_in === false ? "Not signed in" : exhausted ? "Quota exhausted" : "Signed in";
  const dl = h("dl", {class: "kv"}, h("dt", {text: "Account"}), h("dd", {text: [p.auth_method, p.plan ? p.plan.charAt(0).toUpperCase() + p.plan.slice(1) + " plan" : ""].filter(Boolean).join(" · ") || "—"}),
    h("dt", {text: "Version"}), h("dd", {class: "num", text: p.version || "—"}), p.desktop != null ? [h("dt", {text: "Desktop app"}), h("dd", {text: p.desktop ? "installed" : "not installed"})] : null,
    p.login_expires ? [h("dt", {text: "Login expires"}), h("dd", {text: fmtDay(iso(p.login_expires))})] : null);
  const quota = q.available === null || q.available === undefined && used == null ? h("p", {class: "t2", style: "font-size:var(--fs-1);margin:8px 0 0", text: q.note || "Quota is not reported for this provider."})
    : [h("div", {class: "qbar", role: "progressbar", "aria-valuenow": Math.round(used || 0), "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Quota used"}, h("i", {style: "width:" + Math.min(100, used || 0) + "%;background:var(--" + (exhausted || used >= 90 ? "critical" : used >= 70 ? "warning" : "good") + ")"})),
       h("div", {class: "t2", style: "font-size:var(--fs-1)"}, (used != null ? Math.round(used) + "% of the " + (q.window_minutes ? (q.window_minutes >= 1440 ? Math.round(q.window_minutes / 1440) + "-day" : Math.round(q.window_minutes / 60) + "-hour") : "") + " window used" : ""), q.resets_at ? " · resets " + fmtDay(q.resets_at) + " " + tm(q.resets_at) : "")];
  return panel(p.name || key, null, h("span", {class: "prov"}, h("span", {class: "dot d-" + tone, "aria-hidden": "true"}), state), [dl, quota]);
}
function viewHealth(main, tiles) {
  if (!tiles) main.append(h("div", {class: "head"}, h("h1", {text: "Health"}), h("span", {class: "sub", text: "Agents, guard, configuration and disk"})));
  const pv = D.providers || {};
  const c = D.canary || {}, sum = c.summary || {}, recs = c.records || [], misses = recs.filter(r => !r.ok).slice(-40).reverse();
  const canaryBody = Object.keys(sum).length ? [h("div", {style: "display:grid;gap:12px"}, Object.keys(sum).map(a => { const [ok, tot] = sum[a], pct = Math.round(100 * ok / tot), tone = pct >= 95 ? "good" : pct >= 80 ? "warning" : "critical";
      return h("div", null, h("div", {style: "display:flex;justify-content:space-between;font-size:var(--fs-1)"}, h("strong", {text: a.charAt(0).toUpperCase() + a.slice(1)}), h("span", {class: "num t2", text: pct + "% · " + ok + "/" + tot + " replies"})),
        h("div", {class: "qbar"}, h("i", {style: "width:" + pct + "%;background:var(--" + tone + ")"}))); })),
      misses.length ? h("div", {class: "tw", style: "margin-top:12px"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["When", "Agent", "Project", "Reply began with"].map(x => h("th", {text: x})))),
        h("tbody", null, misses.map(r => h("tr", null, h("td", {class: "num t2", style: "white-space:nowrap", text: fmt(r.ts)}), h("td", {style: "white-space:nowrap", text: r.agent + (r.surface && r.surface !== "unknown" ? " · " + r.surface : "")}), h("td", {style: "white-space:nowrap", title: r.project || "", text: (r.project || "—").split("/").pop()}), h("td", {class: "t2", style: "max-width:0;width:100%"}, h("div", {class: "ell", title: r.excerpt || ""}, r.excerpt || ""))))))) : h("p", {class: "t2", style: "font-size:var(--fs-1)"}, "No deviations: every tracked reply started with the ", markerChip(), " status block.")]
    : empty("No tracked replies yet", "Replies in sessions under ", h("code", {text: D.dev_root}), " are checked after every development turn for the ", markerChip(), " status block.");
  main.append(h("div", {class: "cols even", style: "margin-bottom:16px"}, providerCard("claude", pv.claude), providerCard("codex", pv.codex)));
  const rules = D.guard_rules || [];
  const maxc = Math.max(1, ...rules.map(r => r.count));
  const guardBody = rules.length ? h("div", {class: "tw"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["Rule", "Count", "", "Agents", "Last", "Latest reason"].map(x => h("th", {text: x})))),
      h("tbody", null, rules.map(r => h("tr", null, h("td", {style: "white-space:nowrap"}, h("strong", {text: r.label})), h("td", {class: "num", text: String(r.count)}),
        h("td", {style: "width:90px"}, h("div", {class: "qbar", style: "margin:0"}, h("i", {style: "width:" + (100 * r.count / maxc).toFixed(0) + "%;background:var(--" + (r.count >= 3 ? "warning" : "deemph") + ")"}))),
        h("td", {class: "t2", style: "white-space:nowrap", text: Object.entries(r.agents).map(([a, n]) => a + " " + n).join(" · ")}), h("td", {class: "num t2", style: "white-space:nowrap", text: r.last.slice(5)}),
        h("td", {class: "t2", style: "max-width:0;width:100%"}, h("div", {class: "ell", title: r.example}, r.example)))))))
    : empty("The guard has not blocked anything", "Blocked actions are grouped by rule here; a rule that keeps firing means an agent is fighting the workspace rules.");
  main.append(h("div", {class: "cols", style: "margin-bottom:16px"}, panel("Guard decisions by rule" + guardWindow(), rules.reduce((a, r) => a + r.count, 0), null, guardBody, {flush: !!rules.length}), panel("Instruction canary · 7 days", null, markerChip(), canaryBody)));
  const drift = D.drift;
  const driftBody = drift == null ? h("p", {class: "t2", style: "font-size:var(--fs-1);margin:0"}, "No drift report in this registry. ", h("code", {text: "dev config diff"}), " compares the devkit configuration with Claude Code and Codex.")
    : !drift.length ? h("p", {class: "t2", style: "margin:0", text: "Claude Code and Codex match the devkit configuration."})
    : h("ul", {class: "tl-rows"}, drift.map(d => h("li", {class: "trow"}, h("span", {class: "dot d-warn", style: "margin-top:7px"}), h("span", {class: "tx mono", style: "font-size:var(--fs-1)", text: typeof d === "string" ? d : [d.tool || d.target, d.key, d.current !== undefined ? JSON.stringify(d.current) : "", "→", d.desired !== undefined ? JSON.stringify(d.desired) : ""].filter(Boolean).join(" ")}))));
  const wts = []; for (const p of P) for (const w of p.wts) wts.push([p, w]);
  const recl = wts.filter(([, w]) => w.pr && /MERGED|CLOSED/.test(w.pr.state) && !w.active);
  const wtBody = wts.length ? h("div", {class: "tw"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["Project", "Branch", "PR", "State", "Session", "Size"].map(x => h("th", {text: x})))),
      h("tbody", null, wts.sort((a, b) => (b[1].size_kb || 0) - (a[1].size_kb || 0)).map(([p, w]) => h("tr", {onclick: rowClick(() => openProject(p.id))},
        h("td", null, linkTo(link({view: "projects", id: p.id}), p.name || p.id)), h("td", {class: "mono", style: "font-size:var(--fs-1)", text: w.branch || "detached"}), h("td", {text: w.pr ? "#" + w.pr.number + " " + String(w.pr.state).toLowerCase() : "—"}),
        h("td", {text: w.missing ? "missing" : [w.modified ? w.modified + " modified" : "", w.untracked ? w.untracked + " untracked" : "", w.unpushed ? w.unpushed + " unpushed" : ""].filter(Boolean).join(" · ") || "clean"}),
        h("td", null, w.active ? h("span", {class: "badge"}, h("span", {class: "dot d-live"}), "Live") : rel(w.last_activity)), h("td", {class: "num", text: kb(w.size_kb)})))))) : empty("No worktrees", "Create one with ", h("code", {text: "dev wt new <type>/<topic>"}), " only when work must run in parallel.");
  const sizes = P.map(p => [p.name || p.id, (p.size_kb || 0) + p.wts.reduce((a, w) => a + (w.size_kb || 0), 0), p.id]).filter(r => r[1] > 0).sort((a, b) => b[1] - a[1]).slice(0, 12);
  main.append(h("div", {class: "cols", style: "margin-bottom:16px"}, panel("Worktrees", wts.length, recl.length ? cmdBtn("dev gc --apply") : null, wtBody, {flush: !!wts.length}),
    h("div", {class: "stack-v"}, panel("Configuration drift", drift ? drift.length : null, null, driftBody),
      panel("Disk", null, null, sizes.length ? [rankedBars(sizes, kb, id => openProject(id)), h("p", {class: "t2", style: "font-size:var(--fs-1);margin:10px 0 0"}, "Reclaim safely with ", h("code", {text: "dev gc"}), " (dry run).")] : empty("Not measured yet", "The hourly sync measures sizes.")))));
}
/* the work state dev doctor shows (selfcheck.work_state) for each project with open work: items doing without a lock, open on a
   merged branch or under one title, and runs that stopped moving or began before the latest release, each with its fix */
function workStatePanel() {
  const ws = D.work_state;
  if (!ws) return panel("Work state", null, null, empty("Not checked", "This copy of the page has no work state. ", h("code", {text: "dev doctor"}), " checks the project you are in."));
  const rows = []; for (const p of ws.projects || []) for (const x of p.rows || []) rows.push([p, x]);
  const fixes = x => String(x.fix || "").split(" · ").filter(Boolean);
  const typed = c => /<[^>]+>/.test(c);   /* a part only you can fill in, in the run's own session: words to read, never a copy button */
  /* the fix reads in the row (the copy button shows on hover and focus only, as in every list here) */
  const row = (p, x) => { const copyable = fixes(x).filter(c => !typed(c));
    return h("li", {class: "act", "data-key": "work~" + p.project + "~" + x.kind + "~" + (x.run || x.text)}, stateDot("warn", "Stale"),
      h("div", {style: "min-width:0"}, h("div", {class: "strong", text: x.text}),
        h("div", {class: "ad", style: "display:block"}, scopeName(p.project), fixes(x).length ? [" · Fix: ", fixes(x).flatMap((c, i) => (i ? [" · "] : []).concat(h("code", {text: c})))] : null)),
      copyable.length ? h("div", {class: "ar"}, copyable.map(c => icopy(c, "Copy the fix"))) : h("span")); };
  const none = ws.checked ? "No stale work items or runs in the " + plural(ws.checked, "project") + " with open work." : "No project cloned here has open work.";
  return panel("Work state", ws.count || null, null, rows.length ? h("ul", {class: "acts"}, rows.map(([p, x]) => row(p, x))) : empty("Nothing stale", none), {flush: !!rows.length});
}
/* Health: status tiles first (agents, guard, canary, drift, worktrees, disk), the detail below */
function viewHealthV3(main) {
  const pv = D.providers || {}, c = D.canary || {}, sum = c.summary || {}, rules = D.guard_rules || [], drift = D.drift;
  const wts = []; for (const p of P) for (const w of p.wts) wts.push(w);
  const recl = wts.filter(w => w.pr && /MERGED|CLOSED/.test(w.pr.state) && !w.active).length, disk = P.reduce((a, p) => a + (p.size_kb || 0) + p.wts.reduce((b, w) => b + (w.size_kb || 0), 0), 0);
  const prov = key => { const p = pv[key]; if (!p || !p.installed) return null; const q = p.quota || {}, used = q.used_percent;
    const tone = p.logged_in === false ? "warn" : q.available === false ? "risk" : "ok", word = p.logged_in === false ? "Not signed in" : q.available === false ? "Quota used up" : "Signed in";
    return statTile({label: p.name || key, value: word, viz: used != null ? segBar([[tone === "risk" ? "risk" : used >= 70 ? "warn" : "mark", Math.round(used), "Used"], ["none", Math.max(0, 100 - Math.round(used)), "Left"]], "Quota") : h("span", {class: "stat t2"}, h("span", {class: "dot k-" + tone, "aria-hidden": "true"}), q.note || "Quota not reported"), cls: "sp-2 wordv"}); };
  const can = Object.keys(sum).map(a => [a, sum[a][0], sum[a][1]]), ok = can.reduce((a, x) => a + x[1], 0), all = can.reduce((a, x) => a + x[2], 0);
  main.append(h("div", {class: "head"}, h("h1", {text: "Health"}), h("span", {class: "sub", text: "Agents, guard, configuration and disk"})));
  main.append(h("div", {class: "g12 tiles"}, prov("claude"), prov("codex"),
    statTile({label: "Guard decisions" + guardWindow(), value: rules.reduce((a, r) => a + r.count, 0), viz: segBar(rules.slice(0, 4).map((r, i) => [i ? "none" : "mark", r.count, r.label]), "Guard decisions by rule"), cls: "sp-2"}),
    statTile({label: "Status block, 7 days", value: all ? Math.round(100 * ok / all) + "%" : "No data", cls: all ? "sp-2" : "sp-2 wordv", viz: all ? segBar([["ok", ok, "Started with it"], ["risk", all - ok, "Missed"]], "Replies in 7 days") : h("span", {class: "t2", text: "No replies yet"}), legend: all ? legend([["ok", ok, "Started with it"], ["risk", all - ok, "Missed"]]) : null}),
    statTile({label: "Configuration drift", value: drift == null ? "Not reported" : drift.length, cls: drift == null ? "sp-2 wordv" : "sp-2", viz: h("span", {class: "stat t2"}, h("span", {class: "dot k-" + (drift && drift.length ? "warn" : "ok"), "aria-hidden": "true"}), drift == null ? "This registry has no drift report" : drift.length ? "Run dev config diff" : "Matches devkit")}),
    statTile({label: "Worktrees", value: wts.length, unit: disk ? kb(disk) + " on disk" : null, viz: h("span", {class: "stat t2"}, h("span", {class: "dot k-" + (recl ? "warn" : "ok"), "aria-hidden": "true"}), recl ? plural(recl, "finished worktree") + " to reclaim" : "Nothing to reclaim"), cls: "sp-2"})));
  const issues = D.issues || [], flags = allFlags();
  main.append(h("div", {class: "cols even mb"},
    panel("Setup issues", issues.length || null, issues.length ? icopy("dev doctor", "Copy dev doctor") : null, issues.length ? h("ul", {class: "acts"}, issues.map(i => h("li", {class: "act", "data-key": "issue~" + i.kind + "~" + i.text},
      stateDot("warn", "Needs a look"), h("div", {style: "min-width:0"}, h("div", {class: "strong", text: i.text}), h("div", {class: "ad", text: "Kind: " + i.kind})), h("span")))) : empty("No setup issues in the last sync."), {flush: !!issues.length}),
    panel("Contradictions", flags.length || null, null, flags.length ? h("ul", {class: "acts"}, flags.map(f => h("li", {class: "act", onclick: rowClick(f.open)}, stateDot("warn", "Contradiction " + f.code),
      h("div", {style: "min-width:0"}, h("a", {class: "at", href: "#", "data-nav": "", onclick: e => { e.preventDefault(); f.open(); }, text: f.where}), h("div", {class: "ad", style: "display:block"}, flagNodes(f), f.fix ? h("span", {class: "t2", text: " " + f.fix}) : null)),
      f.cmd ? h("div", {class: "ar"}, icopy(f.cmd, "Copy the fix")) : h("span")))) : empty("No contradictions found.", "devkit compares task states, locks, plans and saved statuses on every update."), {flush: !!flags.length})));
  main.append(h("div", {class: "mb"}, workStatePanel()));
  viewHealth(main, true);
  const tp = timePanel(D.timing, true, "How long things take");
  if (tp) main.append(h("div", {class: "mt"}, tp));
}
/* ---------------------------------------------------------------- global search */
let IDX = null;
function buildIndex() {
  IDX = [];
  for (const p of P) {
    IDX.push({type: "project", pid: p.id, title: p.id, text: [p.ns && p.ns.purpose, p.ns && p.ns.state, p.stack.join(" "), p.tags.join(" ")].filter(Boolean).join(" · ")});
    for (const r of p.prsOpen) IDX.push({type: "pr", pid: p.id, title: "#" + r.number + " " + (r.title || ""), text: (r.branch || "") + " " + (CHECKS[r.checks] || ""), url: r.url});
  }
  for (const it of allItems()) IDX.push({type: "task", tid: it.id, pid: isInit(it.scope) ? "" : it.scope, title: (it.legacy ? "" : it.id + " ") + it.title, text: [ISTATUS[it.status] ? ISTATUS[it.status][1] : it.status, scopeName(it.scope), it.claimed_by, it.body].filter(Boolean).join(" · ")});
  for (const x of INITS) IDX.push({type: "initiative", iid: x.id, title: x.title, text: [x.goal, x.scope, x.state, x.projects.join(" ")].filter(Boolean).join(" · ")});
  for (const x of PEOPLE) IDX.push({type: "person", who: x.who, title: x.who, text: x.sessions.map(s => [s.agent, s.project, s.branch, s.progress && s.progress.goal].filter(Boolean).join(" ")).join(" · ")});
  for (const s of (D.session_log || []).slice(0, 600)) IDX.push({type: "session", scope: s.scope, pid: isInit(s.scope) ? "" : s.scope, title: fmt(s.ts) + " · " + scopeName(s.scope), text: [s.who, s.agent, s.branch, s.text].filter(Boolean).join(" · ")});
  for (const f of FILES) { const pr = projectFor(f.path); IDX.push({type: f.path.startsWith("plans/") ? "plan" : f.path.startsWith("projects/") || f.path.startsWith("initiatives/") ? "note" : "file", pid: pr ? pr.id : "", title: f.path, text: f.text, file: f.path}); }
  for (const it of IDX) it.lc = (it.title + "\n" + it.text).toLowerCase();
}
function search(q) {
  if (!IDX) buildIndex(); const tk = tokens(q); if (!tk.length) return [];
  const allowed = new Set(filtered(false).map(p => p.id)); const out = [];
  for (const it of IDX) {
    if (it.pid && facetCount() && !allowed.has(it.pid)) continue;
    if (!tk.every(t => it.lc.includes(t))) continue;
    const tl = it.title.toLowerCase(); out.push([tk.reduce((a, t) => a + (tl.includes(t) ? 3 : 1), 0) + ({initiative: 3.5, project: 3, person: 3, pr: 2.5, task: 2.2, note: 1.5, plan: 1.5, session: 1, file: 0.5}[it.type] || 0), it]);
  }
  return out.sort((a, b) => b[0] - a[0]).map(x => x[1]);
}
function snippet(text, q) { text = plain(text); const tk = tokens(q), lc = text.toLowerCase(); let i = -1; for (const t of tk) { i = lc.indexOf(t); if (i >= 0) break; }
  if (i < 0) return text.slice(0, 160); const s = Math.max(0, i - 70); return (s ? "…" : "") + text.slice(s, s + 180) + (s + 180 < text.length ? "…" : ""); }
function viewSearch(main) {
  const res = search(S.q), types = ["initiative", "project", "person", "task", "pr", "note", "plan", "session", "file"], n = {}; for (const t of types) n[t] = 0; for (const r of res) n[r.type]++;
  const shown = res.filter(r => !S.types.length || S.types.includes(r.type));
  main.append(h("div", {class: "head"}, h("h1", {text: "Search"}), h("span", {class: "sub", text: plural(res.length, "result") + " for “" + S.q + "”"})));
  main.append(h("div", {class: "toolbar"}, h("div", {class: "seg", role: "group", "aria-label": "Result types"}, types.filter(t => n[t]).map(t => h("button", {type: "button", "aria-pressed": String(S.types.includes(t)), onclick: () => set({types: S.types.includes(t) ? S.types.filter(x => x !== t) : S.types.concat([t])}, true)}, (t === "pr" ? "PRs" : t === "person" ? "people" : t + "s") + " " + n[t])))));
  if (!shown.length) { main.append(h("div", {class: "panel"}, empty("Nothing found", "Try fewer words or clear filters. Search covers initiatives, projects, people, tasks, PRs, notes, plans, sessions and every Markdown file in the handoff repo."))); return; }
  const box = h("div", {class: "results panel"});
  for (const r of shown.slice(0, 300)) {
    const run = () => r.url ? openUrl(r.url) : r.tid ? openTask(r.tid) : r.file ? set({view: "explorer", id: "", f: r.file, fq: S.q}, true) : r.iid ? openInitiative(r.iid) : r.who ? go("team") : r.scope ? openScope(r.scope, "sessions") : openProject(r.pid);
    const el = h("button", {class: "res", type: "button", "data-nav": "", onclick: run},
      h("div", {class: "rt"}, h("span", {class: "tag", text: r.type}), h("span", {class: "ell"}, inline(r.title.slice(0, 200)))), h("div", {class: "rs"}, r.pid && r.type !== "project" && BYID.has(r.pid) ? (BYID.get(r.pid).name || r.pid) + " · " : "", snippet(r.text, S.q)));
    highlight(el, S.q); box.append(el);
  }
  main.append(box);
}

/* ---------------------------------------------------------------- task drawer: metadata, lock, history, commands */
let lastFocus = null;
/* ---------------------------------------------------------------- quick-look modals: one native <dialog> (focus trap, Esc, focus returned to the
   trigger), a deep link per item (?modal=<kind>:<id>, and ?task=<id> for tasks), the next action as the one primary button */
let OPENAT = "";   /* "waiting": open the run drawer at its Waiting for you section */
function modalKey() { return S.task ? "task:" + S.task : S.modal || ""; }
function stepRecord(dir) {
  const k = modalKey(), kind = k.slice(0, k.indexOf(":")), keys = [];
  for (const el of $$("[data-open]", $("#view"))) { const v = el.getAttribute("data-open"); if (v.startsWith(kind + ":") && !keys.includes(v)) keys.push(v); }
  const nx = keys[keys.indexOf(k) + dir]; if (keys.indexOf(k) < 0 || !nx) return;
  const j = nx.indexOf(":"); if (nx.startsWith("task:")) openTask(nx.slice(5)); else openModal(nx.slice(0, j), nx.slice(j + 1));
}
function openRunAt(id) { OPENAT = "waiting"; openModal("run", id); }
function openModal(kind, id) { untip(); set({modal: kind + ":" + id, task: ""}, true); }
function closeModal() { untip(); set({modal: "", task: ""}, true); }
function layerHost() { const d = $("#modal"); return d && d.open ? d : document.body; }
function msec(title, body, right) { return h("section", {class: "msec", "aria-label": title}, h("div", {class: "msec-h"}, h("h3", {text: title}), right || null), body); }
function renderModal() {
  const dlg = $("#modal"), key = modalKey(), i = key.indexOf(":"), kind = i > 0 ? key.slice(0, i) : "", id = i > 0 ? key.slice(i + 1) : "";
  for (const el of [$("#tip"), $("#toast")]) if (el.parentNode !== document.body) document.body.append(el);
  untip();
  const parts = MODALS[kind] ? MODALS[kind](id) : null;
  if (!parts) {
    if (dlg.open) { dlg.close(); if (lastFocus && lastFocus.isConnected) lastFocus.focus({preventScroll: true}); }
    lastFocus = null; return;
  }
  if (!dlg.open) lastFocus = document.activeElement;
  const closeBtn = h("button", {class: "btn icon ghost", type: "button", onclick: closeModal, "aria-label": "Close (Esc)", title: "Close (Esc)"}, icon("close"));
  const foot = (parts.foot || []).filter(Boolean);
  append(clear(dlg), [h("header", {class: "mh"}, h("div", {class: "mh-t"}, parts.head), closeBtn), h("div", {class: "mbody", "aria-busy": parts.busy ? "true" : null}, parts.body), foot.length ? h("footer", {class: "mf"}, foot) : null]);
  const opening = !dlg.open;
  if (opening) dlg.showModal();
  if (!LIVE.quiet) closeBtn.focus({preventScroll: true});   /* a live refresh keeps the reader's focus and place */
  if (parts.after && (opening || !LIVE.quiet)) parts.after(dlg);
  if (opening && kind === "run" && OPENAT === "waiting") { const w = $("#waiting-for-you", dlg); if (w) { w.scrollIntoView({block: "start"}); w.focus({preventScroll: true}); } }
  OPENAT = "";
}
function mtitle(text) { return h("h2", {id: "modal-title", text}); }
/* ---------------------------------------------------------------- details on demand: the live page fetches a drawer's record once and again when
   its area changes; the static copy shows what the snapshot holds and says what is missing */
const DET = new Map();
const DETAIL_AREA = {task: "items", run: "runs", job: "jobs", ci: "ci"};
const KINDWORD = {task: "Task", run: "Run", job: "Job", ci: "CI result", member: "Crew member"};
const MISSING = {task: "It may have been deleted or moved to another project. The lists update on their own.",
  run: "Runs live on the machine that ran them, and a finished run leaves this page after an hour.",
  job: "There is no job with this name. dev jobs lists them.", ci: "A newer result for the same branch replaced it.", member: "The crew roster has no member with this id. dev crew lists them."};
function detailPath(kind, id) { const e = encodeURIComponent(id); return {task: "/api/v1/items/" + e, run: "/api/v1/runs/" + e, job: "/api/v1/jobs/" + e, ci: "/api/v1/ci"}[kind]; }
function detail(kind, id) {
  if (!LIVE.on) return {state: "static"};
  const k = kind + ":" + id; if (!DET.has(k)) loadDetail(kind, id); return DET.get(k);
}
async function loadDetail(kind, id, refresh) {
  const k = kind + ":" + id, prev = DET.get(k); let next = null;
  if (!prev) DET.set(k, {state: "loading"});
  try {
    const r = await fetch(detailPath(kind, id), {cache: "no-store", credentials: "same-origin", headers: {Accept: "application/json"}});
    if (r.status === 401) { setLive("ended"); next = {state: "ended"}; }
    else if (r.status === 404) next = {state: prev && prev.state === "ok" ? "removed" : "missing"};
    else if (!r.ok) { let pb = {}; try { pb = await r.json(); } catch (e) { pb = {}; } next = {state: "error", problem: pb, status: r.status}; }
    else {
      let data = await r.json();
      if (kind === "ci") data = (data.watches || []).find(w => ciKey(w) === id) || null;
      next = !data ? {state: prev && prev.state === "ok" ? "removed" : "missing"}
        : {state: "ok", data, updated: refresh && prev && prev.state === "ok" && JSON.stringify(prev.data) !== JSON.stringify(data) ? Date.now() : (prev && prev.updated) || 0};
    }
  } catch (e) { next = {state: "stopped"}; }
  DET.set(k, next);
  if (next.updated && (!prev || next.updated !== prev.updated)) announce(KINDWORD[kind] + " updated.");
  if (modalKey() === k) rerenderModal();
}
function refreshDetail(changed) {
  const k = modalKey(), i = k.indexOf(":"), kind = k.slice(0, i), all = changed.includes("*");
  for (const key of Array.from(DET.keys())) { const kk = key.slice(0, key.indexOf(":")); if (key !== k && (all || changed.includes(DETAIL_AREA[kk]))) DET.delete(key); }
  if (LIVE.on && DETAIL_AREA[kind] && $("#modal").open && (all || changed.includes(DETAIL_AREA[kind]))) loadDetail(kind, k.slice(i + 1), true);
}
function retryDetail() { DET.delete(modalKey()); rerenderModal(); }
/* the focus inside the open drawer, put back after a redraw: the same control (by id, name, then words, then place), else the close button */
function dlgFocus(dlg) {
  const act = document.activeElement, sel = "a,button,input,select,summary,[tabindex]", words = el => (el.textContent || "").replace(/\s+/g, " ").trim();
  if (!dlg || !dlg.open || !dlg.contains(act)) return () => {};
  const id = act.id, name = act.getAttribute("aria-label"), said = words(act), at = $$(sel, dlg).indexOf(act);
  return () => {
    if (!dlg.open || dlg.contains(document.activeElement)) return;
    const all = $$(sel, dlg), f = (id && all.find(el => el.id === id)) || (name && all.find(el => el.getAttribute("aria-label") === name))
      || (said && all.find(el => words(el) === said)) || all[at] || $(".mh .btn.icon", dlg);
    if (f) f.focus({preventScroll: true});
  };
}
/* a re-render keeps the drawer's scroll, the scroll of its logs (or their bottom) and the focus inside it */
function rerenderModal() {
  const dlg = $("#modal"); if (!dlg.open) { renderModal(); return; }
  const b = $(".mbody", dlg), y = b ? b.scrollTop : 0, back = dlgFocus(dlg);
  const logs = $$("pre.log[data-key]", dlg).map(p => [p.getAttribute("data-key"), p.scrollTop, p.scrollTop + p.clientHeight >= p.scrollHeight - 4]);
  LIVE.quiet = true; try { renderModal(); } finally { LIVE.quiet = false; }
  const nb = $(".mbody", dlg); if (nb) nb.scrollTop = y;
  for (const [key, top, bottom] of logs) { const p = $$("pre.log[data-key]", dlg).find(x => x.getAttribute("data-key") === key); if (!p) continue;
    if (bottom) p.scrollTop = p.scrollHeight; else { p.scrollTop = top; if (p.scrollHeight > p.clientHeight + top + 8) { const nl = p.nextElementSibling; if (nl && nl.classList.contains("newl")) nl.hidden = false; } } }
  back();
}
/* the drawer states every kind shares: not found, removed while open, server stopped, session ended, other errors */
function drawerProblem(kind, id, det, crumbsList) {
  const word = KINDWORD[kind] || "Record", close = h("button", {class: "btn", type: "button", onclick: closeModal}, "Close");
  const again = h("button", {class: "btn", type: "button", onclick: retryDetail}, "Try again");
  const head = t => [crumbsList ? crumbs(crumbsList) : null, mtitle(t)];
  if (det.state === "missing" || det.state === "removed")
    return {head: head(word + " " + id + " no longer exists"), body: [h("p", {class: "lead"}, det.state === "removed" ? "It was removed while you were looking at it. " : "", MISSING[kind] || "")], foot: [close]};
  if (det.state === "stopped")
    return {head: head("Could not load the details"), body: [h("p", {class: "lead"}, "The dashboard server is not answering. It stops by itself after 30 minutes without an open page. Start it again with ", h("code", {text: "dev dashboard"}), ".")], foot: [again, cmdBtn("dev dashboard", true, "Copy dev dashboard")]};
  if (det.state === "ended")
    return {head: head("This page is no longer signed in"), body: [h("p", {class: "lead"}, "The dashboard server restarted with a new key. Run ", h("code", {text: "dev dashboard"}), " to open a signed-in page.")], foot: [cmdBtn("dev dashboard", true, "Copy dev dashboard")]};
  if (det.state === "error") { const pb = det.problem || {};
    return {head: head("Could not load the details"), body: [h("p", {class: "lead", text: (pb.title || "Error " + (det.status || "")) + (pb.detail ? ": " + pb.detail : "")})], foot: [again]}; }
  return null;
}
function skeleton() { return h("div", {class: "skel", role: "status"}, h("span", {class: "sr", text: "Loading details…"}), h("i"), h("i"), h("i", {class: "blk"}), h("i"), h("i"), h("i", {class: "blk"}), h("i"), h("i")); }
function staticNote(what) {
  return h("div", {class: "notice"}, "A static copy from " + fmt(D.generated) + ". " + (what ? "The whole " + what + " needs the live dashboard: run " : "Run "), h("code", {text: "dev dashboard"}), what ? "." : " for the live page.", " ", icopy("dev dashboard", "Copy"));
}
function updatedNote(det) { return det && det.updated ? "Updated " + (Date.now() - det.updated < 60000 ? "just now" : rel(det.updated / 1000)) : null; }
function untrusted(text, label, key) { return h("pre", {class: "log", tabindex: "0", "aria-label": label, "data-key": key || null}, String(text == null ? "" : text)); }
/* "› More" for long text: two or three lines, then all of it (textContent only) */
function moreText(text, lines) {
  const cls = lines === 2 ? "clamp2" : "clamp3", p = h("div", {class: cls, text: String(text || "")}), b = h("button", {class: "linkbtn", type: "button", "aria-expanded": "false"}, "› More");
  b.addEventListener("click", e => { e.stopPropagation(); const on = b.getAttribute("aria-expanded") !== "true"; p.className = on ? "" : cls; b.setAttribute("aria-expanded", String(on)); b.textContent = on ? "› Less" : "› More"; });
  return String(text || "").length > (lines === 2 ? 160 : 240) ? h("div", null, p, b) : p;
}
/* "Show all {n} …": the first `n`, then a button; the expansion survives live updates (SHOWN) */
function showAll(key, list, n, rowFn, word) {
  const all = !!SHOWN[key], shown = all ? list : list.slice(0, n);
  return [shown.map(rowFn), list.length > n && !all ? h("li", {class: "showall"}, h("button", {class: "btn sm", type: "button", onclick: e => { e.stopPropagation(); SHOWN[key] = 1; rerenderModal(); }}, "Show all " + list.length + " " + word)) : null];
}
const STEPW = {running: ["▸", "Running"], pending: ["○", "To do"], blocked: ["■", "Blocked"], waiting: ["■", "Waiting for you"], done: ["✓", "Done"], skipped: ["–", "Skipped"], lost: ["!", "Stopped reporting"]};
function stepTime(s) {
  if (s.state === "running" || s.state === "done") return fmtMin(s.min || 0) + (s.est ? " of ~" + fmtAbout(s.est) : "");
  return s.est ? "~" + fmtAbout(s.est) + (s.est_auto ? ", a default estimate" : "") : "";
}
function findRun(id) { return RUNS.find(x => x.id === id) || RRUNS.find(x => x.id === id) || null; }
function sessOfRun(r) { for (const x of PEOPLE) for (const s of x.sessions) if (s.session === r.session && s.agent === r.agent) return s; return null; }
/* ---------------------------------------------------------------- the run drawer: every step with its owner and evidence, the crew, what waits for you */
function runModal(id) {
  const r = findRun(id), remote = !!(r && r.remote);
  const det = remote ? {state: "static"} : detail("run", id), cr = [["Background work", link({view: "background"})], ["Runs"]];
  if (!r && det.state === "loading") return {head: [crumbs(cr), mtitle("Run")], body: [skeleton()], busy: true};
  if (!r && det.state === "static") return drawerProblem("run", id, {state: "missing"}, cr);
  const prob = drawerProblem("run", id, det, cr); if (prob) return prob;
  const d = det.state === "ok" ? det.data : null, x = Object.assign({}, r || {}, d || {}), st = runState(x);
  const steps = d ? d.steps || [] : (r.plan || []), lanes = d ? d.lanes || [] : (r.agents || []).map(a => ({label: a.label, state: a.state, min: a.min}));
  const decs = d ? d.decisions || [] : r.decisions || [], open = decs.filter(z => !z.state || z.state === "pending"), sess = sessOfRun(x);
  const tm_ = runTime(Object.assign({}, x, {plan: steps})), branch = (sess && sess.branch) || x.branch;
  const head = [crumbs(cr), mtitle(x.goal || "Agent run"),
    h("div", {class: "pline"}, meta(stateWord(RUNTONE[st], runStateText(x, st)), x.mode === "auto" ? "Auto" : "Guided", x.project ? scopeName(x.project) : null, branch ? h("span", {class: "meta"}, icon("branch"), branch) : null,
      agentName(x.agent), remote ? "on " + x.host : null, updatedNote(det)))];
  if (det.state === "loading") return {head, body: [skeleton()], busy: true};
  const pct = tm_.total ? 100 * tm_.done / tm_.total : 0, cal = x.estimate_cal && x.estimate && x.estimate_cal !== x.estimate ? x.estimate_cal / x.estimate : 0;
  const first = runFirstEst(x), planned = x.total_min != null && fmtAbout(first) !== fmtAbout(x.total_min);   /* "of about" is the plan's total, not the first estimate */
  const figs = h("div", {class: "mfigs"},
    h("div", null, h("span", {class: "s-l", text: "Progress"}), h("span", {class: "strong num", text: tm_.steps}),
      h("div", {class: "rbar" + (tm_.over ? " over" : ""), role: "progressbar", "aria-valuenow": Math.round(pct), "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuetext": tm_.steps}, h("i", {style: "width:" + pct.toFixed(1) + "%"}))),
    h("div", null, h("span", {class: "s-l", text: "Time"}), h("span", {class: "num", text: tm_.text + (cal && !planned ? ": estimated " + fmtAbout(x.estimate) + ", ×" + cal.toFixed(1) + " from this project's runs" : "")}),
      planned && first ? h("span", {class: "t2 num", text: "first estimate " + fmtAbout(first)}) : null,
      tm_.over ? h("span", {class: "warn-t", text: "over the estimate by " + fmtMin(tm_.over)}) : null),
    h("div", null, h("span", {class: "s-l", text: "Left"}), h("span", {class: "num", text: etaText(x).replace(/ left$/, "") || (x.state === "done" ? "Done" : "No estimate")}),
      x.pace && Math.abs(x.pace - 1) >= 0.05 ? h("span", {class: "t2", text: "pace " + (+x.pace).toFixed(1) + "× the estimate"}) : null));
  const stepLine = s => {
    const dec = open.find(z => z.step === s.name), w = s.waiting || dec ? "waiting" : s.state, sw = stepWord(w), ev = (s.evidence || []).slice(-1)[0], code = stepCode(s);
    const scope = [].concat(s.scope || []).filter(Boolean), files = scope.length === 1 ? String(scope[0]).split(/[\s,]+/).filter(Boolean) : scope;
    const after = (s.after || []).map(a => { const o = steps.find(z => z.name === a); return o ? stepWord(o.state)[0] + " " + stepTitle(o) : "○ " + a; });
    const hist = (s.evidence || []).map(e => ({ts: e.ts, text: [e.kind, e.text, e.check, e.next ? "next: " + e.next : ""].filter(Boolean).join(" · ")}))
      .concat((s.history || []).map(e => ({ts: e.ts, text: (e.from || "") + " → " + (e.to || "") + " · " + who(e.by || "main") + (e.why ? " · " + e.why : "")}))).sort((a, b) => (b.ts || 0) - (a.ts || 0));
    const rows = [s.desc ? h("dd", {class: "wide", text: s.desc}) : null,
      ev ? [h("dt", {text: "Evidence"}), h("dd", {class: "ck " + ckClass(ev.check)}, moreText(ev.check || ev.text, 2))] : null,
      files.length ? [h("dt", {text: "Files"}), h("dd", {title: files.join(", "), text: files.slice(0, 3).join(", ") + (files.length > 3 ? ", +" + (files.length - 3) + " more" : "")})] : null,
      after.length ? [h("dt", {text: "Waits for"}), h("dd", {text: after.join(", ")})] : null,
      s.state === "pending" && s.label && (!s.owner || s.owner === "main") ? h("dd", {class: "wide t2", text: "Assigned to " + who(s.label) + ", not started."}) : null,
      dec ? h("dd", {class: "wide bad", text: "Waiting for your answer to " + dec.id + "."}) : null,
      hist.length ? h("dd", {class: "wide"}, h("details", {class: "recent", "data-key": "ev~" + s.name}, h("summary", null, "Evidence and history (" + hist.length + ")"),
        h("ul", {class: "rrows"}, hist.map(e => h("li", null, h("span", {class: "t2", text: fmt(e.ts) + " · "}), e.text))))) : null].filter(Boolean);
    return h("li", {class: w}, h("span", {class: "sn", text: String(steps.indexOf(s) + 1)}), h("span", {class: "g", "aria-hidden": "true", text: sw[0]}),
      h("span", {class: "nm"}, h("span", {class: "sr", text: sw[1] + ": "}), stepTitle(s), h("span", {class: "ow", text: " · " + stepWho(s)}),
        code ? h("span", {class: "ow"}, " · ", h("span", {class: "sc", title: "The step's name in dev progress", text: code})) : null),
      h("span", {class: "tm num", text: sw[1] + (stepTime(s) ? " · " + stepTime(s) : "")}), rows.length ? h("dl", {class: "sx"}, rows) : null);
  };
  const f = SHOWN.stepf || "all", nOpen = steps.filter(s => s.state !== "done" && s.state !== "skipped").length;
  const shownSteps = steps.filter(s => f === "all" || (f === "open" ? s.state !== "done" && s.state !== "skipped" : s.state === "done" || s.state === "skipped"));
  const segF = h("div", {class: "seg", role: "group", "aria-label": "Show steps"}, [["all", "All", steps.length], ["open", "Open", nOpen], ["done", "Done", steps.length - nOpen]].map(([v, l, n]) =>
    h("button", {type: "button", "aria-pressed": String(f === v), onclick: () => { SHOWN.stepf = v; rerenderModal(); }}, l, h("span", {class: "sn num", text: String(n)}))));
  const laneRow = l => {
    const lst = ["running", "lost", "waiting", "unverified"].includes(l.state) ? l.state : "done", sw = lst === "running" ? ["▸", "Working"] : stepWord(lst);
    const own = steps.filter(s => (l.id && s.owner === l.id) || (!l.id && s.owner === l.label)), ups = (l.updates || []).slice().reverse(), scope = [].concat(l.scope || []).filter(Boolean);
    return h("li", {class: lst}, h("span", {class: "sn"}), h("span", {class: "g", "aria-hidden": "true", text: sw[0]}),
      h("span", {class: "nm"}, h("span", {class: "sr", text: sw[1] + ": "}), who(l.label, l.persona), own.length ? h("span", {class: "ow", text: " · " + own.map(stepTitle).join(", ")}) : null),
      h("span", {class: "tm num", text: sw[1] + " · " + fmtMin(l.min || 0) + (l.done ? " · " + l.done + " done" : "")}),
      ups.length || scope.length ? h("dl", {class: "sx"}, scope.length ? [h("dt", {text: "Files"}), h("dd", {text: scope.join(", ")})] : null,
        ups.length ? [h("dt", {text: "Latest"}), h("dd", null, ups[0].text || "", " ", h("span", {class: "t2", text: rel(ups[0].ts)}))] : null,
        ups.length > 1 ? h("dd", {class: "wide"}, h("details", {class: "recent", "data-key": "up~" + (l.id || l.label)}, h("summary", null, "Updates (" + ups.length + ")"),
          h("ul", {class: "rrows"}, ups.slice(1).map(u => h("li", null, h("span", {class: "t2", text: fmt(u.ts) + " · "}), u.text || ""))))) : null) : null);
  };
  const qs = d ? (d.questions || []).filter(m => m.from !== "ci") : (r.questions || []);
  const laneName = idx => { if (idx === "main") return roleName(mainMember()); const l = lanes.find(z => z.id === idx); return l ? who(l.label, l.persona) : idx; };
  /* the server says which messages still wait (`waiting`): a notice, an answer or a replied question never does */
  const qOpen = q => q.waiting != null ? !!q.waiting : !q.answered && !qs.some(z => z.reply_to === q.id);
  const qRow = q => { const ans = qs.find(z => z.reply_to === q.id), to = q.to_name ? who(q.to_name) : laneName(q.to || "main");
    return h("li", null, h("div", {class: "slw"}, meta(h("strong", {text: q.id}), who(q.from_name || q.from || "main") + " → " + to, rel(q.ts),
      ans ? "Answered in " + ans.id : q.answered ? "Answered" : q.waiting === false ? (q.reply_to ? "Answers " + q.reply_to : "Notice")
        : q.delivered === false ? "Not delivered yet: it reaches " + to + " with their next tool call" : "Open")), moreText(q.text, 3)); };
  const decided = decs.filter(z => z.state === "decided").sort((a, b) => (b.decided || b.ts || 0) - (a.decided || a.ts || 0));
  /* a moot decision waits for nobody: its asking member ended and no step waits on it (the server's words) */
  const moot = decs.filter(z => z.state === "moot");
  const ups = (d ? (d.updates || []).filter(u => u.kind !== "agent") : (r.updates || [])).slice().reverse(), hist = d ? (d.history || []).slice().reverse() : [];
  const body = [figs, runFlags(x, d).map(flagCallout),
    remote ? h("div", {class: "notice"}, "This run is on " + x.host + ". Its evidence, history and questions stay on that machine; open the dashboard there to see them. Last presence update " + rel(x.asOf) + ".") : null,
    !remote && det.state === "static" ? staticNote("run: evidence, history and questions") : null,
    open.length ? h("section", {class: "msec", id: "waiting-for-you", "aria-labelledby": "wfy-h", tabindex: "-1"}, h("div", {class: "msec-h"}, h("h3", {id: "wfy-h", text: "Waiting for you"}), h("span", {class: "count num", text: String(open.length)})),
      h("ul", {class: "acts"}, open.map(z => h("li", {class: "act"}, stateDot("risk"), h("div", {style: "min-width:0"}, h("div", {class: "strong"}, z.id + "  ", z.question || ""),
        h("div", {class: "ad", style: "display:block", text: (z.options || []).map(o => o + (o === z.recommend ? " (recommended)" : "")).join(" · ")}),
        z.irreversible ? h("div", {class: "ad bad", style: "display:block", text: "Irreversible: " + z.irreversible + ". Auto mode never takes it without your approval."}) : null,
        h("div", {class: "ad", style: "display:block", text: "Asked by " + who(z.from_name || "main") + " " + rel(z.ts) + "." + (z.step ? " It blocks “" + stepTitle(steps.find(q => q.name === z.step) || {name: z.step}) + "”." : "") + " Answer in the session; " + roleName(mainMember()) + " records it."})), h("span"))))) : null,
    msec("Steps", steps.length ? h("ol", {class: "steps"}, shownSteps.map(stepLine)) : empty("No steps planned", "The run has a goal but no plan yet."), h("span", {style: "display:flex;gap:8px;align-items:center;margin-left:auto"}, h("span", {class: "count num", text: String(steps.length)}), steps.length ? segF : null)),
    msec("Crew", lanes.length ? h("ol", {class: "steps"}, lanes.map(laneRow)) : empty("Only the main session worked on this run."), lanes.length ? h("span", {class: "count num", text: plural(lanes.length, "lane")}) : null),
    d || qs.length ? msec("Questions between the crew", qs.length ? h("ul", {class: "rrows"}, qs.filter(qOpen).concat(qs.filter(q => !qOpen(q)).reverse()).map(qRow)) : empty("No questions between the crew."), qs.length ? h("span", {class: "count num", text: qs.length + " · " + qs.filter(qOpen).length + " open"}) : null) : null,
    d ? msec("Decisions", decided.length || moot.length ? h("ul", {class: "rrows"}, decided.map(z => h("li", null, h("div", {class: "strong", text: z.question || z.id}),
      h("div", {class: "slw"}, meta("→ " + (z.answer || "—"), z.by === "auto" ? "automatically" : z.by === "main" ? "by " + roleName(mainMember()) : "by you", rel(z.decided || z.ts), z.by === "auto" ? h("span", {class: "tag", text: "Decided automatically: review it"}) : null)),
      z.overrides ? h("div", {class: "t2", text: "You changed it: it was “" + z.overrides + "”."}) : null)).concat(moot.map(z => h("li", {"data-key": "moot~" + z.id},
      h("div", {class: "t2"}, h("strong", {text: z.id}), " " + (z.question || "")), h("div", {class: "t2", text: z.moot || "Moot: nobody waits on it."}))))) : empty("No decisions recorded.")) : null,
    msec("Updates", ups.length ? h("ul", {class: "rrows"}, showAll("ups~" + id, ups, 20, u => h("li", null, h("div", {class: "slw"}, meta(u.kind || "update", rel(u.ts))), h("div", null, u.text || ""),
      u.check ? h("div", {class: "ck " + ckClass(u.check), text: u.check}) : null, u.next ? h("div", {class: "t2", text: "Next: " + u.next}) : null), "updates")) : empty("No updates yet.")),
    d ? msec("Run history", hist.length ? h("ul", {class: "rrows"}, showAll("hist~" + id, hist, 20, e => h("li", {text: fmt(e.ts) + " · " + (e.step || "") + " · " + (e.from || "") + " → " + (e.to || "") + " · " + who(e.by || "main") + (e.why ? " · " + e.why : "")}), "changes")) : empty("No step changes recorded yet.")) : null,
    msec("Details", h("dl", {class: "kv"}, h("dt", {text: "Run id"}), h("dd", {class: "mono", text: x.id || id}), h("dt", {text: "Agent"}), h("dd", {text: agentName(x.agent)}),
      x.session ? [h("dt", {text: "Session"}), h("dd", {class: "mono", text: x.session})] : null, x.project ? [h("dt", {text: "Project"}), h("dd", {text: x.project})] : null,
      h("dt", {text: "Started"}), h("dd", {text: fmt(x.started)}), x.finished ? [h("dt", {text: "Finished"}), h("dd", {text: fmt(x.finished)})] : null,
      h("dt", {text: "Estimate"}), h("dd", {text: x.estimate ? fmtMin(x.estimate) + (x.estimate_cal && x.estimate_cal !== x.estimate ? ", calibrated " + fmtMin(x.estimate_cal) : "") : "No estimate"}),
      h("dt", {text: "Mode"}), h("dd", {text: x.mode === "auto" ? "Auto: the crew takes the recommended option and records it; irreversible actions still wait for you" : "Guided: decisions wait for you"})))];
  return {head, body, foot: [x.project && (BYID.has(x.project) || MEMBEROF.has(x.project)) ? h("a", {class: "btn", href: link({view: "projects", id: x.project})}, "Open project") : null]};
}
/* ---------------------------------------------------------------- the job drawer: did it run, did it work, and if not, why */
function jobModal(name) {
  const j = JOBS.find(x => x.name === name), det = detail("job", name), cr = [["Background work", link({view: "background"})], ["Scheduled jobs"]];
  if (!j && det.state !== "ok" && det.state !== "loading") return drawerProblem("job", name, det.state === "static" ? {state: "missing"} : det, cr);
  const prob = drawerProblem("job", name, det, cr); if (prob) return prob;
  const x = Object.assign({}, j || {}, det.state === "ok" ? det.data : {}), st = jobState(x), last = x.last || null, wnd = W.jobs_days || 30;
  const head = [crumbs(cr), mtitle(name), x.about ? h("div", {class: "pline", text: x.about}) : null,
    h("div", {class: "pline"}, meta(stateWord(JOBST[st][0], jobWord(x, st)), schedText(x.schedule), x.enabled ? "On" : "Off", updatedNote(det)))];
  if (det.state === "loading") return {head, body: [skeleton()], busy: true};
  const hist = det.state === "ok" ? x.history || [] : last ? [last] : [];
  const resWord = r => r.rc === 0 ? "OK" : r.rc == null ? "Skipped" : r.rc === 124 ? "Timed out" : "Failed (exit " + r.rc + ")";
  let strip = null;
  if (hist.length) {
    const rs = hist.slice(0, 40).reverse(), mx = Math.max(1, ...rs.map(r => r.duration || 0)), bw = 5, g = 2, H = 24, w = rs.length * (bw + g) - g;
    const med = rs.map(r => r.duration || 0).sort((a, b) => a - b)[Math.floor(rs.length / 2)] || 0, nf = rs.filter(r => r.rc !== 0 && r.rc != null).length;
    strip = sv("svg", {class: "spark jstrip", width: w, height: H, viewBox: "0 0 " + w + " " + H, role: "img", "aria-label": plural(rs.length, "run") + " in " + wnd + " days, " + nf + " failed, median " + jobDur(med)},
      rs.map((r, i) => { const bh = Math.max(3, Math.round((r.duration || 0) / mx * H)); return sv("rect", {x: i * (bw + g), y: H - bh, width: bw, height: bh, rx: 1, class: r.rc !== 0 && r.rc != null ? "k-risk" : "k-mark", "data-tip": clock(r.start) + ": " + jobDur(r.duration) + ", " + resWord(r)}); }));
  }
  const log = det.state === "ok" ? x.log || [] : null;
  const body = [st === "unscheduled" ? callout("warn", "Not scheduled", "launchd does not have this job, so it does not run. Install it with dev jobs install.", cmdBtn("dev jobs install")) : null,
    st === "overdue" ? callout("warn", "Overdue", "It last ran " + rel(last.start) + " and runs " + schedText(x.schedule).replace(/^[A-Z]/, c => c.toLowerCase()) + ". The Mac may have been asleep, or launchd stopped it. dev jobs shows its state.", cmdBtn("dev jobs")) : null,
    st === "off" ? callout("info", "This job is off", "Turn it on with dev jobs enable " + name + ".", cmdBtn("dev jobs enable " + name)) : null,
    h("div", {class: "mfigs"},
      h("div", null, h("span", {class: "s-l", text: "Last run"}), h("span", {class: "num", text: last ? clock(last.start) + " · " + jobDur(last.duration) : "No runs in " + wnd + " days"}), last ? h("span", {class: last.rc !== 0 && last.rc != null ? "bad" : "t2", text: resWord(last)}) : null),
      h("div", null, h("span", {class: "s-l", text: "Next run"}), h("span", {class: "num", text: !x.enabled || !x.next ? "—" : (/^every/.test(x.schedule || "") ? "by " : "") + clock(x.next)})),
      h("div", null, h("span", {class: "s-l", text: "Failures, " + wnd + " days"}), h("span", {class: "num" + (x.failed ? " bad" : ""), text: (x.failed || 0) + " of " + (x.runs || 0)}), strip)),
    det.state === "static" ? staticNote("job: its runs and log") : null,
    msec("Recent runs", hist.length ? h("div", {class: "tw"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["Start", "Duration", "Result", "Last line of output"].map(t => h("th", {scope: "col", text: t})))),
      h("tbody", null, (SHOWN["jh~" + name] ? hist : hist.slice(0, 50)).map(r => h("tr", null, h("td", {class: "num", style: "white-space:nowrap", text: clock(r.start)}), h("td", {class: "num", text: jobDur(r.duration)}),
        h("td", {class: r.rc !== 0 && r.rc != null ? "bad" : null, style: "white-space:nowrap", text: resWord(r)}), h("td", {class: "t2", style: "max-width:0;width:100%"}, h("div", {class: "ell", title: r.summary || "", text: r.summary || "—"}))))))) : empty("No runs in the last " + wnd + " days."),
      hist.length ? h("span", {class: "count num", text: plural(x.runs || hist.length, "run") + " in the last " + wnd + " days"}) : null),
    hist.length > 50 && !SHOWN["jh~" + name] ? h("button", {class: "btn sm", type: "button", onclick: () => { SHOWN["jh~" + name] = 1; rerenderModal(); }}, "Show all " + hist.length + " runs") : null,
    log ? msec("Log, last 200 lines", log.length ? [h("div", {class: "logh"}, h("span", {class: "rpath", title: x.log_path || "", text: x.log_path || ""}), x.log_path ? icopy(x.log_path, "Copy the log path") : null),
      untrusted(log.join("\n"), "The log of the job " + name, "log~" + name), h("button", {class: "btn sm newl", type: "button", hidden: true, onclick: e => { const p = e.currentTarget.previousElementSibling; p.scrollTop = p.scrollHeight; e.currentTarget.hidden = true; }}, "New lines below")]
      : empty("The log is empty.", "It fills the first time the job runs.")) : null,
    msec("Settings", h("dl", {class: "kv"}, h("dt", {text: "Schedule"}), h("dd", {text: schedText(x.schedule)}), h("dt", {text: "Timeout"}), h("dd", {text: x.timeout_min ? x.timeout_min + " min" : "—"}),
      h("dt", {text: "Notify"}), h("dd", {text: x.notify === "always" ? "After every run" : x.notify === "failure" ? "When it fails" : x.notify || "—"}),
      h("dt", {text: "Installed"}), h("dd", {text: x.installed ? "Yes" : "No: the launchd file is missing"}),
      x.loaded != null ? [h("dt", {text: "Loaded in launchd"}), h("dd", {text: x.loaded ? "Yes" : "No"})] : null)),
    msec("Commands", h("div", {class: "cmds"}, cmdRow("Run it now", "dev jobs run " + name), x.enabled ? cmdRow("Turn it off", "dev jobs disable " + name) : cmdRow("Turn it on", "dev jobs enable " + name), cmdRow("Show the journal", "dev jobs journal")))];
  return {head, body, foot: [cmdBtn("dev jobs run " + name, true, "Copy dev jobs run " + name)], after: dlg => { const p = $("pre.log", dlg); if (p) p.scrollTop = p.scrollHeight; }};
}
/* ---------------------------------------------------------------- the CI drawer: why it failed, and what to do now */
function ciModal(key) {
  const w0 = ciLatest().find(w => ciKey(w) === key), det = detail("ci", key), cr = [["Background work", link({view: "background"})], ["CI"]];
  if (!w0 && det.state !== "ok" && det.state !== "loading") return drawerProblem("ci", key, det.state === "static" ? {state: "missing"} : det, cr);
  const prob = drawerProblem("ci", key, det, cr); if (prob) return prob;
  const w = det.state === "ok" ? det.data : w0 || {repo: key.split("@")[0], branch: key.slice(key.indexOf("@") + 1)}, st = ciState(w), fails = w.failures || [], f0 = fails[0] || {}, prov = provName(w.provider);
  const head = [crumbs(cr), mtitle(repoName(w.repo) + " · " + w.branch),
    h("div", {class: "pline"}, meta(stateWord(CIST[st][0], CIST[st][1]), w.sha || null, prov === "the provider" ? null : prov, w.ts ? rel(w.ts) : null, w.minutes != null ? "watched " + (+w.minutes).toFixed(1) + "m" : null, updatedNote(det)))];
  if (det.state === "loading") return {head, body: [skeleton()], busy: true};
  const firstJob = ((f0.jobs || [])[0] || {}).name || "the failed job";
  const body = [det.state === "static" ? staticNote("CI result: the whole first error") : null,
    fails.map((fx, i) => h("section", {class: "msec", "aria-label": (fx.run || "run") + " #" + (fx.number || "")},
      h("div", {class: "msec-h"}, h("h3", {text: (fx.run || "run") + " #" + (fx.number || "?") + " failed"}), h("span", {class: "count", text: ciClass(w, fx)})),
      (fx.jobs || []).map(jb => h("div", {class: "trow"}, h("span", {class: "tx"}, h("span", {class: "t2", text: "Failed job "}), jb.name || "job", jb.step ? ", step “" + jb.step + "”" : ""), jb.url ? h("a", {class: "more", href: jb.url, target: "_blank", rel: "noopener noreferrer"}, "Open job") : null)),
      fx.excerpt ? [h("div", {class: "logh", text: "From the CI log"}), untrusted(fx.excerpt, "First error from the CI log of " + (fx.run || "the run") + " #" + (fx.number || ""), "ci~" + i)] : h("p", {class: "t2", text: "No log was available. Open the run on " + prov + "."}),
      fx.action ? h("div", {class: "trow"}, h("span", {class: "tx"}, h("span", {class: "t2", text: "Next  "}), sentence(fx.action))) : null)),
    st === "passed" ? h("p", {class: "lead", text: "All runs passed."}) : null,
    w.error ? callout("warn", "The watch could not finish: " + w.error, null) : null,
    (w.runs || []).length ? msec("Runs", h("div", {class: "tw"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["Run", "Status", "Minutes", ""].map(t => h("th", {scope: "col", text: t})))),
      h("tbody", null, w.runs.map(rn => h("tr", null, h("td", {text: (rn.name || "run") + " #" + (rn.number || "")}), h("td", {text: rn.status === "success" ? "Passed" : rn.status === "failure" ? "Failed" : rn.status || "—"}),
        h("td", {class: "num", text: rn.elapsed != null ? (+rn.elapsed).toFixed(1) + "m" : "—"}), h("td", null, rn.url ? h("a", {class: "more", href: rn.url, target: "_blank", rel: "noopener noreferrer"}, "Open") : null))))))) : null,
    msec("Commands", h("div", {class: "cmds"}, f0.id ? cmdRow("Read the failed log", "dev run view " + f0.id + " --failed") : null, cmdRow("Follow the pipeline", "dev ci status --watch"),
      st === "failed" ? cmdRow("Hand it to " + who("link"), "dev crew brief link \"Fix the failed CI run: " + firstJob + ", " + ciClass(w, f0).toLowerCase() + "\"") : null))];
  return {head, body, foot: [f0.url ? h("a", {class: "btn", href: f0.url, target: "_blank", rel: "noopener noreferrer"}, "Open the log on " + prov) : null, f0.id ? cmdBtn("dev run view " + f0.id + " --failed", true, "Copy dev run view " + f0.id + " --failed") : null]};
}
/* ---------------------------------------------------------------- a crew member: what they do, their part in each kind of work, their lanes */
const ACCESS = {read: "Read only", review: "Reviews only", build: "Builds, no web", all: "Full"};
const TIER = {strong: "Strongest model", standard: "Standard model", fast: "Fast model"};
function memberModal(id) {
  const m = CREW.find(x => x.id === id), cr = [["Team", link({view: "team", tab: "crew"})], ["Crew"]];
  if (!m) return drawerProblem("member", id, {state: "missing"}, cr);
  const part = k => RACI.filter(a => k === "A" ? a.A === m.id : (a[k] || []).includes(m.id)).map(a => a.name);
  const lanes = crewLanes(true).filter(l => l.m && l.m.id === m.id);
  const list = (title, xs) => [h("h3", {class: "sub3", text: title + " (" + xs.length + ")"}), xs.length ? h("ul", {class: "rrows"}, xs.map(x => h("li", {text: x}))) : h("p", {class: "t2", style: "margin:0;font-size:var(--fs-1)", text: "None"})];
  return {head: [crumbs(cr), mtitle(roleName(m)), h("div", {class: "pline"}, meta(TIER[m.tier] || m.tier || null, ACCESS[m.tools] || m.tools || null, m.main ? "the main session" : null))],
    body: [m.summary ? msec("What they do", h("p", {class: "lead", text: m.summary})) : null,
      msec("Their part in each kind of work", h("div", {class: "cols even", style: "gap:var(--s5)"}, h("div", null, list("Does", part("R")), list("Approves", part("A"))), h("div", null, list("Is consulted on", part("C")), list("Is informed about", part("I"))))),
      msec("Working now and in the last hour", lanes.length ? h("ul", {class: "lrows"}, lanes.map(l => h("li", {class: "lrow", onclick: rowClick(() => openModal("run", l.run.id))}, h("span", {class: "lk", "aria-hidden": "true", text: (STEPW[l.st] || STEPW.running)[0]}),
        h("div", {class: "lm"}, modalLink("run", l.run.id, l.step || l.run.goal || "run", "lt"), h("div", {class: "ls", text: "“" + (l.run.goal || "") + "” · " + l.word + " · " + fmtMin(l.min || 0)}))))) : empty("Not working on a run now.", "Members work on the steps of a run; when one starts, it shows here."))],
    foot: [h("a", {class: "btn", href: link({view: "team", tab: "raci", rm: m.id})}, "Open in Who does what")]};
}
/* ---------------------------------------------------------------- the connected task: who has it, what blocks it, what is next; what it is
   connected to, each with how it was found (an inferred link is never stated as a fact); the whole body and history */
function taskRuns(x, d) {
  if (d) return (d.runs && d.runs.length ? d.runs : d.run ? [d.run] : []).map(r => Object.assign({}, findRun(r.id) || {}, r));
  const m = /\(([^:)]+)(?::([^)]+))?\)/.exec(x.claimed_by || ""), agent = m ? m[1] : "", sess = m ? m[2] || "" : "";
  return RUNS.map(r => { const why = agent && sess && r.agent === agent && String(r.session || "").startsWith(sess) ? "the holder's session runs it"
      : String(r.goal || "").includes(x.id) || (r.plan || []).some(s => String(s.name || "").includes(x.id)) ? "its goal or a step has the id" : "";
    return why ? Object.assign({}, r, {match: why}) : null; }).filter(Boolean).sort((a, b) => (b.started || 0) - (a.started || 0));
}
function connections(x, d) {
  const out = [], low = x.id.toLowerCase(), p = BYID.get(x.scope), runs = taskRuns(x, d);
  if (runs.length) { const r = runs[0], t = runTime(r);
    out.push({kind: "run", k: "Run", icon: "doing", text: (r.goal || r.id) + " · " + t.steps + (etaText(r) ? " · " + etaText(r) : ""), how: r.match, open: findRun(r.id) || d ? ["run", r.id] : null,
      xs: runs.length > 1 ? "and " + plural(runs.length - 1, "older run") : ""}); }
  let branches = d ? d.branches || [] : [];
  if (!d && p) { const names = new Set(), m = /\(([^:)]+)(?::([^)]+))?\)/.exec(x.claimed_by || ""), s = m ? (BYWHO.get(x.holder) || {sessions: []}).sessions.find(z => z.agent === m[1] && String(z.session || "").startsWith(m[2] || "\u0000")) : null;
    for (const [b, wt] of [[(p.main || {}).branch, p.path]].concat(p.wts.map(w => [w.branch, w.path]))) if (b && !names.has(b) && (b.toLowerCase().includes(low) || (s && s.branch === b))) { names.add(b); branches.push({branch: b, worktree: wt, match: b.toLowerCase().includes(low) ? "the branch name has the id" : "the holder's session works on it"}); } }
  for (const b of branches) out.push({kind: "branch", k: "Branch", icon: "branch", text: b.branch + " · " + (p && b.worktree === p.path ? "main checkout" : b.worktree ? String(b.worktree).split("/").slice(-2).join("/") : "a worktree"), how: b.match});
  const bn = new Set(branches.map(b => b.branch));
  const prs = d ? d.prs || [] : p ? p.prsOpen.map(r => Object.assign({}, r, {match: bn.has(r.branch) ? "the PR's branch is linked" : (String(r.branch) + " " + String(r.title)).toLowerCase().includes(low) ? "the branch or title has the id" : ""})).filter(r => r.match) : [];
  for (const r of prs) out.push({kind: "pr", k: "Pull request", icon: "merge", text: "#" + r.number + " " + (r.title || "") + " · " + String(r.state || "open").toLowerCase() + (r.checks ? " · " + (CHECKS[r.checks] || r.checks) : ""), how: r.match, url: r.url});
  const ci = d ? d.ci || [] : ciLatest().filter(w => bn.has(w.branch) && p && repoName(w.repo).toLowerCase() === repoName(p.github || p.id).toLowerCase()).map(w => Object.assign({}, w, {match: "a watched pipeline of a linked branch"}));
  for (const w of ci) { const rn = (w.runs || [])[0] || {};
    out.push({kind: "ci", k: "CI", icon: "pulse", text: (rn.name ? rn.name + " #" + rn.number + " " : "") + CIST[ciState(w)][1].toLowerCase() + (w.class && w.state === "failed" ? " · " + ciClass(w) : "") + (w.minutes != null ? " · " + (+w.minutes).toFixed(1) + "m" : "") + " · " + rel(w.ts), how: w.match, open: ["ci", ciKey(w)]}); }
  if (d && (d.commits || []).length) out.push({kind: "commits", k: "Commits", icon: "session", text: d.commits.length + (d.commits_complete === false ? " (the newest 100)" : "") + " · newest: " + d.commits[0].subject, how: d.commits[0].match,
    list: d.commits.map(c => String(c.sha || "").slice(0, 7) + " · " + c.author + " · " + rel(c.ts) + " · " + c.subject)});
  const sess = d ? d.sessions || [] : (D.session_log || []).filter(s => String(s.text || "").includes(x.id));
  if (sess.length) out.push({kind: "sessions", k: "Sessions", icon: "session", text: plural(sess.length, "session") + " name it" + (d && d.sessions_complete === false ? " (the newest 200)" : "") + " · latest " + rel(sess[0].ts) + (sess[0].who ? " by " + sess[0].who : ""), how: "these sessions name it",
    list: sess.map(s => fmt(s.ts) + " · " + [s.who, s.agent, s.branch].filter(Boolean).join(" · ") + " · " + plain(stripMarker(s.text)))});
  const plans = d ? d.plans || [] : FILES.filter(f => f.path.startsWith("plans/") && String(f.text || "").includes(x.id)).map(f => ({path: f.path, title: (/^# (.+)$/m.exec(f.text || "") || [])[1] || f.path}));
  for (const pl of plans) out.push({kind: "plan", k: "Plan", icon: "doc", text: pl.title, how: "the plan names this task", href: FILEMAP.has(pl.path) || LIVE.on ? link({view: "explorer", f: pl.path, id: ""}) : null});
  const links = d ? d.links || [] : (x.links || []).map(v => { const s = String(v), i = s.indexOf(":"); return {kind: i > 0 ? s.slice(0, i) : "link", target: i > 0 ? s.slice(i + 1) : s, text: s}; });
  const lessons = links.filter(l => l.kind === "lesson" || /^L-/.test(l.target));
  for (const l of lessons) out.push({kind: "lesson", k: "Lesson", icon: "book", text: l.target, how: "linked in the task"});
  if (!lessons.length) for (const id of (String(x.title).match(/\bL-[0-9a-z]{4,}\b/g) || [])) out.push({kind: "lesson", k: "Lesson", icon: "book", text: id, how: "the title names it"});
  for (const l of links.filter(l => !lessons.includes(l))) out.push({kind: "link", k: l.kind === "initiative" ? "Initiative" : "Link", icon: "doc", text: l.kind === "initiative" && BYINIT.has(l.target) ? BYINIT.get(l.target).title : l.text, how: "linked in the task",
    open: l.kind === "initiative" && BYINIT.has(l.target) ? ["initiative", l.target] : null});
  return out;
}
function connRow(c) {
  const name = c.k + ": " + c.text, val = c.open ? modalLink(c.open[0], c.open[1], c.text) : c.url ? h("a", {href: c.url, target: "_blank", rel: "noopener noreferrer", text: c.text}) : c.href ? h("a", {href: c.href, text: c.text}) : h("span", {text: c.text});
  if (val.tagName === "A") val.setAttribute("aria-label", name);
  return h("li", {class: "conn"}, icon(c.icon), h("span", {class: "k", text: c.k}), h("span", {class: "v"}, val),
    h("span", {class: "how", text: "Found: " + sentence(c.how)}), c.xs ? h("span", {class: "xs", text: c.xs}) : null,
    c.list ? h("details", {class: "xs recent", "data-key": "conn~" + c.kind}, h("summary", null, "Show " + plural(c.list.length, c.kind === "commits" ? "commit" : "session")), h("ul", {class: "rrows"}, c.list.map(t => h("li", {text: t})))) : null);
}
function taskModal(id) {
  const it = ITEMBY.get(id), det = it && it.legacy ? {state: "static"} : detail("task", id);
  if (!it && det.state !== "ok" && det.state !== "loading") return drawerProblem("task", id, det.state === "static" ? {state: "missing"} : det, null);
  const d = det.state === "ok" ? det.data : null;
  const x = Object.assign({id, title: id, scope: "", status: "todo", bucket: "next", priority: "normal", version: 1, changelog: [], links: [], body: "", claimed_by: "", holder: "", agent: "", open: true}, it || {});
  if (d) { const m = d.meta || {}, lk = d.lock || {};
    Object.assign(x, {title: d.title, scope: d.scope, status: d.status, type: d.type, body: d.body, changelog: d.changelog || [], path: d.path, open: OPEN.includes(d.status), claimed_by: lk.holder || "", holder: whoOf(lk.holder),
      agent: lk.agent || "", lock: lk.state || "", lock_expires: lk.expires || "", bucket: m.bucket || x.bucket, priority: m.priority || x.priority, due: m.due || x.due || "", version: m.version || x.version,
      created: m.created || x.created, created_by: m.created_by || x.created_by, updated: m.updated || x.updated, updated_by: m.updated_by || x.updated_by, claimed_at: m.claimed_at || x.claimed_at, assignee: m.assignee || x.assignee}); }
  const ini = isInit(x.scope), scopeHref = ini ? link({view: "initiatives", id: x.scope.slice(12), tab: "tasks"}) : link({view: "projects", id: x.scope, tab: "tasks"});
  const prob = drawerProblem("task", id, det, [[scopeName(x.scope), scopeHref], [id]]); if (prob) return prob;
  const head = [crumbs([[scopeName(x.scope), scopeHref], [x.legacy ? "Note task" : x.id]]), mtitle(x.title),
    h("div", {class: "pline"}, meta(istatusBadge(x.status), (x.bucket || "next").replace(/^./, c => c.toUpperCase()), typeTag(x.type), prioBadge(x.priority), duePill(x.due, !x.open), !x.legacy ? "version " + x.version : null, updatedNote(det)))];
  if (det.state === "loading") return {head, body: [skeleton()], busy: true};
  const cmds = taskCmds(x), primary = x.open && !x.legacy && cmds.length ? cmds[0] : null, conns = x.legacy ? [] : connections(x, d), runs = x.legacy ? [] : taskRuns(x, d);
  const run = runs.find(r => r.state !== "done") || null, rstep = run ? (run.plan || run.steps || []).find(s => String(s.name || "").includes(x.id)) : null, exp = x.lock === "expired";
  const whoHas = [];
  if (x.status === "done" || x.status === "cancelled") whoHas.push((x.status === "done" ? "Done" : "Cancelled") + " by " + (x.updated_by || "someone") + " " + rel(iso(x.updated)) + ".");
  else if (x.claimed_by && !exp) whoHas.push((isMe(x.holder) ? "You (" + x.holder + ")" : x.holder) + (x.agent ? " with " + agentName(x.agent) : "") + (x.claimed_at ? ", since " + clock(iso(x.claimed_at)) : "") + "." + (x.lock_expires ? " The lock ends in " + dur(iso(x.lock_expires) - nowS()) + "." : ""));
  else if (x.claimed_by) whoHas.push("The lock expired " + rel(iso(x.lock_expires)) + "; " + x.holder + " still holds it. Anyone can release it.");
  if (run) whoHas.push((rstep ? stepWho(rstep) : roleName(mainMember())) + " works on it in “" + (run.goal || run.id) + "”" + (rstep ? " (step “" + stepTitle(rstep) + "”)" : "") + ".");
  if (!whoHas.length) whoHas.push(x.open ? (x.legacy ? "Nobody: a checkbox in the note has no lock. The next dev sync imports it." : "Nobody. Start it to take the lock.") : "Nobody.");
  const blocks = [];
  if (x.status === "blocked") { const e = (x.changelog || []).map(parseLog).find(z => /block/i.test(z.op) || /blocked/i.test(z.detail)); blocks.push("Blocked" + (e && e.why ? ": " + e.why.replace(/[.\s]+$/, "") : "") + "."); }
  if (run) for (const z of pending(run)) blocks.push("Waiting for your answer to " + z.id + " in “" + (run.goal || run.id) + "”.");
  for (const c of conns.filter(c => c.kind === "pr" && / CI failing/.test(c.text))) { const n = (/#(\d+)/.exec(c.text) || [])[1], w = ciLatest().find(z => z.state === "failed" && (d ? d.prs || [] : []).some(r => r.number == n && r.branch === z.branch));
    blocks.push("Pull request #" + n + " is failing CI" + (w ? " (" + ciClass(w) + ")" : "") + "."); }
  if (rstep) for (const a of rstep.after || []) { const o = (run.plan || run.steps || []).find(s => s.name === a); if (o && o.state !== "done" && o.state !== "skipped") blocks.push("It waits for step “" + stepTitle(o) + "”, which is " + (o.state === "unverified" ? "waiting for " + roleName(mainMember()) + " to check it" : stepWord(o.state)[1].toLowerCase()) + "."); }
  if (!blocks.length) blocks.push("Nothing recorded.");
  const nextStep = run ? (run.plan || run.steps || []).find(s => s.state === "running" || s.state === "pending") : null;
  const nextText = run && run.next ? sentence(run.next) : nextStep ? "The run's next step: “" + nextStep.name + "”." : primary ? sentence(primary[0]) : x.open ? "Nothing planned." : "Nothing: it is " + (x.status === "done" ? "done." : x.status + ".");
  const found = new Set(conns.map(c => c.kind)), missing = [["run", "a run"], ["branch", "a branch"], ["pr", "a pull request"], ["plan", "a plan"]].filter(([k]) => !found.has(k)).map(z => z[1]);
  const notFound = missing.length ? "Not found: " + (missing.length > 1 ? missing.slice(0, -1).join(", ") + " or " + missing[missing.length - 1] : missing[0]) + "." : "";
  const log = (x.changelog || []);
  const body = [
    h("dl", {class: "ans"}, h("dt", {text: "Who has it"}), h("dd", null, whoHas.map(t => h("span", {text: t}))),
      h("dt", {text: "What blocks it"}), h("dd", {class: blocks[0] === "Nothing recorded." ? "t2" : null}, blocks.map(t => h("span", {text: t}))),
      h("dt", {text: "What's next"}), h("dd", null, h("span", {text: nextText}), primary ? h("div", {class: "nextcmd"}, h("span", {class: "label", text: primary[0]}), h("div", {style: "display:flex;gap:8px;align-items:center;min-width:0"}, cmdCode(primary[1]), icopy(primary[1], "Copy " + primary[0].toLowerCase()))) : null)),
    itemFlags(x, x.body).map(flagCallout),
    det.state === "static" && !x.legacy ? h("div", {class: "notice"}, "A static copy from " + fmt(D.generated) + ": it shows the first 4,000 characters and the last 60 history entries. Run ", h("code", {text: "dev dashboard"}), " for the whole task.") : null,
    x.legacy ? callout("info", "A checkbox in the handoff note", "It has no id, lock or history yet. The next dev sync imports it as a work item.") : msec("Connected to", conns.length ? [h("ul", {class: "conns"}, conns.map(connRow)), notFound ? h("p", {class: "tnote", text: notFound}) : null]
      : h("p", {class: "t2", style: "margin:0;font-size:var(--fs-1)", text: "Nothing connected yet. A run, branch, pull request or plan shows up here once it names " + x.id + "."})),
    msec("Description", x.body ? md(x.body) : h("p", {class: "t2", style: "margin:0;font-size:var(--fs-1)"}, "No description yet. It lives in ", h("code", {text: x.path || "the task file"}), ".")),
    !x.legacy ? msec("History", log.length ? [changelog(SHOWN["hist~" + id] ? log : log.slice(0, 20)), log.length > 20 && !SHOWN["hist~" + id] ? h("button", {class: "btn sm", type: "button", onclick: () => { SHOWN["hist~" + id] = 1; rerenderModal(); }}, "Show all " + log.length + " entries") : null] : empty("No history yet."), h("span", {class: "count num", text: String(log.length)})) : null,
    cmds.length > (primary ? 1 : 0) ? msec("Commands", [h("div", {class: "cmds"}, (primary ? cmds.slice(1) : cmds).map(([label, c]) => cmdRow(label, c))), h("p", {class: "tnote"}, "Changes of scope need ", h("code", {text: '--why "…"'}), ": the reason is kept in the history.")]) : null,
    !x.legacy ? msec("Details", h("dl", {class: "kv"}, h("dt", {text: "Id"}), h("dd", {class: "num", text: x.id}), h("dt", {text: ini ? "Initiative" : "Project"}), h("dd", null, scopeChip(x.scope)),
      x.assignee ? [h("dt", {text: "Assignee"}), h("dd", {text: x.assignee})] : null,
      h("dt", {text: "Created"}), h("dd", {text: [x.created ? fmt(iso(x.created)) : "", x.created_by ? "by " + x.created_by : ""].filter(Boolean).join(" ") || "—"}),
      h("dt", {text: "Updated"}), h("dd", {text: [x.updated ? fmt(iso(x.updated)) + " (" + rel(iso(x.updated)) + ")" : "", x.updated_by ? "by " + x.updated_by : ""].filter(Boolean).join(" ") || "—"}),
      x.path ? [h("dt", {text: "File"}), h("dd", null, h("code", {style: "word-break:break-all", text: x.path}), " ", icopy(D.handoff + "/" + x.path, "Copy path"))] : null)) : null];
  return {head, body, foot: [h("a", {class: "btn", href: scopeHref}, ini ? "Open initiative" : "Open project"), primary ? cmdBtn(primary[1], true, "Copy: " + primary[0]) : null]};
}
function initModal(slug) {
  const x = BYINIT.get(slug); if (!x) return null;
  const st = istate(x), c = x.cnt, mc = tally(x.members.map(memberState));
  return {head: [h("div", {class: "mh-row"}, progRing(x, 64, true), h("div", {class: "mh-tt"}, mtitle(x.title),
      h("div", {class: "pline"}, meta(h("span", {class: "stat"}, h("span", {class: "dot k-" + st, "aria-hidden": "true"}), HS[st]), duePill(x.due, x.status === "done", "No due date"), x.owner ? "owner " + x.owner : null))))],
    body: [msec("Member projects", [memberWaffle(x, 12, 30, true), legend(hparts(mc))], h("span", {class: "count num", text: String(x.members.length)})),
      msec("Tasks", [segBar([["mark", c.doing, "Doing"], ["risk", c.blocked, "Blocked"], ["none", c.todo, "To do"]], "Open tasks"), legend([["mark", c.doing, "Doing"], ["risk", c.blocked, "Blocked"], ["none", c.todo, "To do"], ["done", c.done, "Done"]])], h("span", {class: "count num", text: c.open + " open"})),
      x.state ? msec("Current state", h("p", {class: "lead clamp3", title: plain(x.state)}, inline(plain(x.state)))) : null,
      h("dl", {class: "kv"}, x.milestone ? [h("dt", {text: "Milestone"}), h("dd", {text: x.milestone})] : null, x.goal ? [h("dt", {text: "Goal"}), h("dd", {class: "clamp3", title: plain(x.goal), text: plain(x.goal)})] : null,
        h("dt", {text: "People"}), h("dd", null, x.people.length ? avatars(x.people, 6) : h("span", {class: "t2", text: "Nobody right now"})))],
    foot: [cmdBtn("dev initiative show " + slug, false, "Copy show command"), h("a", {class: "btn primary", href: link({view: "initiatives", id: slug})}, "Open full page")]};
}
function projectModal(id) {
  const p = BYID.get(id); if (!p) return null;
  const st = hstate(p), doing = sortItems(p.items.filter(i => i.status === "doing" || i.status === "blocked")), m = p.main || {};
  return {head: [h("div", {class: "mh-row"}, progRing(p, 64, true), h("div", {class: "mh-tt"}, mtitle(p.name || p.id),
      h("div", {class: "pline"}, meta(h("span", {class: "stat"}, h("span", {class: "dot k-" + st, "aria-hidden": "true"}), HS[st]), p.owner, m.branch ? h("span", {class: "meta"}, icon("branch"), m.branch) : null, p.local ? null : "not cloned here"))))],
    body: [p.health.reasons.length ? h("ul", {class: "reasons"}, p.health.reasons.map(r => h("li", null, h("span", {class: "dot k-" + (p.health.level === "risk" ? "risk" : "warn"), "aria-hidden": "true"}), r))) : null,
      HWHY[st] ? h("p", {class: "t2", style: "margin:0;font-size:var(--fs-1)", text: HWHY[st] + "."}) : null, projFlags(p).map(flagCallout),
      h("div", {class: "mfigs"},
        h("div", null, h("span", {class: "s-l", text: "Open PRs: " + p.prs}), ciStrip(p, 16)),
        h("div", null, h("span", {class: "s-l", text: "Commits, 14 days"}), spark(commitSeries([p], 14), "Commits per day", "commit", true)),
        h("div", null, h("span", {class: "s-l", text: "Tasks"}), segBar([["mark", p.cnt.doing, "Doing"], ["risk", p.cnt.blocked, "Blocked"], ["none", p.cnt.todo, "To do"]], "Open tasks"), h("span", {class: "t2 num", text: p.cnt.open + " open"}))),
      doing.length ? msec("Doing or blocked", h("ul", {class: "irows"}, doing.slice(0, 5).map(i => itemRow(i))), h("span", {class: "count num", text: String(doing.length)})) : null,
      p.status_line ? msec("Latest status", statusCard(p.status_line.line, p.status_line.parts, [p.status_line.agent || "agent", rel(p.status_line.ts)].join(", "))) : null],
    foot: [h("a", {class: "btn", href: link({view: "projects", id})}, "Open full page"), p.local ? cmdBtn("dev claude " + id, true, "Copy dev claude") : cmdBtn("dev clone " + id, true, "Copy dev clone")]};
}
function personModal(who) {
  const x = BYWHO.get(who); if (!x) return null;
  const claimed = x.claimed.map(id => ITEMBY.get(id)).filter(Boolean), exp = claimed.find(i => i.lock === "expired");
  return {head: [h("div", {class: "mh-row"}, avatar(who, true), h("div", {class: "mh-tt"}, mtitle(who), h("div", {class: "pline", text: (x.sessions.length ? plural(x.sessions.length, "live session") : "No live session") + (x.updated ? ", seen " + rel(x.updated) : "")})))],
    body: [x.sessions.length ? msec("Live sessions", h("div", {class: "sbars"}, x.sessions.map((s, i) => sessBar(s, who, i)))) : null,
      claimed.length ? msec("Claimed tasks", h("ul", {class: "lrows"}, claimed.map(it => h("li", {class: "lrow", onclick: rowClick(() => openTask(it.id))}, lockRing(it.claimed_at, it.lock_expires),
        h("div", {class: "lm"}, h("a", {class: "lt rl", href: taskHref(it.id), "data-nav": "", onclick: e => { e.preventDefault(); openTask(it.id); }, text: it.title}), h("div", {class: "ls ell", text: scopeName(it.scope)}))))), h("span", {class: "count num", text: String(claimed.length)})) : null,
      x.recent.length ? msec("Recent sessions", h("ul", {class: "rrows"}, x.recent.slice(0, 5).map(r => h("li", null, h("div", {class: "slw"}, meta(fmt(r.ts), scopeChip(r.scope), r.agent)), h("div", {class: "ell", title: plain(stripMarker(r.text))}, logText(r)))))) : null],
    foot: [h("a", {class: "btn", href: link({view: "team"})}, "Open Team"), exp ? cmdBtn("dev task " + flagOf(exp.scope) + " release " + exp.id + ' --why "lock expired"', true, "Copy release command") : null]};
}
function sessionModal(key) {
  const j = key.lastIndexOf("~"), who = key.slice(0, j), x = BYWHO.get(who), s = x && x.sessions[+key.slice(j + 1)];
  if (!s) return null;
  const known = s.project && (BYID.has(s.project) || MEMBEROF.has(s.project));
  return {head: [mtitle(scopeName(s.project || "") || "Session"), h("div", {class: "pline"}, meta(who, s.agent || "agent", s.branch ? h("span", {class: "meta"}, icon("branch"), s.branch) : null))],
    body: [sessionBlock(s, null, x.updated, true)],
    foot: [h("a", {class: "btn", href: modalHref("person", who)}, "More from " + who.split("@")[0]), known ? h("a", {class: "btn primary", href: link({view: "projects", id: s.project})}, "Open project") : null]};
}
function actKey(a) { let hh = 5381; const s = a.title + "|" + (a.project || "") + "|" + a.cmd; for (let i = 0; i < s.length; i++) hh = ((hh << 5) + hh + s.charCodeAt(i)) | 0; return a.kind + "~" + (hh >>> 0).toString(36); }
function actionModal(key) {
  const a = needs().find(x => actKey(x) === key); if (!a) return null;
  const p = a.project && BYID.get(a.project), href = actionHref(a);
  return {head: [mtitle(a.title), h("div", {class: "pline"}, meta(h("span", {class: "stat"}, h("span", {class: "dot k-" + (a.sev === "risk" ? "risk" : a.sev === "warn" ? "warn" : "done"), "aria-hidden": "true"}), SEVL[a.sev]), KIND[a.kind] || a.kind, p ? p.name || p.id : null))],
    body: [a.detail ? h("p", {class: "lead"}, a.modal || a.view ? a.detail : inline(a.detail)) : null, p && /ci-failing|conflicts|review/.test(a.kind) ? msec("Open PRs of " + (p.name || p.id), ciStrip(p, 20)) : null,
      a.cmd ? h("div", {class: "nextcmd"}, h("span", {class: "label", text: "The command that moves it forward"}), cmdCode(a.cmd)) : null],
    foot: [a.url ? h("a", {class: "btn", href: a.url, target: "_blank", rel: "noopener noreferrer"}, "Open on " + (a.kind === "ci-watch" ? "the provider" : "GitHub")) : null, href ? h("a", {class: "btn" + (a.cmd ? "" : " primary"), href}, a.go || (p ? "Open " + (p.name || p.id) : "Open")) : null, a.cmd ? cmdBtn(a.cmd, true, "Copy command") : null]};
}
function actionsModal(label) {
  const list = needs().filter(a => (KIND[a.kind] || a.kind) === label); if (!list.length) return null;
  return {head: [mtitle((GROUP[list[0].kind] || (n => n + " " + label + " items"))(list.length)), h("div", {class: "pline", text: SEVL[list[0].sev]})],
    body: [h("ul", {class: "acts"}, list.map(a => { const tgt = a.modal || ["action", actKey(a)];
      return h("li", {class: "act", onclick: rowClick(() => openModal(tgt[0], tgt[1]))}, h("span", {class: "dot d-" + a.sev, "aria-hidden": "true"}),
        h("div", {style: "min-width:0"}, modalLink(tgt[0], tgt[1], a.title, "at"), h("div", {class: "ad"}, h("span", {class: "ell", text: actionWhere(a) + (a.detail ? ": " + plain(a.detail) : "")}))), h("div", {class: "ar"}, a.cmd ? icopy(a.cmd, "Copy command") : null)); }))],
    foot: [h("a", {class: "btn", href: link({view: "now", all: true, kind: label})}, "Open the full list"), list[0].cmd ? cmdBtn(list[0].cmd, true, "Copy the first command") : null]};
}
function sinceModal(which) {
  const s = D.since || {}, T = {sessions: "Sessions since yesterday", commits: "Commits since yesterday", merged: "Merged PRs since yesterday", done: "Tasks completed since yesterday"};
  if (!T[which]) return null;
  const list = which === "sessions" ? h("ul", {class: "rrows"}, (s.sessions || []).map(x => h("li", null, h("div", {class: "slw"}, meta(fmt(x.ts), x.who, x.agent, h("button", {class: "lnk", type: "button", onclick: () => { closeModal(); x.project && openScope(x.project); }, text: scopeName(x.project || "") || "—"}))), h("div", {class: "ell", title: plain(stripMarker(x.text))}, logText(x)))))
    : which === "commits" ? rankedBars((s.commits || []).map(([pid, n]) => [scopeName(pid), n, pid]), v => String(v), pid => { closeModal(); openProject(pid); })
    : which === "merged" ? h("ul", {class: "rrows"}, (s.merged || []).map(x => h("li", {class: "ell"}, h("a", {href: x.url, target: "_blank", rel: "noopener noreferrer", text: "#" + x.number}), " ", x.title, h("span", {class: "t2", text: " in " + x.repo.split("/").pop()}))))
    : h("ul", {class: "rrows"}, (s.done || []).map(x => h("li", {class: "ell"}, x.id && ITEMBY.has(x.id) ? modalLinkTask(x.id, x.text) : inline(x.text), h("span", {class: "t2", text: " in " + scopeName(x.project) + (x.who ? ", by " + x.who : "")}))));
  return {head: [mtitle(T[which]), h("div", {class: "pline", text: "from " + fmtDay(s.from)})], body: [list], foot: [h("a", {class: "btn", href: link({view: "activity"})}, "Open Activity")]};
}
function modalLinkTask(id, text) { return h("a", {class: "rl", href: taskHref(id), onclick: e => { e.preventDefault(); openTask(id); }, text}); }
const MODALS = {task: taskModal, initiative: initModal, project: projectModal, person: personModal, session: sessionModal, action: actionModal, actions: actionsModal, since: sinceModal,
  run: runModal, job: jobModal, ci: ciModal, member: memberModal};
/* ---------------------------------------------------------------- command palette + sheet */
function fuzzy(q, s) { const toks = String(q || "").toLowerCase().split(/\s+/).filter(Boolean); if (!toks.length) return 1; let total = 0; s = s.toLowerCase(); for (const t of toks) { const sc = fuzzy1(t, s); if (!sc) return 0; total += sc; } return total; }
function fuzzy1(q, s) { const i = s.indexOf(q); if (i >= 0) return 100 - Math.min(i, 80) + (i === 0 ? 20 : 0); let qi = 0, score = 0, prev = -2; for (let j = 0; j < s.length && qi < q.length; j++) if (s[j] === q[qi]) { score += prev === j - 1 ? 3 : 1; prev = j; qi++; } return qi === q.length ? score : 0; }
function paletteItems() {
  const items = [];
  for (const [v, label, key] of VIEWS) items.push({grp: "View", label: "Go to " + label, hint: "g " + key, run: () => go(v)});
  items.push({grp: "View", label: "Everything that needs you", hint: needs().length + " items", run: () => go("now", {all: true})});
  for (const a of needs().filter(x => x.cmd)) items.push({grp: "Action", label: "Copy: " + a.cmd, hint: a.title, run: () => copy(a.cmd)});
  items.push({grp: "Setting", label: "Toggle theme (light/dark)", run: toggleTheme}, {grp: "Setting", label: "Theme: follow system", run: () => { try { localStorage.removeItem("devkit.theme"); } catch (e) {} delete document.documentElement.dataset.theme; }},
    {grp: "Setting", label: "Toggle dense mode", run: () => set({dense: !S.dense}, true)}, {grp: "Setting", label: "Clear search and filters", run: () => set(Object.assign({q: ""}, CLEAR), true)},
    {grp: "Help", label: "Keyboard shortcuts", hint: "?", run: openSheet}, {grp: "Help", label: "Print this view", run: () => window.print()});
  for (const x of INITS) { items.push({grp: "Initiative", label: x.title, hint: (progOf(x).pct != null ? progOf(x).pct + "% · " : "") + x.status + " · " + plural(x.members.length, "project"), run: () => openInitiative(x.id)}); items.push({grp: "Command", label: "Copy: dev initiative show " + x.id, run: () => copy("dev initiative show " + x.id)}); }
  for (const x of PEOPLE) items.push({grp: "Person", label: x.who, hint: x.sessions.length ? plural(x.sessions.length, "live session") : "idle", run: () => go("team")});
  for (const p of P) {
    items.push({grp: "Project", label: p.id, hint: p.health.level === "ok" ? p.status : p.health.reasons[0], run: () => openProject(p.id)});
    for (const c of p.local ? ["dcd ", "dev claude ", "dev codex ", "dev handoff "] : ["dev clone "]) items.push({grp: "Command", label: "Copy: " + c + p.id, run: () => copy(c + p.id)});
    for (const r of p.prsOpen) items.push({grp: "PR", label: "#" + r.number + " " + (r.title || ""), hint: (CHECKS[r.checks] || "") + " · " + p.name, run: () => openUrl(r.url)});
  }
  for (const it of allItems().filter(i => i.open).slice(0, 1500)) items.push({grp: "Task", label: (it.legacy ? "" : it.id + " ") + it.title, hint: scopeName(it.scope) + (it.holder ? " · " + it.holder : ""), run: () => openTask(it.id)});
  for (const f of FILES) items.push({grp: "File", label: f.path, run: () => set({view: "explorer", id: "", f: f.path, fq: "", task: ""}, true)});
  return items;
}
function openPalette() {
  const pal = $("#palette"); if (!pal.hidden) return; if ($("#modal").open) closeModal(); lastFocus = document.activeElement;
  const all = paletteItems(); let sel = 0, shown = [];
  const input = h("input", {type: "text", placeholder: "Jump to a project, PR, task, file or view — or copy a command", "aria-label": "Command", "aria-controls": "pal-list", role: "combobox", "aria-expanded": "true", autocomplete: "off"});
  const list = h("ul", {id: "pal-list", role: "listbox"});
  clear(pal).append(input, list, h("div", {class: "ft"}, h("span", null, h("kbd", {text: "↑↓"}), " move"), h("span", null, h("kbd", {text: "↵"}), " run"), h("span", null, h("kbd", {text: "esc"}), " close")));
  pal.hidden = false; $("#scrim").hidden = false; $("#scrim").classList.add("on");
  function draw() {
    const q = input.value.trim();
    shown = all.map(it => [fuzzy(q, it.label + " " + (it.hint || "")), it]).filter(x => x[0] > 0).sort((a, b) => b[0] - a[0]).slice(0, 60).map(x => x[1]);
    sel = Math.min(sel, Math.max(0, shown.length - 1)); clear(list);
    shown.forEach((it, i) => list.append(h("li", {role: "option", id: "pal-" + i, "aria-selected": String(i === sel), onclick: () => run(i), onmousemove: () => { if (sel !== i) { sel = i; paint(); } }}, h("span", {class: "ell"}, inline(it.label)), it.hint ? h("span", {class: "t2 ell", style: "max-width:40%", text: it.hint}) : null, h("span", {class: "grp", text: it.grp}))));
    if (!shown.length) list.append(h("li", {class: "t2", text: "No match"}));
    paint();
  }
  function paint() { $$("li[role=option]", list).forEach((li, i) => li.setAttribute("aria-selected", String(i === sel))); input.setAttribute("aria-activedescendant", "pal-" + sel); const cur = $("#pal-" + sel); if (cur) cur.scrollIntoView({block: "nearest"}); }
  function run(i) { const it = shown[i]; closePalette(); if (it) it.run(); }
  input.addEventListener("input", () => { sel = 0; draw(); });
  input.addEventListener("keydown", e => { if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(shown.length - 1, sel + 1); paint(); } else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); paint(); } else if (e.key === "Enter") { e.preventDefault(); run(sel); } });
  draw(); input.focus();
}
function closePalette() { const pal = $("#palette"); if (pal.hidden) return; pal.hidden = true; if (!S.task) { $("#scrim").classList.remove("on"); $("#scrim").hidden = true; } if (lastFocus && lastFocus.focus) lastFocus.focus(); }
function openSheet() {
  const sh = $("#sheet"); const rows = [["/", "Search everything"], ["⌘K · Ctrl K", "Command palette"], ["g n · t · b · m", "Now · Tasks · Background work · Team"], ["g c", "Team: the crew"], ["g p · i", "Projects · Initiatives"], ["g a · e · h", "Activity · Explorer · Health"], ["u", "Up: from a page back to its list"],
    ["← →", "Switch tabs (tab bar focused) · read the chart day by day"], ["j / k", "Next / previous item"], ["↵", "Open the focused item"], ["[ · ]", "In a drawer: the previous or next record of its list"], ["Esc", "Close the drawer or palette, clear search"], ["?", "This sheet"]];
  clear(sh).append(h("h2", {text: "Keyboard shortcuts"}), h("dl", null, rows.map(([k, v]) => [h("dt", null, h("kbd", {text: k})), h("dd", {style: "margin:0", text: v})])),
    h("p", {class: "t2", style: "margin:14px 0 0;font-size:var(--fs-1)", text: "The URL keeps the page, tab, task and filters — #/initiatives/<slug>, #/projects/<owner>/<repo>?tab=tasks&task=<id> — so you can bookmark or share any view. " + (LIVE.on ? "The live page updates in place as data changes." : "This static copy reloads itself every 2 minutes while visible.")}),
    h("div", {style: "text-align:right;margin-top:10px"}, h("button", {class: "btn", type: "button", onclick: closeSheet}, "Close")));
  sh.hidden = false; $("#scrim").hidden = false; $("#scrim").classList.add("on"); $("button", sh).focus();
}
function closeSheet() { $("#sheet").hidden = true; if (!S.task && $("#palette").hidden) { $("#scrim").classList.remove("on"); $("#scrim").hidden = true; } }

/* ---------------------------------------------------------------- navigation, theme, keyboard */
function go(view, patch) { set(Object.assign({view, id: "", task: ""}, PAGEKEYS, patch || {}), true); window.scrollTo(0, 0); }
function openProject(id, tab) {
  if (!BYID.has(id) && !MEMBEROF.has(id)) { toast("No handoff note for " + id + " yet"); return; }
  set(Object.assign({}, PAGEKEYS, {view: "projects", id, tab: tab || ""}), true); window.scrollTo(0, 0);
}
function openInitiative(id, tab) { if (!BYINIT.has(id)) { toast("No initiative " + id); return; } set(Object.assign({}, PAGEKEYS, {view: "initiatives", id, tab: tab || ""}), true); window.scrollTo(0, 0); }
function openScope(scope, tab) { if (isInit(scope)) openInitiative(scope.slice(12), tab); else openProject(scope, tab); }
function openTask(id) { if (!ITEMBY.has(id)) { toast("No task " + id); return; } set({task: id}, true); }
function closeDrawer() { closeModal(); }
function toggleTheme() {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; store.set("devkit.theme", next);
  $("#theme").setAttribute("aria-label", "Switch to " + (next === "dark" ? "light" : "dark") + " theme"); render();
}
let gAt = 0;
document.addEventListener("keydown", e => {
  const tag = (e.target && e.target.tagName) || "", typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (e.target && e.target.isContentEditable);
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); if ($("#palette").hidden) openPalette(); else closePalette(); return; }
  if (e.key === "Escape") {
    if (!$("#palette").hidden) { closePalette(); return; } if (!$("#sheet").hidden) { closeSheet(); return; } if (S.task || S.modal) { closeModal(); return; }
    if (e.target.id === "q" && S.q) { set({q: "", view: S.view === "search" ? store.get("devkit.prevView", "now") : S.view}, true); return; }
    if (typing) e.target.blur(); return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") { e.preventDefault(); $("#q").focus(); $("#q").select(); return; }
  if (e.key === "?") { e.preventDefault(); openSheet(); return; }
  if (gAt && Date.now() - gAt < 1200) { gAt = 0; const k = e.key.toLowerCase(), v = VIEWS.find(x => x[2] === k) || (k === "o" ? VIEWS[0] : null);
    if (k === "c") { e.preventDefault(); go("team", {tab: "crew"}); } else if (v) { e.preventDefault(); go(v[0]); } return; }
  if ((e.key === "[" || e.key === "]") && $("#modal").open) { e.preventDefault(); stepRecord(e.key === "]" ? 1 : -1); return; }
  if (e.key === "g") { gAt = Date.now(); return; }
  if (e.key === "u" && !S.task && !S.modal) { if (S.id) { e.preventDefault(); go(S.view); } else if (S.all) set({all: false, kind: ""}, true); return; }
  if (e.key === "j" || e.key === "k") {
    e.preventDefault(); const scope = $("#modal").open ? $("#modal") : $("#view");
    const items = $$("[data-nav]", scope).filter(x => x.offsetParent !== null); if (!items.length) return;
    const i = items.indexOf(document.activeElement); const n = items[i < 0 ? 0 : Math.max(0, Math.min(items.length - 1, i + (e.key === "j" ? 1 : -1)))];
    n.focus(); n.scrollIntoView({block: "nearest"});
  }
});

/* ---------------------------------------------------------------- render + boot */
/* sticky headers need visible overflow; a table that does not fit its panel scrolls inside it instead */
function fitTables(root) { for (const w of $$(".tw.st", root || document)) { w.classList.remove("x"); if (w.scrollWidth > w.clientWidth + 1) w.classList.add("x"); } }
const VIEWFN = {initiatives: viewInitiatives, projects: viewProjects, team: viewTeam, tasks: viewTasks, activity: viewActivity, explorer: viewExplorer, health: viewHealthV3, search: viewSearch, background: viewBackground};
function pageTitle() {
  if (S.view === "initiatives" && S.id) return (BYINIT.get(S.id) || {}).title || S.id;
  if (S.view === "projects" && S.id) return (BYID.get(S.id) || {}).name || S.id;
  if (S.view === "background") return "Background work";
  if (S.view === "now" && S.all) return "Needs you";
  return (VIEWS.find(v => v[0] === S.view) || [0, "Search"])[1];
}
function render() {
  const fid = document.activeElement && document.activeElement.id && $("#view").contains(document.activeElement) ? document.activeElement.id : "";
  const page = S.view + "/" + S.id;
  document.body.classList.toggle("dense", !!S.dense);
  renderNav(); renderFilters();
  const main = clear($("#view")); main.className = "";
  (VIEWFN[S.view] || VIEWFN.now)(main);
  fitTables(main);
  RS = Object.assign({}, S);
  renderModal();
  if (fid) { const el = document.getElementById(fid); if (el && !$("#modal").open) el.focus({preventScroll: true}); }
  if (page !== lastView) { if (lastView) window.scrollTo(0, 0); lastView = page; document.title = pageTitle() + " · Firefly devkit"; }
  liveTicker();
}
/* ---------------------------------------------------------------- live mode (the page served by `dev dashboard`): the event stream says a newer
   snapshot exists and which areas changed; the page fetches only those, swaps the data, re-derives and re-renders in place, keeping the route,
   filters, scroll position and the open dialog. While the reader types or has a menu open, the swap waits. The static export has none of this. */
const LIVE = {on: !!D.live, state: D.live ? "init" : "static", pending: null, busy: false, again: null, quiet: false, retry: 0, at: 0, since: 0, dataAt: 0, failAt: 0, pop: false, newVersion: "", hiddenAt: 0};
const LIVE_GAP = 2000;   /* at most one in-place re-render every 2 s, however often agents write */
const PAGEVER = D.version || "";
const LIVEWORD = {init: "Connecting…", connecting: "Connecting…", live: "Live", reconnecting: "Reconnecting…", offline: "Offline", ended: "Session ended"};
const LIVETONE = {connecting: "none", live: "ok", reconnecting: "warn", offline: "risk", ended: "risk"};
const LIVENAME = {init: "connecting", connecting: "connecting", live: "connected", reconnecting: "reconnecting", offline: "offline", ended: "session ended"};
function liveTicker() {
  const t = $("#ticker"); if (!t) return;
  if (!LIVE.on) { t.textContent = "Synced " + rel(D.generated); return; }
  t.textContent = "Data from " + new Date(LIVE.dataAt || (D.rendered || D.generated) * 1000).toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  t.setAttribute("data-live", LIVE.state);
}
function setLive(state) { if (LIVE.state === "ended" && state !== "ended") return; const was = LIVE.state; LIVE.state = state; if (was !== state) { liveIndicator(); renderBanner(); liveTicker(); fitNav(); } }
/* the indicator: a button with a dot and one word; its popover says how current the data is */
function liveIndicator() {
  const b = $("#live"), pop = $("#livepop"); if (!b) return;
  const st = LIVE.state, stat = st === "static";
  b.setAttribute("data-state", st);
  const gen = new Date(D.generated * 1000).toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
  b.setAttribute("aria-label", stat ? "A static copy, generated " + gen : "Live updates: " + LIVENAME[st]);
  append(clear(b), [stat ? null : stateDot(LIVETONE[st] || "none"), stat ? [h("span", {class: "lgw", text: "Generated "}), gen] : LIVEWORD[st]]);
  b.setAttribute("aria-expanded", String(LIVE.pop));
  pop.hidden = !LIVE.pop;
  if (!LIVE.pop) return;
  const changed = LIVE.dataAt || (D.rendered || D.generated) * 1000;
  append(clear(pop), stat ? [h("strong", {text: "A static copy"}), h("p", null, "It does not update. Run ", h("code", {text: "dev dashboard"}), " for the live page.")]
    : [h("strong", {text: "Live updates"}),
      h("p", {text: (st === "live" && LIVE.since ? "Connected for " + fmtMin((Date.now() - LIVE.since) / 60000) + ". " : st === "live" ? "Connected. " : LIVEWORD[st] + " ") +
        "The data last changed at " + new Date(changed).toLocaleTimeString() + "."}),
      h("p", {text: "Pull requests and checks are read again every minute; providers and configuration every 10 minutes."}),
      h("p", {class: "t2", text: "Server " + location.host + " · devkit " + (D.version || "") + ". It stops after 30 minutes with no open page."})]);
}
function toggleLivePop(open) { LIVE.pop = open == null ? !LIVE.pop : open; liveIndicator(); }
/* the banner under the nav: offline, session ended, a new devkit version; a polite status region, never an alert */
function renderBanner() {
  const b = $("#banner"); if (!b) return; clear(b);
  const box = (tone, title, body, acts) => h("div", {class: "callout"}, stateDot(tone), h("div", {class: "cbody"}, h("strong", {text: title}), h("div", {text: body})), h("div", {class: "cact"}, acts));
  const reload = primary => h("button", {class: "btn" + (primary ? " primary" : ""), type: "button", onclick: () => location.reload()}, "Reload");
  let el = null;
  if (LIVE.state === "ended") el = box("risk", "This page is no longer signed in.", "The dashboard server restarted with a new key. Run dev dashboard to open a signed-in page.", [cmdBtn("dev dashboard")]);
  else if (LIVE.state === "offline") el = box("risk", "The dashboard server stopped.", "It stops by itself after 30 minutes with no open page. You are looking at data from " + new Date(LIVE.dataAt || (D.rendered || D.generated) * 1000).toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"}) + ".", [cmdBtn("dev dashboard"), reload(false)]);
  else if (LIVE.newVersion && LIVE.newVersion !== PAGEVER) el = box("ok", "devkit was updated to " + LIVE.newVersion + ".", "Reload to get the new page. This page changes nothing, so reloading is safe.", [reload(true)]);
  b.hidden = !el; if (el) b.append(el);
}
/* screen readers hear what matters, at most once every 10 s (batched): decisions, CI failures, failed jobs, finished or blocked runs, the stream */
const SAY = {q: [], at: 0, t: 0};
function announce(msg) {
  if (msg) SAY.q.push(msg);
  const wait = SAY.at + 10000 - Date.now(); clearTimeout(SAY.t);
  if (wait > 0) { SAY.t = setTimeout(() => announce(""), wait); return; }
  if (!SAY.q.length) return;
  const el = $("#live-status"); if (el) el.textContent = SAY.q.splice(0).join(" ");
  SAY.at = Date.now();
}
function newsOf(a, b) {
  const out = [], dec = new Set(), runs = new Map((a.runs || []).map(r => [r.id, r])), ci = new Map(((a.ci || {}).watches || []).map(w => [ciKey(w) + w.sha + w.ts, w])), jobs = new Map((a.jobs || []).map(j => [j.name, j]));
  for (const r of a.runs || []) for (const d of r.decisions || []) dec.add(r.id + "~" + d.id);
  for (const r of b.runs || []) { for (const d of r.decisions || []) if (!dec.has(r.id + "~" + d.id)) out.push("Decision waiting: " + d.question + ", asked by " + who(d.from_name || "main") + ".");
    const o = runs.get(r.id); if (o && o.state !== r.state && (r.state === "done" || r.state === "blocked")) out.push("Run " + (r.goal || r.id) + (r.state === "done" ? " finished." : " is blocked.")); }
  for (const w of (b.ci || {}).watches || []) if (w.state === "failed" && !ci.has(ciKey(w) + w.sha + w.ts)) out.push("CI failed on " + w.branch + ": " + ciClass(w, (w.failures || [])[0]) + ".");
  for (const j of b.jobs || []) { const o = jobs.get(j.name), f = jobFailure(j); if (j.state === "failed" && f.start && (!o || o.state !== "failed" || jobFailure(o).start !== f.start)) out.push("Job " + j.name + " failed."); }
  return out;
}
function userBusy() {
  const a = document.activeElement, tag = (a && a.tagName) || "";
  return !$("#palette").hidden || !!$(".facet[open],.cmenu[open]") || (/^(INPUT|SELECT|TEXTAREA)$/.test(tag) && a.id !== "q");
}
/* rows carry data-key (who they are) and data-sig (what they show): after a live update, rows whose signature changed flash once */
function rowSigs() { const m = new Map(); for (const el of $$("[data-key][data-sig]", $("#view"))) m.set(el.getAttribute("data-key"), el.getAttribute("data-sig")); return m; }
function applyLive() {
  if (!LIVE.pending) return;
  const wait = userBusy() ? 1000 : LIVE.at + LIVE_GAP - Date.now();
  if (wait > 0) { clearTimeout(LIVE.retry); LIVE.retry = setTimeout(applyLive, wait); return; }
  LIVE.at = Date.now(); LIVE.dataAt = Date.now();
  const old = D, before = rowSigs(), act = document.activeElement, row = act && act.closest ? act.closest("[data-key]") : null;
  const focusKey = row && $("#view").contains(row) ? row.getAttribute("data-key") : "", focusIdx = row ? $$("a,button,input,select,summary,[tabindex]", row).indexOf(act) : -1;
  const opened = new Set($$("details[data-key]", $("#view")).filter(d => d.open).map(d => d.getAttribute("data-key")));
  D = LIVE.pending; LIVE.pending = null; derive(); IDX = null;
  if (D.version && D.version !== PAGEVER) LIVE.newVersion = D.version;
  const dlg = $("#modal"), body = dlg && dlg.open ? $(".mbody", dlg) : null, y = window.scrollY, my = body ? body.scrollTop : 0, back = dlgFocus(dlg);
  LIVE.quiet = true;
  try { render(); } finally { LIVE.quiet = false; }
  back();
  for (const d of $$("details[data-key]", $("#view"))) if (opened.has(d.getAttribute("data-key"))) d.open = true;
  window.scrollTo(0, y);
  const nb = dlg && dlg.open ? $(".mbody", dlg) : null; if (nb) nb.scrollTop = my;
  if (focusKey && !$("#modal").open) { const r = $$("[data-key]", $("#view")).find(el => el.getAttribute("data-key") === focusKey);
    if (r) { const f = $$("a,button,input,select,summary,[tabindex]", r)[Math.max(0, focusIdx)] || r; if (f.focus) f.focus({preventScroll: true}); } }
  const changed = []; for (const [k, sig] of rowSigs()) if (before.has(k) && before.get(k) !== sig) changed.push(k);
  if (changed.length && changed.length <= 10) for (const el of $$("[data-key]", $("#view"))) if (changed.includes(el.getAttribute("data-key"))) { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
  for (const m of newsOf(old, D)) announce(m);
  renderBanner(); liveTicker(); if (LIVE.pop) liveIndicator();
}
async function liveGet(path) {
  const r = await fetch(path, {cache: "no-store", credentials: "same-origin", headers: {Accept: "application/json"}});
  if (r.status === 401) { setLive("ended"); throw new Error("signed out"); }
  if (!r.ok) throw new Error(path + " answered " + r.status);
  return r.json();
}
async function liveUpdate(changed) {
  if (LIVE.busy) { LIVE.again = (LIVE.again || []).concat(changed); return; }
  LIVE.busy = true;
  try {
    const all = changed.includes("*"), areas = changed.filter(k => k !== "files" && k !== "*");
    const next = Object.assign({}, LIVE.pending || D);
    if (all || areas.length) Object.assign(next, await liveGet("/api/v1/dashboard?" + (all ? "omit=files" : "only=" + areas.map(encodeURIComponent).join(","))));
    if (all || changed.includes("files")) { const f = await liveGet("/api/v1/files"); next.files = f.files || []; next.files_meta = f.meta || {}; }
    LIVE.pending = next; applyLive(); refreshDetail(changed);
  } catch (e) { /* the next event, or the reconnect, tries again */ }
  finally { LIVE.busy = false; if (LIVE.again) { const a = LIVE.again; LIVE.again = null; liveUpdate(a); } }
}
/* the server answers /health while it runs: a failure after 30 s of reconnecting means it stopped; a 401 means it restarted with a new key */
async function liveProbe() {
  try { const hh = await liveGet("/api/v1/health"); if (hh && hh.version && hh.version !== PAGEVER) { LIVE.newVersion = hh.version; renderBanner(); } }
  catch (e) { if (LIVE.state !== "ended" && LIVE.failAt && Date.now() - LIVE.failAt >= 30000) setLive("offline"); }
}
function liveStart() {
  if (!LIVE.on || !window.EventSource) return;
  let ver = D.snapshot || 0, es = null;
  setTimeout(() => { if (LIVE.state === "init") setLive("connecting"); }, 500);
  const stream = () => {
    es = new EventSource("/api/v1/events?since=" + ver);
    es.addEventListener("open", () => {
      const back = LIVE.state === "reconnecting" || LIVE.state === "offline";
      LIVE.failAt = 0; if (!LIVE.since || back) LIVE.since = Date.now();
      setLive("live");
      if (back) { announce("Live updates are back."); liveProbe(); }
    });
    es.addEventListener("error", () => {
      if (LIVE.state === "ended") { es.close(); return; }
      if (LIVE.state === "live") announce("Live updates lost. Retrying.");
      if (!LIVE.failAt) LIVE.failAt = Date.now();
      if (LIVE.state !== "offline") setLive("reconnecting");
      if (es.readyState === 2) { es.close(); liveProbe(); setTimeout(() => { if (LIVE.state !== "ended") stream(); }, 3000); }   /* the browser gave up: try again */
    });
    es.addEventListener("snapshot", e => {
      let m = null; try { m = JSON.parse(e.data); } catch (x) { return; }
      setLive("live");
      if (m && m.version > ver) { ver = m.version; liveUpdate(m.changed && m.changed.length ? m.changed : ["*"]); }
    });
  };
  stream();
  setInterval(() => { if ((LIVE.state === "reconnecting" || LIVE.state === "offline") && LIVE.failAt) liveProbe(); }, 5000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") { LIVE.hiddenAt = Date.now(); return; }
    if (LIVE.hiddenAt && Date.now() - LIVE.hiddenAt > 60000 && LIVE.state === "live") liveUpdate(["*"]);
    LIVE.hiddenAt = 0;
  });
  if (!D.files) liveUpdate(["files"]);   /* the first paint leaves the explorer's file contents out: they load now */
}
