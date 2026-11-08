/* ---------------------------------------------------------------- Now (#/now): what needs you, how the runs go and when they land
   UX spec P-fyka51gico5 3.1 and 4.4; data contract P-fyli4qqn0y7. The server ranks, words and times everything (lib/devkit/dashnow.py):
   this section only draws, and never re-sorts. It is a fragment inside the app's closure: it uses app.js helpers, its top-level names
   start with now, viewNow or NOW_, and it registers itself on its last line. */
const NOW_ROWS = 5;     /* queue rows before "All N" */
const NOW_RUNS = 4;     /* run blocks before "N more runs" */
const NOW_STRIP = 8;    /* step glyphs on one landing strip */
const NOW_TONE = {you: "Waits for you", alert: "Broken", caution: "At risk"};
const NOW_STALE = {attention: ["prs", "registry"], safety: ["registry"], since: ["registry", "prs"]};   /* an area's slow sources */
const NOW_HOUSEKEEPING = ["collision", "expired-lock", "idle-work", "reclaim", "stale-note", "drift", "layout"];
/* glyphs are drawn, one stroke weight; the word next to each one carries its meaning */
const NOW_GLYPH = {
  you: [["rect", {x: 3, y: 3, width: 10, height: 10, rx: 1.5}]],
  alert: [["path", {d: "M4.5 4.5l7 7M11.5 4.5l-7 7"}]],
  caution: [["path", {d: "M8 2.5 13.5 8 8 13.5 2.5 8z"}]],
  done: [["path", {d: "M3.5 8.5l3 3 6-7"}]],
  running: [["path", {d: "M5.5 4v8l6.5-4z"}]],
  pending: [["circle", {cx: 8, cy: 8, r: 4.5}]]
};
NOW_GLYPH.blocked = NOW_GLYPH.alert;
NOW_GLYPH.unverified = NOW_GLYPH.caution;
let nowSinceOpen = null;   /* the Since yesterday line is open on the day's first visit and before noon (decided once per page) */

function nowGlyph(kind, label) {
  return sv("svg", {class: "now-glyph " + kind, viewBox: "0 0 16 16", width: 16, height: 16, focusable: "false", role: label ? "img" : null,
    "aria-label": label || null, "aria-hidden": label ? null : "true"}, (NOW_GLYPH[kind] || []).map(([t, a]) => sv(t, a)));
}
function nowId(s) { return String(s || "").replace(/[^A-Za-z0-9_-]/g, "_"); }
function nowAge(ts) { return dur(Math.max(0, nowS() - ts)); }
/* durations: to the minute under an hour, to 5 minutes above (UX spec 3) */
function nowSpan(min) {
  min = Math.max(0, Math.round(min || 0)); if (min < 60) return min + " min";
  const r = Math.round(min / 5) * 5; return Math.floor(r / 60) + " h" + (r % 60 ? " " + String(r % 60).padStart(2, "0") : "");
}
function nowJoin(parts, sep) { const out = []; parts.forEach((p, i) => { if (i) out.push(sep); out.push(p); }); return out; }
function nowCopy(cmd, id) {
  return h("button", {class: "now-cmd", type: "button", id, title: cmd, "aria-label": "Copy: " + cmd, onclick: e => { e.stopPropagation(); copy(cmd); }},
    icon("copy"), h("code", {text: cmd}));
}
function nowError(e) { return h("p", {class: "now-error"}, nowGlyph("alert", "Failed"), h("span", null, h("strong", {text: e.title || "This part could not be read"}), " ", e.detail || "")); }
/* an area is stale at twice the refresh of its oldest slow source (contract, as_of) */
function nowStale(area) {
  const ao = D.as_of || {}; let old = null;
  for (const s of NOW_STALE[area] || []) { const x = ao[s]; if (x && x.at && x.every_s && nowS() - x.at > 2 * x.every_s && (!old || x.at < old.at)) old = x; }
  return old ? h("span", {class: "now-stale"}, nowGlyph("caution"), "as of " + tm(old.at) + " · " + nowAge(old.at) + " old") : null;
}
/* the nav dot: amber when something waits for you, red when something is broken (UX spec 2.5) */
function nowDot() {
  const att = D.attention, items = att && Array.isArray(att.items) ? att.items : [];
  const you = items.filter(i => i.rank === 1).length, broken = items.filter(i => i.tone === "alert" && i.rank <= 4).length, n = (att && att.headline && att.headline.count) || you;
  if (you) return ["you", "Now, " + plural(n, "item") + (n === 1 ? " needs" : " need") + " you"];
  return broken ? ["risk", "Now, " + plural(broken, "thing") + (broken === 1 ? " is" : " are") + " broken"] : null;
}

