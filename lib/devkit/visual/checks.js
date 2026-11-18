// devkit's in-page visual checks. The capture driver runs them on the page state it captured, after the screenshot:
//
//   const {runChecks} = require("./checks.js");
//   const signals = await page.evaluate(runChecks, viewportWidth);
//
// runChecks is self-contained (page.evaluate sends its source alone), reads the page and never changes it: no
// scrolling, focus, or DOM writes. Each signal is {check, pass, rule[], anchor: {region, selector}, measured,
// threshold}, with `other` (the second element of an overlap, the sibling an overflow crosses, what covers a
// control), `advice: true` for the phone-size advice, and `text` (the full text of a truncation, at most 120
// characters). A region is [x, y, width, height] in page CSS px. A signal carries no name and no other page text:
// the capture driver takes the anchor's name from the redacted description and redacts `text` before any write.
async function runChecks(viewportWidth) {
  const vw = Number(viewportWidth) || window.innerWidth;
  const phone = vw < 768;
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

  // a CSS selector that matches the element alone: its id, its test id, or a path of nth-of-type steps from the
  // nearest ancestor with a unique id (from html when none has one)
  const unique = sel => { try { return document.querySelectorAll(sel).length === 1; } catch { return false; } };
  const selector = el => {
    if (el.id && unique(`#${CSS.escape(el.id)}`)) return `#${CSS.escape(el.id)}`;
    const testid = el.getAttribute("data-testid");
    if (testid && unique(`[data-testid="${CSS.escape(testid)}"]`)) return `[data-testid="${CSS.escape(testid)}"]`;
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

  // what the checks look at: rendered elements outside aria-hidden decorations
  const all = Array.from(document.body ? document.body.querySelectorAll("*") : []);
  const shown = el => el.checkVisibility({opacityProperty: true, visibilityProperty: true}) &&
    el.getClientRects().length > 0;
  const decoration = el => !!el.closest("[aria-hidden=true]");
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
  const visible = all.filter(el => shown(el) && !decoration(el));
  const controls = visible.filter(isControl);
  const texts = new Map(visible.map(el => [el, ownText(el)]).filter(([, nodes]) => nodes.length));

  // overlap: two visible text or control boxes cross by more than 2 px both ways, neither holds the other, and
  // neither is fixed; on a control it counts against target spacing (wcag:2.5.8)
  {
    const boxes = [];
    for (const el of new Set([...controls, ...texts.keys()])) {
      if (fixed(el)) continue;
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
      const moved = worst.sources.filter(s => s.node && s.node.isConnected).sort((a, b) => area(b) - area(a))[0];
      const el = moved && (moved.node.nodeType === 1 ? moved.node : moved.node.parentElement);
      signal("layout-shift", "states", ["nielsen:1"], el || document.documentElement, cls.toFixed(3), "0.1");
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
    signal("offscreen", "responsive", ["nielsen:7"], el, "covered at its center", "uncovered", {other: anchor(hit)});
  }

  return signals;
}

if (typeof module === "object" && module.exports) module.exports = {runChecks};
