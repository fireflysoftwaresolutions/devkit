# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The dashboard Now data contract (spec P-fyli4qqn0y7), as a checker both builders share.

`check(doc) -> list[str]`: every way `doc` breaks the contract, one sentence each; [] when it keeps it. `doc` holds
any of the five areas (attention, runs, safety, since, as_of); an area may instead be `{"error": {title, detail}}`.
The rules: required keys present (absent values are null, never omitted), types and enums, the size caps, the queue
order, instants as integer epoch seconds and durations as integer seconds in `_s` fields, copy commands without
control characters, https links only, and `type_to_answer` (consent, typed by the developer) never next to a `copy`.
Standard library only, Python 3.9.
"""
from __future__ import annotations

AREAS = ("attention", "runs", "safety", "since", "as_of")
TONES = ("you", "alert", "caution", "ink")
OPEN_KINDS = ("run", "ci", "job", "branches", "pr", "all")
HEADLINE_KINDS = ("needs_you", "urgent", "look", "clear_running", "clear_idle", "welcome")
ITEM_KEYS = ("id", "rank", "kind", "tone", "title", "lines", "clause", "since", "count", "project", "run_id", "asker",
             "blocks", "options", "irreversible", "type_to_answer", "copy", "open", "url")
WAITING_KINDS = ("decision", "grant", "blocked", "stale")
ETA_STATES = ("clock", "after_answer", "blocked", "unknown", "none", "done")
ETA_BASES = ("critical_path", "estimate", None)
SAFETY_STATES = ("not_backed_up", "embargoed", "waiting")
SAFETY_TONES = ("caution", "ok", "unknown")
SINCE_KINDS = ("release", "task_done", "pr_merged", "run_finished")
SETUP_KEYS = ("agents", "project", "sync")
DEGRADED_SOURCES = ("prs", "ci", "registry")
EPOCH_MIN = 1_000_000_000          # 2001: anything smaller is not an epoch in seconds
ITEMS_CAP, RUNS_CAP, REMOTE_RUNS_CAP, ROWS_CAP = 200, 20, 20, 50


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str(v) -> bool:
    return isinstance(v, str)


def _opt_str(v) -> bool:
    return v is None or isinstance(v, str)


def _epoch(v, nullable=True) -> bool:
    return (v is None and nullable) or (_int(v) and v >= EPOCH_MIN)


def _control(s: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in s)


class _Checker:
    def __init__(self):
        self.errors = []

    def err(self, where: str, what: str):
        self.errors.append(f"{where}: {what}")

    def keys(self, where: str, obj, keys) -> bool:
        if not isinstance(obj, dict):
            self.err(where, f"is {type(obj).__name__}, not an object")
            return False
        missing = [k for k in keys if k not in obj]
        if missing:
            self.err(where, "misses " + ", ".join(missing))
        return not missing

    def text(self, where: str, v, cap: int, nullable=False):
        if v is None and nullable:
            return
        if not _str(v):
            self.err(where, "is not a string")
        elif len(v) > cap:
            self.err(where, f"is {len(v)} characters, over {cap}")
        elif "\n" in v:
            self.err(where, "holds a newline")

    def units(self, where: str, obj):
        """Every `_s` field holds integer seconds (or an object of them); recursive."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                w = f"{where}.{k}"
                if k.endswith("_s") and v is not None:
                    vals = list(v.values()) if isinstance(v, dict) else [v]
                    if not all(_int(x) for x in vals):
                        self.err(w, "is not integer seconds")
                self.units(w, v)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self.units(f"{where}[{i}]", v)

    def open_(self, where: str, v):
        if self.keys(where, v, ("kind", "id", "anchor")):
            if v["kind"] not in OPEN_KINDS:
                self.err(where + ".kind", f"{v['kind']!r} is not one of {', '.join(OPEN_KINDS)}")
            if not _opt_str(v["id"]) or not _opt_str(v["anchor"]):
                self.err(where, "id and anchor are strings or null")

    def copy(self, where: str, v):
        if v is None:
            return
        if not _str(v) or not v.strip():
            self.err(where, "is not a command")
        elif _control(v):
            self.err(where, "holds a control character")

    def url(self, where: str, v):
        if v is not None and not (_str(v) and v.startswith("https://")):
            self.err(where, "is not an https URL")

    def error_area(self, where: str, v) -> bool:
        """True when the area is an error document (checked here)."""
        if isinstance(v, dict) and set(v) == {"error"}:
            e = v["error"]
            if self.keys(where + ".error", e, ("title", "detail")):
                self.text(where + ".error.title", e["title"], 160)
                self.text(where + ".error.detail", e["detail"], 300)
            return True
        return False

    # ----------------------------------------------------------------------- attention

    def attention(self, a):
        w = "attention"
        if not self.keys(w, a, ("headline", "items", "total", "next_up", "setup", "degraded")):
            return
        h = a["headline"]
        if self.keys(w + ".headline", h, ("kind", "count", "text", "line")):
            if h["kind"] not in HEADLINE_KINDS:
                self.err(w + ".headline.kind", f"{h['kind']!r} is not a headline kind")
            if not _int(h["count"]) or h["count"] < 0:
                self.err(w + ".headline.count", "is not a count")
            self.text(w + ".headline.text", h["text"], 160)
            self.text(w + ".headline.line", h["line"], 300)
        items = a["items"]
        if not isinstance(items, list):
            self.err(w + ".items", "is not a list")
            items = []
        if len(items) > ITEMS_CAP:
            self.err(w + ".items", f"holds {len(items)} rows, over {ITEMS_CAP}")
        if not _int(a["total"]) or a["total"] < len(items):
            self.err(w + ".total", "is not a count of at least the rows listed")
        ids = set()
        for i, it in enumerate(items):
            self.item(f"{w}.items[{i}]", it)
            if isinstance(it, dict):
                if it.get("id") in ids:
                    self.err(f"{w}.items[{i}].id", f"{it.get('id')!r} is repeated")
                ids.add(it.get("id"))
        if isinstance(h, dict) and _int(h.get("count")):
            n = sum(1 for it in items if isinstance(it, dict) and _int(it.get("rank")) and it["rank"] <= 4)
            if a["total"] == len(items) and h["count"] != n:
                self.err(w + ".headline.count", f"is {h['count']}, but {n} rows have ranks 1-4")
        try:
            keys = [order_key(it) for it in items]
            if keys != sorted(keys):
                self.err(w + ".items", "are not in the order (rank, -lanes, since, id)")
        except (TypeError, KeyError, AttributeError):
            pass  # the item checks above already name what is wrong
        nu = a["next_up"]
        if not isinstance(nu, list) or len(nu) > 3:
            self.err(w + ".next_up", "is not a list of at most 3")
        else:
            for i, x in enumerate(nu):
                if self.keys(f"{w}.next_up[{i}]", x, ("project", "task_id", "title", "copy")):
                    self.text(f"{w}.next_up[{i}].title", x["title"], 160)
                    self.copy(f"{w}.next_up[{i}].copy", x["copy"])
        su = a["setup"]
        if not isinstance(su, list):
            self.err(w + ".setup", "is not a list")
        else:
            for i, x in enumerate(su):
                if self.keys(f"{w}.setup[{i}]", x, ("key", "done", "text", "copy")):
                    if x["key"] not in SETUP_KEYS:
                        self.err(f"{w}.setup[{i}].key", f"{x['key']!r} is not one of {', '.join(SETUP_KEYS)}")
                    if not isinstance(x["done"], bool):
                        self.err(f"{w}.setup[{i}].done", "is not true or false")
                    self.copy(f"{w}.setup[{i}].copy", x["copy"])
        dg = a["degraded"]
        if not isinstance(dg, list):
            self.err(w + ".degraded", "is not a list")
        else:
            for i, x in enumerate(dg):
                if self.keys(f"{w}.degraded[{i}]", x, ("source", "detail", "as_of")):
                    if x["source"] not in DEGRADED_SOURCES:
                        self.err(f"{w}.degraded[{i}].source", f"{x['source']!r} is not a source")
                    self.text(f"{w}.degraded[{i}].detail", x["detail"], 300)
                    if not _epoch(x["as_of"]):
                        self.err(f"{w}.degraded[{i}].as_of", "is not an epoch")

    def item(self, w, it):
        if not self.keys(w, it, ITEM_KEYS):
            return
        if not _str(it["id"]) or not it["id"]:
            self.err(w + ".id", "is not a stable id")
        if not _int(it["rank"]) or not 1 <= it["rank"] <= 6:
            self.err(w + ".rank", "is not 1-6")
        if not _str(it["kind"]) or not it["kind"]:
            self.err(w + ".kind", "is not a kind")
        if it["tone"] not in TONES:
            self.err(w + ".tone", f"{it['tone']!r} is not one of {', '.join(TONES)}")
        self.text(w + ".title", it["title"], 160)
        if not isinstance(it["lines"], list) or len(it["lines"]) > 3:
            self.err(w + ".lines", "is not a list of at most 3")
        else:
            for i, ln in enumerate(it["lines"]):
                self.text(f"{w}.lines[{i}]", ln, 300)
        self.text(w + ".clause", it["clause"], 120)
        if not _epoch(it["since"]):
            self.err(w + ".since", "is not an epoch or null")
        if not _int(it["count"]) or it["count"] < 1:
            self.err(w + ".count", "is not at least 1")
        for k in ("project", "run_id", "asker", "irreversible"):
            if not _opt_str(it[k]):
                self.err(f"{w}.{k}", "is not a string or null")
        b = it["blocks"]
        if b is not None and self.keys(w + ".blocks", b, ("run", "lanes")):
            if not _int(b["lanes"]) or b["lanes"] < 0:
                self.err(w + ".blocks.lanes", "is not a count")
        if it["options"] is not None:
            if not isinstance(it["options"], list):
                self.err(w + ".options", "is not a list")
            else:
                for i, o in enumerate(it["options"]):
                    if self.keys(f"{w}.options[{i}]", o, ("key", "text", "recommended")) and \
                            not isinstance(o["recommended"], bool):
                        self.err(f"{w}.options[{i}].recommended", "is not true or false")
        tta = it["type_to_answer"]
        if tta is not None and not (isinstance(tta, list) and tta and all(_str(x) for x in tta)):
            self.err(w + ".type_to_answer", "is not a list of lines")
        self.copy(w + ".copy", it["copy"])
        if tta is not None and it["copy"] is not None:
            self.err(w, "has both type_to_answer and copy: consent is never copyable")
        if it["rank"] == 1 and it["copy"] is not None:
            self.err(w + ".copy", "is set on a rank-1 row, which is answered by typing")
        self.open_(w + ".open", it["open"])
        self.url(w + ".url", it["url"])

    # ----------------------------------------------------------------------- runs

    def runs(self, runs):
        w = "runs"
        if not isinstance(runs, list):
            self.err(w, "is not a list")
            return
        local = [r for r in runs if isinstance(r, dict) and r.get("machine") is None]
        if len(local) > RUNS_CAP or len(runs) - len(local) > REMOTE_RUNS_CAP:
            self.err(w, f"holds {len(local)} runs of this Mac and {len(runs) - len(local)} of others, over the caps")
        for i, r in enumerate(runs):
            wi = f"{w}[{i}]"
            if not self.keys(wi, r, ("id", "goal", "project", "machine", "who", "waiting_on", "eta")):
                continue
            if not _str(r["id"]) or not r["id"]:
                self.err(wi + ".id", "is not an id")
            if not _opt_str(r["machine"]) or not _opt_str(r["who"]):
                self.err(wi, "machine and who are strings or null")
            wo = r["waiting_on"]
            if wo is not None and self.keys(wi + ".waiting_on", wo, ("kind", "ref", "since")):
                if wo["kind"] not in WAITING_KINDS:
                    self.err(wi + ".waiting_on.kind", f"{wo['kind']!r} is not one of {', '.join(WAITING_KINDS)}")
                if not _epoch(wo["since"]):
                    self.err(wi + ".waiting_on.since", "is not an epoch or null")
            self.eta(wi + ".eta", r["eta"], wo)

    def eta(self, w, e, wo):
        if not self.keys(w, e, ("state", "basis", "remaining_s", "lands_at", "over_estimate", "text")):
            return
        st = e["state"]
        if st not in ETA_STATES:
            self.err(w + ".state", f"{st!r} is not one of {', '.join(ETA_STATES)}")
        if e["basis"] not in ETA_BASES:
            self.err(w + ".basis", f"{e['basis']!r} is not a basis")
        self.text(w + ".text", e["text"], 300)
        rem, land = e["remaining_s"], e["lands_at"]
        for k, v, epoch in (("remaining_s", rem, False), ("lands_at", land, True)):
            if v is None:
                continue
            if not self.keys(f"{w}.{k}", v, ("low", "high")):
                continue
            if not (_int(v["low"]) and _int(v["high"])) or v["low"] > v["high"]:
                self.err(f"{w}.{k}", "is not a range of integers, low first")
            elif v["low"] % 300 or v["high"] % 300:
                self.err(f"{w}.{k}", "is not rounded to 5 minutes")
            elif epoch and not (_epoch(v["low"], False) and _epoch(v["high"], False)):
                self.err(f"{w}.{k}", "is not a range of epochs")
        want = {"clock": (True, True), "after_answer": (True, False)}.get(st, (False, False))
        if (rem is not None, land is not None) != want:
            self.err(w, f"state {st} wants remaining_s {'set' if want[0] else 'null'} and lands_at "
                        f"{'set' if want[1] else 'null'}")
        oe = e["over_estimate"]
        if oe is not None and self.keys(w + ".over_estimate", oe, ("estimate_s", "now_s")) and st != "clock":
            self.err(w + ".over_estimate", "is set outside the clock state")
        if st == "after_answer" and not (isinstance(wo, dict) and wo.get("kind") in ("decision", "grant")):
            self.err(w, "is after_answer, but the run waits on no decision or grant")

    # ----------------------------------------------------------------------- safety, since, as_of

    def safety(self, s):
        w = "safety"
        if not self.keys(w, s, ("totals", "line", "projects")):
            return
        t = s["totals"]
        if self.keys(w + ".totals", t, ("not_backed_up", "embargoed", "waiting", "unknown_projects")):
            if not all(_int(v) and v >= 0 for v in t.values()):
                self.err(w + ".totals", "holds a value that is not a count")
        ln = s["line"]
        if self.keys(w + ".line", ln, ("tone", "text", "oldest", "open")):
            if ln["tone"] not in SAFETY_TONES:
                self.err(w + ".line.tone", f"{ln['tone']!r} is not one of {', '.join(SAFETY_TONES)}")
            self.text(w + ".line.text", ln["text"], 300)
            if not _epoch(ln["oldest"]):
                self.err(w + ".line.oldest", "is not an epoch or null")
            self.open_(w + ".line.open", ln["open"])
        if not isinstance(s["projects"], list):
            self.err(w + ".projects", "is not a list")
            return
        for i, p in enumerate(s["projects"]):
            wp = f"{w}.projects[{i}]"
            if not self.keys(wp, p, ("project", "not_backed_up", "embargoed", "waiting", "oldest", "mapped_at", "rows")):
                continue
            if not (_epoch(p["oldest"]) and _epoch(p["mapped_at"])):
                self.err(wp, "oldest and mapped_at are epochs or null")
            rows = p["rows"] if isinstance(p["rows"], list) else []
            if len(rows) > ROWS_CAP:
                self.err(wp + ".rows", f"holds {len(rows)} rows, over {ROWS_CAP}")
            for j, r in enumerate(rows):
                wr = f"{wp}.rows[{j}]"
                if not self.keys(wr, r, ("branch", "state", "reason", "commits", "oldest", "copy")):
                    continue
                if r["state"] not in SAFETY_STATES:
                    self.err(wr + ".state", f"{r['state']!r} is not one of {', '.join(SAFETY_STATES)}")
                if not _epoch(r["oldest"]):
                    self.err(wr + ".oldest", "is not an epoch or null")
                if r["commits"] is not None and not _int(r["commits"]):
                    self.err(wr + ".commits", "is not a count")
                self.copy(wr + ".copy", r["copy"])
                if r["copy"] is not None and r["state"] != "not_backed_up":
                    self.err(wr + ".copy", "is set on a branch that needs no backup command")

    def since(self, s):
        w = "since"
        if not isinstance(s, dict) or not self.keys(w + ".line", s.get("line"), ("from", "items", "text")):
            if not isinstance(s, dict):
                self.err(w, "is not an object")
            return
        ln = s["line"]
        if not _epoch(ln["from"], False):
            self.err(w + ".line.from", "is not an epoch")
        self.text(w + ".line.text", ln["text"], 300)
        for i, x in enumerate(ln["items"] if isinstance(ln["items"], list) else []):
            if self.keys(f"{w}.line.items[{i}]", x, ("kind", "count", "text")):
                if x["kind"] not in SINCE_KINDS:
                    self.err(f"{w}.line.items[{i}].kind", f"{x['kind']!r} is not one of {', '.join(SINCE_KINDS)}")
                if not _int(x["count"]) or x["count"] < 1:
                    self.err(f"{w}.line.items[{i}].count", "is not at least 1")
        kinds = [x.get("kind") for x in ln["items"] if isinstance(x, dict)] if isinstance(ln["items"], list) else []
        if "release" in kinds and kinds[0] != "release":
            self.err(w + ".line.items", "list releases first")

    def as_of(self, a):
        w = "as_of"
        if not self.keys(w, a, ("registry", "prs")):
            return
        for k in ("registry", "prs"):
            if self.keys(f"{w}.{k}", a[k], ("at", "every_s")):
                if not _epoch(a[k]["at"]):
                    self.err(f"{w}.{k}.at", "is not an epoch or null")
                if not _int(a[k]["every_s"]) or a[k]["every_s"] <= 0:
                    self.err(f"{w}.{k}.every_s", "is not a positive number of seconds")


def order_key(it: dict):
    """The queue's order: (rank, -blocks.lanes, since (oldest first, null last), id)."""
    lanes = (it.get("blocks") or {}).get("lanes") or 0
    since = it.get("since")
    return (it["rank"], -lanes, since is None, since or 0, it["id"])


def check(doc) -> list:
    """Every way `doc` breaks the Now contract; [] when it keeps it."""
    c = _Checker()
    if not isinstance(doc, dict):
        return ["the document is not an object"]
    if not any(k in doc for k in AREAS):
        return ["the document holds none of the areas " + ", ".join(AREAS)]
    for k in AREAS:
        if k not in doc or c.error_area(k, doc[k]):
            continue
        getattr(c, k)(doc[k])
        c.units(k, doc[k])
    return c.errors