/* ---- the answer */
function nowDate() { return new Date().toLocaleDateString(undefined, {weekday: "long", month: "short", day: "numeric"}) + " · " + tm(nowS()); }
function nowHead(att) {
  const hd = att.headline || {}, err = att.error, text = err ? err.title : hd.text, line = err ? err.detail : hd.line;
  const note = LIVE.on ? "" : "a copy from " + tm(D.generated) + "; it does not update.";
  return h("header", {class: "now-head"},
    h("div", {class: "now-said"}, h("h1", {class: "now-h1", text: text || "Now"}),
      h("p", {class: "now-line"}, line || null, line && note ? " " : null, note ? h("span", {class: "now-note", text: line ? "· " + note : note.charAt(0).toUpperCase() + note.slice(1)}) : null)),
    h("time", {class: "now-date num", datetime: new Date().toISOString(), text: nowDate()}));
}

/* ---- the Needs you queue: the server's order, the first five rows */
function nowTarget(it) {
  const o = it.open, ext = label => it.url ? {href: it.url, ext: true, label} : null;
  if (!o) return ext("Open");
  if (o.kind === "run") return {href: modalHref("run", o.id), label: "Open the run", go: () => /^D\d+$/.test(o.anchor || "") ? openRunAt(o.id) : openModal("run", o.id)};
  if (o.kind === "ci") {
    const w = CIW.watches.find(x => "ci_failed:" + x.repo + ":" + x.branch === it.id);
    return w ? {href: modalHref("ci", ciKey(w)), label: "Open the log", go: () => openModal("ci", ciKey(w))} : ext("Open the log");
  }
  if (o.kind === "job") return {href: modalHref("job", o.id), label: "Open the job", go: () => openModal("job", o.id)};
  if (o.kind === "pr") return ext("Open the pull request");
  if (o.kind === "branches") { const pid = o.id || it.project; return pid ? {href: link({view: "projects", id: pid, tab: "branches"}), label: "Branches"} : null; }
  if (o.kind === "all") return {href: link({view: "now", all: true, kind: o.id === "housekeeping" ? "housekeeping" : ""}), label: "See all " + (it.count || "")};
  return ext("Open");
}
function nowOpen(it, idp) {
  const t = nowTarget(it); if (!t) return null;
  return h("a", {class: "now-open", id: idp + nowId(it.id), href: t.href, "data-nav": "", target: t.ext ? "_blank" : null, rel: t.ext ? "noopener noreferrer" : null, text: t.label,
    onclick: t.go ? e => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return; e.preventDefault(); t.go(); } : null});
}
function nowTitle(it) {
  const t = String(it.title || ""), m = (it.kind === "decision" || it.kind === "grant") && /^(D\d+)( · [\s\S]*)$/.exec(t);
  return h("h3", {class: "now-tt"}, m ? [h("span", {class: "now-id", text: m[1]}), m[2]] : t,
    it.irreversible ? h("span", {class: "now-tag", text: "Irreversible"}) : null);
}
/* housekeeping is one quiet row: its count and what it holds, opening the list (UX spec 3.1, rank 6) */
function nowHousekeeping(it) {
  const t = nowTarget(it), line = (Array.isArray(it.lines) ? it.lines : [])[0];
  return h("li", {class: "now-item ink now-house", "data-key": it.id, "data-kind": it.kind, "data-sig": JSON.stringify([it.count, it.lines])}, h("div", {class: "now-g"}),
    h("p", {class: "now-hk"}, h("a", {href: t ? t.href : link({view: "now", all: true, kind: "housekeeping"}), id: "now-o-" + nowId(it.id), "data-nav": ""},
      h("span", {class: "now-key", text: it.title || "Housekeeping"}), h("span", {class: "now-n num", text: " " + (it.count || "")})), line ? h("span", {class: "now-l", text: " · " + line}) : null));
}
function nowItem(it) {
  if (it.kind === "housekeeping") return nowHousekeeping(it);
  const tone = it.tone || "ink", typed = Array.isArray(it.type_to_answer) ? it.type_to_answer : [];
  const opts = Array.isArray(it.options) && it.options.length ? h("p", {class: "now-opts"}, "Options: ", nowJoin(it.options.map(o => h("span", null,
    h("span", {class: "now-key", text: o.key}), " " + o.text, o.recommended ? h("span", {class: "now-rec", text: " (recommended)"}) : null)), " · ")) : null;
  /* consent is typed in the session, never pasted: the words show as keys to press, with no copy button (UX spec 3.9) */
  const type = typed.length ? h("p", {class: "now-type"}, "In the session, type: ", nowJoin(typed.map(t => h("kbd", {text: t})), " or ")) : null;
  const acts = [it.copy ? nowCopy(it.copy, "now-c-" + nowId(it.id)) : null, nowOpen(it, "now-o-")].filter(Boolean);
  return h("li", {class: "now-item " + tone, "data-key": it.id, "data-kind": it.kind, "data-sig": JSON.stringify([it.title, it.lines, it.count, it.since, it.copy, it.options])},
    h("div", {class: "now-g"}, NOW_TONE[tone] ? nowGlyph(tone, NOW_TONE[tone]) : null),
    h("div", {class: "now-body"},
      h("div", {class: "now-t"}, nowTitle(it), it.since ? h("span", {class: "now-age num", text: nowAge(it.since)}) : null),
      (Array.isArray(it.lines) ? it.lines : []).slice(0, 3).map(l => h("p", {class: "now-l", text: l})), opts, type,
      acts.length ? h("div", {class: "now-acts"}, acts) : null));
}
function nowQueue(att) {
  const items = Array.isArray(att.items) ? att.items : [], total = att.total || items.length;
  const head = h("div", {class: "now-sh"}, h("h2", {class: "now-h2", id: "now-q-h", text: "Needs you"}), nowStale("attention"),
    total ? h("a", {class: "now-all", href: link({view: "now", all: true}), "data-nav": ""}, "All " + total, icon("chevron")) : null);
  const degraded = (Array.isArray(att.degraded) ? att.degraded : []).map(g => h("p", {class: "now-degraded"}, nowGlyph("caution"),
    h("span", {text: (g.detail || "") + (g.as_of ? " Shown: the results from " + tm(g.as_of) + "." : "")})));
  const body = att.error ? null : items.length ? h("ol", {class: "now-items"}, items.slice(0, NOW_ROWS).map(nowItem)) : h("p", {class: "now-quiet", text: "Nothing waits for you."});
  return h("section", {class: "now-queue", "aria-labelledby": "now-q-h"}, head, degraded, body);
}

