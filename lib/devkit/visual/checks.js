// devkit's in-page visual checks. The capture driver runs them on the page state it captured, after the screenshot,
// in an isolated world of the page's top frame:
//
//   const {runChecks} = require("./checks.js");
//   const signals = await <call runChecks in the world>(viewportWidth, {spacing, mask, focus: false});
//   … axe, then the detector …
//   const focus = await <call runChecks in the world>(viewportWidth, {mask, focus: "only"});
//
// runChecks is self-contained (its source is sent alone) and writes nothing to the page: no DOM writes, no scrolling.
// The one check that has to act, focus-visible, moves the focus through the first 30 tab stops and then gives the
// focus back to the element that had it, finishes the transitions its focus started, and puts the scroll back. A page
// can still change itself when a control gets the focus (a menu that opens on focus), so the capture driver runs that
// walk last, alone, after axe and the detector have read the page as captured.
//
// `spacing` is the project's spacing scale for off-scale-spacing: a list of px values (from DESIGN.md's tokens), or
// "page" for the five values the page uses most. Without it, off-scale-spacing does not run. `mask` is the request's
// own mask selectors, a list of CSS selectors; the capture's mask rules (capture.cjs's MASK_RULES) are always masked.
// No check looks at a masked element or at anything inside one, so no signal carries its box, selector, or text.
// `focus` is false for every check but focus-visible, "only" for focus-visible alone; without it every check runs,
// focus-visible last.
//
// Each signal is {check, pass, rule[], anchor: {region, selector}, measured, threshold}, with `other` (the second
// element of an overlap, the sibling an overflow crosses, what covers a control, the sibling a near miss almost
// meets), `advice: true` for the phone-size advice, and `text` (the full text of a truncation, at most 120
// characters). A region is [x, y, width, height] in page CSS px. A signal carries no name and no other page text:
// the capture driver takes the anchor's name from the redacted description and redacts `text` before any write.
async function runChecks(viewportWidth, options) {
  const vw = Number(viewportWidth) || window.innerWidth;
  const phone = vw < 768;
  const opts = options && typeof options === "object" ? options : {};
  const CONTROLS = "a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], " +
    "[role=link], [role=checkbox], [role=radio], [role=switch], [role=tab], [role=menuitem], [role=option], " +
    "[role=slider], [role=spinbutton], [role=combobox], [role=textbox], [contenteditable=''], [contenteditable=true]";
  const BODY_TEXT = "p, li, dd, dt, td, th, blockquote";
  const signals = [];
  const styles = new Map();
  const style = el => {
    if (!styles.has(el)) styles.set(el, getComputedStyle(el));
    return styles.get(el);
  };

  // geometry: boxes are {left, top, right, bottom} in page CSS px; regions are rounded [x, y, width, height]
  const page = r => ({left: r.left + scrollX, top: r.top + scrollY, right: r.right + scrollX, bottom: r.bottom + scrollY});
  const region = b => [b.left, b.top, b.right - b.left, b.bottom - b.top].map(Math.round);
  const cross = (a, b) => [Math.min(a.right, b.right) - Math.max(a.left, b.left),
                           Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)];
  const inside = (a, b) => a.left >= b.left - 1 && a.top >= b.top - 1 && a.right <= b.right + 1 && a.bottom <= b.bottom + 1;

  // a CSS selector that matches the element alone, in devkit's order as capture.cjs writes it: its test id, its id,
  // or a path of nth-of-type steps from the nearest ancestor with a unique id (from html when none has one)
  const unique = sel => { try { return document.querySelectorAll(sel).length === 1; } catch { return false; } };
  const selector = el => {
    const testid = el.getAttribute("data-testid");
    if (testid !== null && unique(`[data-testid="${CSS.escape(testid)}"]`)) return `[data-testid="${CSS.escape(testid)}"]`;
    if (el.id && unique(`#${CSS.escape(el.id)}`)) return `#${CSS.escape(el.id)}`;
    const steps = [];
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (node !== el && node.id && unique(`#${CSS.escape(node.id)}`)) {
        steps.unshift(`#${CSS.escape(node.id)}`);
        break;
      }
      const tag = node.localName;
      const same = node.parentElement ? Array.from(node.parentElement.children).filter(c => c.localName === tag) : [node];
      steps.unshift(same.length > 1 ? `${CSS.escape(tag)}:nth-of-type(${same.indexOf(node) + 1})` : CSS.escape(tag));
    }
    return steps.join(" > ");
  };
  // an anchor is the element's own border box, as the capture's description stores it
  const anchor = el => ({region: region(page(el.getBoundingClientRect())), selector: selector(el)});
  const signal = (check, pass, rule, el, measured, threshold, extra) =>
    signals.push({check, pass, rule, anchor: anchor(el), measured, threshold, ...extra});

  // what the capture masks: the same rules as capture.cjs's MASK_RULES, and the request's own selectors (the capture
  // driver refuses a selector that is not valid CSS before the checks run, so one that does not parse is left out)
  const MASK_RULES = ['input[type=password i]', '[autocomplete~="current-password" i]', '[autocomplete~="new-password" i]',
    '[autocomplete~="one-time-code" i]', '[autocomplete^="cc-" i]', '[autocomplete*=" cc-" i]', '[data-visual-mask]'];
  const parses = sel => { try { document.querySelector(sel); return true; } catch { return false; } };
  const maskSelector = [...MASK_RULES, ...(Array.isArray(opts.mask) ? opts.mask : [])]
    .filter(sel => typeof sel === "string" && sel.trim() && parses(sel)).join(", ");
  const masked = el => !!el.closest(maskSelector);

  // what the checks look at: rendered elements outside aria-hidden decorations and outside masked elements
  const all = Array.from(document.body ? document.body.querySelectorAll("*") : []);
  const shown = el => el.checkVisibility({opacityProperty: true, visibilityProperty: true}) &&
    el.getClientRects().length > 0;
  const decoration = el => !!el.closest("[aria-hidden=true]");
  // visually hidden: text a screen reader reads that the page clips to nothing, as the screen-reader-only patterns
  // do (clip-path inset(50%), clip rect(0 0 0 0), a box of 1 px or less that clips its overflow). It covers nothing a
  // user sees, so it overlaps nothing.
  const px = (v, size) => v.endsWith("%") ? parseFloat(v) / 100 * size : parseFloat(v);
  const insetCloses = (value, w, h) => {
    const m = /^inset\(([^)]*?)(?:\s+round\b[^)]*)?\)$/.exec(value);
    if (!m) return false;
    const [t, r = t, b = t, l = r] = m[1].trim().split(/\s+/);  // top, right, bottom, left, as the shorthand expands
    return px(t, h) + px(b, h) >= h || px(l, w) + px(r, w) >= w;
  };
  const rectCloses = (value, w, h) => {
    const m = /^rect\(([^)]*)\)$/.exec(value);
    if (!m) return false;
    const [t, r, b, l] = m[1].split(/\s*,\s*|\s+/).map((v, i) => v === "auto" ? [0, w, h, 0][i] : parseFloat(v));
    return r - l <= 0 || b - t <= 0;
  };
  const clipped = new Map();
  const clippedAway = el => {
    if (!el || el === document.documentElement) return false;
    if (clipped.has(el)) return clipped.get(el);
    const cs = style(el), r = el.getBoundingClientRect();
    const away = insetCloses(cs.clipPath, r.width, r.height) ||
      (cs.position === "absolute" || cs.position === "fixed") && rectCloses(cs.clip, r.width, r.height) ||
      cs.overflowX !== "visible" && cs.overflowY !== "visible" && Math.min(r.width, r.height) <= 1 ||
      clippedAway(el.parentElement);
    clipped.set(el, away);
    return away;
  };
  const fixed = el => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (style(node).position === "fixed") return true;
    }
    return false;
  };
  const isControl = el => el.matches(CONTROLS);
  const ownText = el => Array.from(el.childNodes).filter(n => n.nodeType === 3 && n.data.trim());
  // the box of an element's own text: from its first to its last text node, so a block's empty width is left out
  const textBox = nodes => {
    const range = document.createRange();
    range.setStartBefore(nodes[0]);
    range.setEndAfter(nodes[nodes.length - 1]);
    const r = range.getBoundingClientRect();
    return r.width > 0 && r.height > 0 ? page(r) : null;
  };
  const visible = all.filter(el => shown(el) && !decoration(el) && !masked(el));
  const controls = visible.filter(isControl);
  const texts = new Map(visible.map(el => [el, ownText(el)]).filter(([, nodes]) => nodes.length));
  if (opts.focus === "only") {
    focusVisible();
    return signals;
  }

  // overlap: two visible text or control boxes cross by more than 2 px both ways, neither holds the other, and
  // neither is fixed or visually hidden; on a control it counts against target spacing (wcag:2.5.8)
  {
    const boxes = [];
    for (const el of new Set([...controls, ...texts.keys()])) {
      if (fixed(el) || clippedAway(el)) continue;
      const control = isControl(el);
      const box = control ? page(el.getBoundingClientRect()) : textBox(texts.get(el));
      if (box) boxes.push({el, box, control});
    }
    boxes.sort((a, b) => a.box.top - b.box.top);
    const order = new Map(all.map((el, i) => [el, i]));
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length && boxes[j].box.top < boxes[i].box.bottom; j++) {
        let [a, b] = [boxes[i], boxes[j]];
        if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
        const [w, h] = cross(a.box, b.box);
        if (w <= 2 || h <= 2 || inside(a.box, b.box) || inside(b.box, a.box)) continue;
        if (b.control && !a.control || b.control === a.control && order.get(b.el) < order.get(a.el)) [a, b] = [b, a];
        signal("overlap", "layout", [a.control ? "wcag:2.5.8" : "nielsen:8"], a.el,
               `${Math.round(w)}×${Math.round(h)} px intersection`, "2 px", {other: anchor(b.el)});
      }
    }
  }

  // page-overflow: the document is wider than the viewport; the anchor is the outermost element that reaches
  // furthest past its right edge, or html when none does (a wide margin)
  const pageWidth = document.documentElement.scrollWidth;
  if (pageWidth > vw) {
    const right = el => el.getBoundingClientRect().right + scrollX;
    const clipped = el => {
      for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
        if (style(node).overflowX !== "visible") return true;
      }
      return false;
    };
    let widest = null, reach = vw + 0.5;
    for (const el of [document.body, ...visible]) {
      if (!el || right(el) <= reach || fixed(el) || clipped(el)) continue;
      if (el.parentElement && el.parentElement !== document.documentElement && right(el.parentElement) > vw + 0.5) continue;
      widest = el;
      reach = right(el);
    }
    signal("page-overflow", "responsive", ["wcag:1.4.10"], widest || document.documentElement, `${pageWidth} px`, `${vw} px`);
  }

  // element-overflow: content spills more than 1 px out of a box with overflow: visible and over a sibling
  const seen = new Set(visible);
  for (const el of visible) {
    const cs = style(el);
    if (cs.overflowX !== "visible" || cs.overflowY !== "visible" || !el.clientWidth || !el.clientHeight) continue;
    const spillX = el.scrollWidth - el.clientWidth, spillY = el.scrollHeight - el.clientHeight;
    if (spillX <= 1 && spillY <= 1) continue;
    const own = page(el.getBoundingClientRect());
    const left = own.left + el.clientLeft, top = own.top + el.clientTop;
    const spill = {left, top, right: left + el.scrollWidth, bottom: top + el.scrollHeight};
    const crossed = Array.from(el.parentElement.children).find(sib => {
      if (sib === el || !seen.has(sib)) return false;
      const box = page(sib.getBoundingClientRect());
      const [w, h] = cross(spill, box);
      return w > 1 && h > 1;
    });
    if (crossed) {
      signal("element-overflow", "layout", ["nielsen:8"], el, `${Math.max(spillX, spillY)} px past the box`, "1 px",
             {other: anchor(crossed)});
    }
  }

  // truncation: text cut by an ellipsis or a line clamp, with its full text cut at 120 characters; form controls are
  // left out, since their text is a value that never leaves the page
  for (const el of visible) {
    if (el.matches("input, textarea, select, [contenteditable]")) continue;
    const cs = style(el);
    const clamp = [cs.getPropertyValue("-webkit-line-clamp"), cs.getPropertyValue("line-clamp")].some(v => v && v !== "none");
    const cutX = cs.textOverflow === "ellipsis" && cs.overflowX !== "visible" && el.scrollWidth > el.clientWidth;
    const cutY = clamp && el.scrollHeight > el.clientHeight;
    const full = cutX || cutY ? Array.from(el.innerText.replace(/\s+/g, " ").trim()) : [];
    if (!full.length) continue;
    signal("truncation", "content", ["nielsen:6"], el,
           `${cutX ? el.scrollWidth - el.clientWidth : el.scrollHeight - el.clientHeight} px cut`, "0 px",
           {text: full.length > 120 ? full.slice(0, 119).join("") + "…" : full.join("")});
  }

  // tap-target, advice at phone widths: a control under 44×44 CSS px; a link inside a sentence is exempt, and a
  // control that has a label counts with the label's box
  if (phone) {
    for (const el of controls) {
      const inSentence = el.localName === "a" && style(el).display === "inline" &&
        Array.from(el.parentElement.childNodes).some(n => n !== el && n.nodeType === 3 && n.data.trim());
      const boxes = [el, ...Array.from(el.labels || [])].map(e => e.getBoundingClientRect());
      if (inSentence || boxes.some(r => r.width >= 43.99 && r.height >= 43.99)) continue;
      signal("tap-target", "accessibility", ["platform:touch-target-44"], el,
             `${Math.round(boxes[0].width)}×${Math.round(boxes[0].height)} px`, "44×44 px", {advice: true});
    }
  }

  // min-text-size: text under 12 px; at phone widths, body text under 16 px, as advice
  for (const [el, nodes] of texts) {
    if (!textBox(nodes)) continue;
    const size = parseFloat(style(el).fontSize);
    const small = size < 12, body = phone && size < 16 && !!el.closest(BODY_TEXT);
    if (small || body) {
      signal("min-text-size", "typography", ["ds:min-text-size"], el, `${+size.toFixed(1)} px`,
             small ? "12 px" : "16 px", small ? {} : {advice: true});
    }
  }

  // layout-shift: the cumulative layout shift of the load and the steps over 0.1: the largest session window of
  // shifts (each less than 1 s after the last, the window at most 5 s, none right after input), anchored on the
  // element that moved the most in its largest shift
  if (PerformanceObserver.supportedEntryTypes.includes("layout-shift")) {
    const observer = new PerformanceObserver(() => {});
    observer.observe({type: "layout-shift", buffered: true});
    const shifts = observer.takeRecords().filter(e => !e.hadRecentInput);
    observer.disconnect();
    let cls = 0, worst = null, sum = 0, start = -Infinity, last = -Infinity, top = null;
    for (const e of shifts) {
      if (e.startTime - last >= 1000 || e.startTime - start >= 5000) [sum, start, top] = [0, e.startTime, null];
      sum += e.value;
      last = e.startTime;
      if (!top || e.value > top.value) top = e;
      if (sum > cls) [cls, worst] = [sum, top];
    }
    if (cls > 0.1) {
      const area = s => s.currentRect.width * s.currentRect.height;
      const element = s => s.node.nodeType === 1 ? s.node : s.node.parentElement;
      const moved = worst.sources.filter(s => s.node && s.node.isConnected && element(s) && !masked(element(s)))
        .sort((a, b) => area(b) - area(a))[0];
      signal("layout-shift", "states", ["nielsen:1"], moved ? element(moved) : document.documentElement,
             cls.toFixed(3), "0.1");
    }
  }

  // broken-image: an image whose load ended without a picture; decode() rejecting confirms it
  for (const img of visible.filter(el => el.localName === "img")) {
    if (!img.complete || img.naturalWidth > 0 || !img.getAttribute("src") && !img.getAttribute("srcset")) continue;
    try {
      await img.decode();
    } catch {
      signal("broken-image", "content", ["nielsen:9"], img, "0 px natural width", "a decoded picture");
    }
  }

  // offscreen: a control wholly outside the page sideways, or covered by another element at its center point; the
  // center is tested only inside the viewport, where hit testing reaches
  for (const el of controls) {
    const r = el.getBoundingClientRect(), box = page(r);
    if (box.right <= 0 || box.left >= pageWidth) {
      const off = box.right <= 0 ? -box.right : box.left - pageWidth;
      signal("offscreen", "responsive", ["nielsen:7"], el, `${Math.round(off)} px outside the page`, "0 px");
      continue;
    }
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    const hit = document.elementFromPoint(x, y);
    if (!hit || hit === el || el.contains(hit) || hit.contains(el)) continue;
    if (Array.from(el.labels || []).some(label => label.contains(hit))) continue;
    signal("offscreen", "responsive", ["nielsen:7"], el, "covered at its center", "uncovered",
           masked(hit) ? {} : {other: anchor(hit)});
  }

  // near-miss alignment: sibling blocks (in normal flow, not floated) whose start edges differ by 1-3 px, or, when
  // both reach the parent's content end (full-width blocks), whose end edges do; the ragged end of blocks sized to
  // their content and the edges of blocks centered on one another are left alone. Of two near edges, the one fewer
  // siblings share is the one that is off (on a tie, the later one), and the signal names the nearest sibling on
  // the shared edge
  {
    const BLOCKS = new Set(["block", "flex", "grid", "table", "list-item", "flow-root"]);
    const inFlow = el => {
      const cs = style(el);
      return BLOCKS.has(cs.display) && cs.position !== "absolute" && cs.position !== "fixed" && cs.float === "none";
    };
    const groups = new Map();
    for (const el of visible) {
      if (!el.parentElement || !inFlow(el)) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      if (!groups.has(el.parentElement)) groups.set(el.parentElement, []);
      groups.get(el.parentElement).push({el, box: page(r)});
    }
    for (const [parent, blocks] of groups) {
      if (blocks.length < 2) continue;
      const ps = style(parent), pb = page(parent.getBoundingClientRect());
      const rtl = ps.direction === "rtl";
      const end = rtl ? pb.left + parent.clientLeft + parseFloat(ps.paddingLeft)
        : pb.left + parent.clientLeft + parent.clientWidth - parseFloat(ps.paddingRight);
      const misses = new Map(); // block -> [[side, px, the sibling it almost meets]]
      for (const [side, start] of [["left", !rtl], ["right", rtl]]) {
        const edge = b => b.box[side];
        const sharing = start ? blocks : blocks.filter(b => Math.abs(edge(b) - end) < 3.5);
        if (sharing.length < 2) continue;
        const count = new Map(), first = new Map();
        sharing.forEach((b, i) => {
          const k = Math.round(edge(b));
          count.set(k, (count.get(k) || 0) + 1);
          if (!first.has(k)) first.set(k, i);
        });
        // a key outranks another when more blocks share it, or as many and it comes first
        const outranks = (a, b) => count.get(a) > count.get(b) || count.get(a) === count.get(b) && first.get(a) < first.get(b);
        sharing.forEach((b, i) => {
          const k = Math.round(edge(b));
          let best = null;
          sharing.forEach((o, j) => {
            const ko = Math.round(edge(o)), d = Math.abs(edge(b) - edge(o));
            if (ko === k || d < 0.5 || d >= 3.5 || !outranks(ko, k)) return;
            if (Math.abs(b.box.left + b.box.right - o.box.left - o.box.right) < 1) return; // centered on one another
            const near = Math.abs(i - j) - (j < i ? 0.5 : 0);
            if (!best || outranks(ko, best.k) || ko === best.k && near < best.near) best = {k: ko, near, o, d};
          });
          if (best) {
            if (!misses.has(b)) misses.set(b, []);
            misses.get(b).push([side, best.d, best.o]);
          }
        });
      }
      for (const b of blocks) {
        const sides = misses.get(b);
        if (!sides) continue;
        signal("near-miss-alignment", "layout", ["ds:near-miss-alignment"], b.el,
               `${sides.map(([side, d]) => `${side} edge ${Math.round(d)} px`).join(" and ")} off`,
               "aligned, or more than 3 px apart", {other: anchor(sides[0][2].el)});
      }
    }
  }

  // off-scale spacing: a margin, padding, or gap whose length is not on the project's scale. The values are the
  // computed ones (CSS Typed OM), so an auto margin, a percentage, and a calc() are no step on any scale; a negative
  // margin is measured by its size. The scale is the one given (DESIGN.md's tokens), or the five values the page uses
  // most; lengths are compared to the half pixel
  if (Array.isArray(opts.spacing) || opts.spacing === "page") {
    const SPACING = ["margin-top", "margin-right", "margin-bottom", "margin-left", "padding-top", "padding-right",
                     "padding-bottom", "padding-left", "row-gap", "column-gap"];
    const step = v => Math.round(Math.abs(v) * 2) / 2;
    const used = [], uses = new Map();
    for (const el of visible) {
      const computed = el.computedStyleMap();
      const lengths = [];
      for (const prop of SPACING) {
        const v = computed.get(prop);
        if (!(v instanceof CSSUnitValue) || v.unit !== "px" || v.value === 0) continue;
        lengths.push([prop, v.value]);
        uses.set(step(v.value), (uses.get(step(v.value)) || 0) + 1);
      }
      if (lengths.length) used.push([el, lengths]);
    }
    const scale = (Array.isArray(opts.spacing)
      ? opts.spacing.map(Number).filter(v => Number.isFinite(v) && v !== 0).map(step)
      : Array.from(uses).sort((a, b) => b[1] - a[1] || a[0] - b[0]).slice(0, 5).map(([v]) => v)).sort((a, b) => a - b);
    if (scale.length) {
      const on = new Set(scale);
      for (const [el, lengths] of used) {
        const off = lengths.filter(([, v]) => !on.has(step(v)));
        if (!off.length) continue;
        signal("off-scale-spacing", "spacing", ["ds:off-scale-spacing"], el,
               off.map(([prop, v]) => `${prop} ${+v.toFixed(1)} px`).join(", "), `on the scale: ${scale.join(", ")} px`);
      }
    }
  }

  // focus-visible: each of the first 30 tab stops (positive tabindex first, then document order; one radio button
  // per group; not frames, whose focus moves into another document) must show an outline, border, or shadow change
  // when it has the focus, on itself, on its ::before or ::after, or on its parent (a :focus-within ring). Each stop is
  // measured from rest: the focus leaves the last stop first, and the transitions its focus starts are finished, so
  // the change measured is the one the page ends on. Last, the focus goes back where it was, and the scroll too
  function focusVisible() {
    const FOCUSABLE ="a[href], area[href], button, input:not([type=hidden]), select, textarea, summary, [tabindex], " +
      "[contenteditable]";
    const radioStop = el => {
      if (el.localName !== "input" || el.type !== "radio" || !el.name) return true;
      const group = Array.from(document.getElementsByName(el.name))
        .filter(r => r.localName === "input" && r.type === "radio" && r.form === el.form);
      return (group.find(r => r.checked) || group[0]) === el;
    };
    const stops = visible.filter(el => el.matches(FOCUSABLE) && el.tabIndex >= 0 && !el.matches(":disabled") &&
      !el.closest("[inert]") && !["iframe", "frame", "object", "embed"].includes(el.localName) && radioStop(el));
    const order = [...stops.filter(el => el.tabIndex > 0).sort((a, b) => a.tabIndex - b.tabIndex),
                   ...stops.filter(el => el.tabIndex === 0)].slice(0, 30);
    // what draws a focus ring, as one string: an outline that shows, each border side that shows, the shadow
    const ring = cs => {
      const outline = cs.outlineStyle === "none" ? "none"
        : `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor} ${cs.outlineOffset}`;
      const borders = ["Top", "Right", "Bottom", "Left"].map(s => {
        const kind = cs[`border${s}Style`], width = cs[`border${s}Width`];
        return kind === "none" || kind === "hidden" || parseFloat(width) === 0 ? "none" : `${kind} ${width} ${cs[`border${s}Color`]}`;
      });
      return [outline, ...borders, cs.boxShadow].join(";");
    };
    const look = el => {
      const parts = [ring(getComputedStyle(el))];
      for (const pseudo of ["::before", "::after"]) {
        const cs = getComputedStyle(el, pseudo);
        parts.push(cs.content === "none" || cs.content === "normal" ? "" : ring(cs));
      }
      if (el.parentElement) parts.push(ring(getComputedStyle(el.parentElement)));
      return parts.join("/");
    };
    const running = new Set(document.getAnimations());
    const finish = () => {
      for (const a of document.getAnimations()) {
        if (running.has(a) || !(a instanceof CSSTransition)) continue;
        try { a.finish(); } catch { /* a transition that cannot end: its value stays where it is */ }
      }
    };
    const away = () => {
      const now = document.activeElement;
      if (now && now !== document.body && now !== document.documentElement) now.blur();
    };
    const was = document.activeElement, x = scrollX, y = scrollY;
    try {
      for (const el of order) {
        away();
        finish();
        const rest = look(el);
        el.focus({preventScroll: true, focusVisible: true});
        if (document.activeElement !== el) continue;
        finish();
        if (look(el) === rest) {
          signal("focus-visible", "accessibility", ["wcag:2.4.7"], el, "no outline, border, or shadow change on focus",
                 "a visible focus indicator");
        }
      }
    } finally {
      away();
      if (was && was !== document.body && was !== document.documentElement && was.isConnected) {
        was.focus({preventScroll: true});
      }
      finish();
      if (scrollX !== x || scrollY !== y) scrollTo({left: x, top: y, behavior: "instant"});
    }
  }
  if (opts.focus !== false) focusVisible();

  return signals;
}

if (typeof module === "object" && module.exports) module.exports = {runChecks};