/* ---- the runs, each with its landing strip (the one expressive element, UX spec 4.4) */
function nowWaits(r) { const w = r.waiting_on; return !!w && (w.kind === "decision" || w.kind === "grant"); }
function nowStepState(s, you) { return s.state === "done" ? "done" : s.state === "running" ? "running" : s.state === "blocked" ? (you ? "you" : "blocked") : s.state === "unverified" ? "unverified" : "pending"; }
/* at most eight glyphs in plan order: the last done step, the waiting and running ones, then the next open ones; the rest are counted */
function nowStripSteps(plan, you) {
  const st = plan.map(s => nowStepState(s, you));
  if (plan.length <= NOW_STRIP) return {steps: st, rest: 0};
  const keep = new Set(), last = st.lastIndexOf("done");
  if (last >= 0) keep.add(last);
  st.forEach((s, i) => { if (keep.size < NOW_STRIP && s !== "done" && s !== "pending") keep.add(i); });
  st.forEach((s, i) => { if (keep.size < NOW_STRIP && s === "pending") keep.add(i); });
  const idx = Array.from(keep).sort((a, b) => a - b);
  return {steps: idx.map(i => st[i]), rest: st.filter(s => s === "done").length - idx.filter(i => st[i] === "done").length};
}
function nowLands(r) {
  const e = r.eta || {}, w = r.waiting_on || {};
  if (e.state === "unknown" && w.since) return "No update for " + nowAge(w.since) + ". " + (e.text || "");
  if (e.state === "none" && r.active_min) return nowSpan(r.active_min) + " so far. " + (e.text || "");
  return e.text || "";
}
function nowBracket(r) {
  const e = r.eta || {}, rs = e.remaining_s, la = e.lands_at;
  if (e.state === "after_answer" && rs) return "+" + Math.round(rs.low / 60) + "–" + Math.round(rs.high / 60) + " min";
  if (e.state === "clock" && la) return tm(la.low) + (la.high !== la.low ? "–" + tm(la.high) : "");
  return "";
}
function nowStrip(r, lands) {
  const plan = Array.isArray(r.plan) ? r.plan : [], you = nowWaits(r), {steps, rest} = nowStripSteps(plan, you);
  /* the line from the run's start (a cap), the steps on it in plan order, then the dotted run-out to the landing bracket */
  const done = plan.filter(s => s.state === "done").length, total = plan.length || r.steps || 0, gap = 30, x0 = 10, w = steps.length ? x0 + (steps.length - 1) * gap + 16 : 0;
  const track = sv("svg", {class: "now-track", width: w, height: 16, viewBox: "0 0 " + w + " 16", "aria-hidden": "true", focusable: "false"},
    steps.length ? [sv("path", {class: "now-seg", d: "M1 3.5V12.5"}), sv("path", {class: "now-seg", d: "M1 8H" + (x0 - 2)})] : null);
  steps.forEach((s, i) => {
    if (i) track.append(sv("path", {class: "now-seg", d: "M" + (x0 + (i - 1) * gap + 18) + " 8H" + (x0 + i * gap - 2)}));
    track.append(sv("g", {class: "now-step " + s, "data-state": s, transform: "translate(" + (x0 + i * gap) + " 0)"}, (NOW_GLYPH[s] || []).map(([t, a]) => sv(t, a))));
  });
  const bracket = nowBracket(r), label = done + " of " + total + " steps done" + (you ? "; waiting for your answer to " + r.waiting_on.ref : "") + ". " + lands;
  return h("div", {class: "now-strip", role: "img", "aria-label": label}, rest ? h("span", {class: "now-rest", text: "+" + rest + " done"}) : null,
    steps.length ? track : null, h("span", {class: "now-out" + (you ? " you" : "")}), bracket ? h("span", {class: "now-bracket num", text: bracket}) : null);
}
/* a moot decision waits for nobody (its asking member ended, and no step waits on it): one quiet line each under its run,
   in the server's words, never a row of Needs you */
function nowMoot(r) {
  return (Array.isArray(r.moot) ? r.moot : []).map(m => h("p", {class: "now-moot", "data-key": "moot:" + r.id + ":" + m.id},
    h("span", {class: "now-mid", text: m.id}), m.question ? " · " + m.question + " " : " ", m.text || ""));
}
function nowRun(r) {
  const plan = Array.isArray(r.plan) ? r.plan : [], done = plan.filter(s => s.state === "done").length, total = plan.length || r.steps || 0;
  const w = r.waiting_on, lands = nowLands(r), est = runEst(r);
  const status = done + " of " + total + " steps" + (nowWaits(r) ? " · paused on you " + nowAge(w.since)
    : r.active_min ? " · " + nowSpan(r.active_min) + (est ? " of about " + nowSpan(est) : " so far") : "");
  const name = (r.goal || r.id) + (r.project ? " · " + repoName(r.project) : "");
  /* a run on this Mac opens today's run drawer until the run page ships (slice 2); another machine's run has no detail here */
  const title = r.machine ? h("span", {text: name}) : h("a", {href: modalHref("run", r.id), id: "now-r-" + nowId(r.id), "data-nav": "", text: name,
    onclick: e => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return; e.preventDefault(); openModal("run", r.id); }});
  const meta = [r.branch || (sessOfRun(r) || {}).branch, r.mode, r.agent ? agentName(r.agent) : null, r.machine ? "on " + r.machine : null,
    r.machine && (r.seen || r.updated) ? "last update " + rel(r.seen || r.updated) : null].filter(Boolean).join(" · ");
  return h("li", {class: "now-run" + (r.machine ? " now-far" : ""), "data-key": "run:" + r.id,
    "data-sig": JSON.stringify([r.eta, r.waiting_on, done, total, (Array.isArray(r.moot) ? r.moot : []).map(m => m.id)]),
    onclick: r.machine ? null : rowClick(() => openModal("run", r.id))},
    h("h3", {class: "now-rt"}, title), meta ? h("p", {class: "now-meta", text: meta}) : null, nowStrip(r, lands),
    h("p", {class: "now-status num", text: status}), h("p", {class: "now-lands", text: lands}), nowMoot(r));
}
function nowDoneLine(r) {
  const t = (r.eta && r.eta.text) || "Finished";
  return h("p", {class: "now-done", "data-key": "run:" + r.id}, nowGlyph("done"), h("span", {text: (r.goal || r.id) + " · " + t.charAt(0).toLowerCase() + t.slice(1)}));
}
function nowTeam() {
  const here = String(D.host || "").toLowerCase(), others = PEOPLE.filter(x => !isMe(x.who) && String(x.host || "").toLowerCase() !== here && x.sessions.some(s => s.project));
  return others.length ? h("p", {class: "now-team", text: others.map(x => x.who + " works on " + Array.from(new Set(x.sessions.map(s => repoName(s.project)).filter(Boolean))).join(", ")).join(" · ")}) : null;
}
function nowRunsCol(runs) {
  const list = Array.isArray(runs) ? runs : [], done = list.filter(r => (r.eta && r.eta.state === "done") || r.state === "done"), going = list.filter(r => !done.includes(r));
  const head = h("div", {class: "now-sh"}, h("h2", {class: "now-h2", id: "now-r-h"}, "Running", going.length ? h("span", {class: "now-count num", text: " " + going.length}) : null));
  const body = runs && runs.error ? nowError(runs.error) : [
    going.length ? h("ol", {class: "now-runlist"}, going.slice(0, NOW_RUNS).map(nowRun)) : h("p", {class: "now-quiet", text: "Nothing is running."}),
    going.length > NOW_RUNS ? h("a", {class: "now-more", href: link({view: "background"}), "data-nav": ""}, plural(going.length - NOW_RUNS, "more run")) : null,
    done.map(nowDoneLine)];
  return h("section", {class: "now-runs", "aria-labelledby": "now-r-h"}, head, body, nowTeam());
}

/* ---- the quiet states: what to start next, and the first steps of a new install */
function nowNext(att) {
  const list = Array.isArray(att.next_up) ? att.next_up : [];
  return list.length ? h("section", {class: "now-next", "aria-labelledby": "now-n-h"}, h("div", {class: "now-sh"}, h("h2", {class: "now-h2", id: "now-n-h", text: "Next up"})),
    h("ol", {class: "now-items"}, list.slice(0, 3).map(t => h("li", {class: "now-nx", "data-key": "next:" + t.task_id},
      h("div", {class: "now-body"}, h("h3", {class: "now-tt", text: t.title}), h("p", {class: "now-l", text: repoName(t.project) + " · " + t.task_id})),
      t.copy ? nowCopy(t.copy, "now-c-" + nowId(t.task_id)) : null)))) : null;
}
function nowSetup(att) {
  const list = Array.isArray(att.setup) ? att.setup : [];
  return list.length ? h("section", {class: "now-setup", "aria-labelledby": "now-s-h"}, h("div", {class: "now-sh"}, h("h2", {class: "now-h2", id: "now-s-h", text: "Get started"})),
    h("ol", {class: "now-items"}, list.map(s => h("li", {class: "now-nx", "data-key": "setup:" + s.key},
      h("span", {class: "now-st"}, nowGlyph(s.done ? "done" : "pending"), s.done ? "Done" : "To do"), " ",
      h("span", {class: "now-body", text: s.text}), s.copy ? nowCopy(s.copy, "now-c-setup-" + nowId(s.key)) : null)))) : null;
}

/* ---- the closing lines: is the work safe, and what changed since yesterday */
function nowSafety(sf) {
  if (!sf) return null;
  if (sf.error) return h("div", {class: "now-safety"}, nowError(sf.error));
  const l = sf.line || {}, text = String(l.text || ""), pid = l.open && l.open.kind === "branches" ? l.open.id || ((sf.projects || [])[0] || {}).project : "";
  const tail = l.tone === "caution" && l.oldest ? ", the oldest " + rel(l.oldest) + "." : /[.!?]$/.test(text) ? "" : ".", stale = nowStale("safety");
  return h("p", {class: "now-safety"}, h("span", {class: "now-g"}, l.tone === "caution" ? nowGlyph("caution", "At risk") : l.tone === "ok" ? nowGlyph("done", "Safe") : null),
    h("span", null, text + tail, stale ? [" ", stale] : null, pid ? [" ", h("a", {href: link({view: "projects", id: pid, tab: "branches"}), "data-nav": "", text: "Branches"})] : null));
}
function nowSince(s) {
  const line = s && s.line; if (!line) return null;
  const items = Array.isArray(line.items) ? line.items : [], to = {task_done: "done", pr_merged: "merged"};
  const parts = items.map(it => to[it.kind] ? h("a", {href: modalHref("since", to[it.kind]), text: it.text, onclick: e => { e.preventDefault(); openModal("since", to[it.kind]); }})
    : it.kind === "run_finished" ? h("a", {href: link({view: "background"}), text: it.text}) : h("span", {text: it.text}));
  const stale = nowStale("since"), body = ["Since yesterday: ", parts.length ? nowJoin(parts, " · ") : "nothing finished.", stale ? [" ", stale] : null];
  if (nowSinceOpen === null) {
    const day = lkey(new Date()); nowSinceOpen = new Date().getHours() < 12 || store.get("devkit.now.since", "") !== day; store.set("devkit.now.since", day);
  }
  return nowSinceOpen ? h("p", {class: "now-since"}, body) : h("details", {class: "now-since", "data-key": "now-since"}, h("summary", {text: "Since yesterday"}), h("p", null, body.slice(1)));
}

/* ---- the page */
function nowLoading() {
  const shape = cls => h("div", {class: "now-shape " + cls});
  return h("div", {class: "now-loading", "aria-busy": "true"}, h("p", {class: "sr", text: "Loading what needs you."}), shape("now-shape-h1"),
    h("div", {class: "now-grid"}, h("div", {class: "now-queue"}, [1, 2, 3].map(() => shape("now-shape-row"))), h("div", {class: "now-runs"}, [1, 2].map(() => shape("now-shape-run")))));
}
function viewNowAll(main) {
  if (S.kind && S.kind !== "housekeeping") { viewActions(main); return; }   /* an old #/actions?kind=… link: that kind of today's actions */
  const att = D.attention || {}, items = Array.isArray(att.items) ? att.items : [], house = S.kind === "housekeeping";
  const list = house ? ACTIONS.filter(a => NOW_HOUSEKEEPING.includes(a.kind)) : items;
  main.append(crumbs([["Now", link({view: "now", all: false, kind: ""})], [house ? "Housekeeping" : "Needs you"]]));
  main.append(h("header", {class: "now-head"}, h("div", {class: "now-said"}, h("h1", {class: "now-h1", text: house ? "Housekeeping" : "Everything that needs you"}),
    h("p", {class: "now-line", text: plural(house ? list.length : att.total || items.length, "item") + ", most urgent first"}))));
  if (house) { main.append(list.length ? h("ul", {class: "acts"}, list.map(a => actionRow(a))) : h("p", {class: "now-quiet", text: "Nothing to tidy."})); return; }
  main.append(h("section", {class: "now-queue now-wide", "aria-label": "Needs you"}, items.length ? h("ol", {class: "now-items"}, items.map(nowItem)) : h("p", {class: "now-quiet", text: "Nothing waits for you."})));
}
function viewNow(main) {
  main.classList.add("now-view");
  if (S.all) { viewNowAll(main); return; }
  const att = D.attention;
  if (!att || typeof att !== "object") { main.append(nowLoading()); return; }
  main.append(nowHead(att));
  const kind = (att.headline || {}).kind;
  /* nothing needs you and nothing runs: the two columns become one block (Next up, or the first steps) */
  if (!att.error && (kind === "clear_idle" || kind === "welcome")) main.append(h("div", {class: "now-one"}, kind === "welcome" ? nowSetup(att) : nowNext(att)));
  else main.append(h("div", {class: "now-grid"}, nowQueue(att), nowRunsCol(D.runs)));
  main.append(h("div", {class: "now-foot"}, nowSafety(D.safety), nowSince(D.since)));
}
VIEWFN.now = viewNow;
