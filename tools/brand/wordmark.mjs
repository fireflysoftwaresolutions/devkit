// Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
// Outlines the "devkit" wordmark from Manrope's real glyph contours (fontkit) and records the exact centre of
// the i's dot, where the amber spark goes. Output: assets/src/wordmark.json (font units, y down), committed, so
// render.py needs no Node dependencies. Run from a brand-kit checkout (it has fontkit and @fontsource/manrope):
//   cd <brand-kit> && npm ci && cp <devkit>/tools/brand/wordmark.mjs . && node wordmark.mjs <devkit>/assets/src/wordmark.json
import { openSync } from "fontkit";
import { writeFileSync } from "fs";

const WEIGHT = 600;          // reads well from 24 px (terminal art) to 200 px (banners)
const TRACK = -60;           // font units between letters: tight, like the Firefly wordmark
const font = openSync(`node_modules/@fontsource/manrope/files/manrope-latin-${WEIGHT}-normal.woff`);
const r = (v) => Math.round(v * 10) / 10;

let x = 0;
const paths = [];
let spark = null;
let minY = Infinity, maxY = -Infinity;
for (const ch of "devkit") {
  const g = font.glyphForCodePoint(ch.codePointAt(0));
  // split the glyph into contours so the i's dot can be dropped and measured
  const contours = [];
  for (const c of g.path.commands) {
    if (c.command === "moveTo") contours.push({ cmds: [], xs: [], ys: [] });
    contours[contours.length - 1].cmds.push(c);
    for (let i = 0; i < c.args.length; i += 2) {
      contours[contours.length - 1].xs.push(c.args[i]);
      contours[contours.length - 1].ys.push(c.args[i + 1]);
    }
  }
  for (const k of contours) {
    const top = Math.max(...k.ys);
    if (ch === "i" && Math.min(...k.ys) > font.xHeight) {   // the dot: entirely above the x-height
      spark = { cx: r(x + (Math.min(...k.xs) + Math.max(...k.xs)) / 2), cy: r(-(Math.min(...k.ys) + Math.max(...k.ys)) / 2),
                dot: r(Math.max(...k.xs) - Math.min(...k.xs)) };
      continue;
    }
    minY = Math.min(minY, Math.min(...k.ys)); maxY = Math.max(maxY, top);
    const L = { moveTo: "M", lineTo: "L", quadraticCurveTo: "Q", bezierCurveTo: "C", closePath: "Z" };
    paths.push(k.cmds.map((c) => {
      const pts = [];
      for (let i = 0; i < c.args.length; i += 2) pts.push(r(x + c.args[i]), r(-c.args[i + 1]));
      return L[c.command] + pts.join(" ");
    }).join(""));
  }
  x += g.advanceWidth + TRACK;
}
const out = { font: `Manrope ${WEIGHT}`, unitsPerEm: font.unitsPerEm, xHeight: font.xHeight, track: TRACK,
              width: r(x - TRACK), top: r(-maxY), bottom: r(-minY), spark, d: paths.join("") };
writeFileSync(process.argv[2] || "wordmark.json", JSON.stringify(out, null, 1));
console.log(`wordmark ${out.width} x ${out.bottom - out.top} units · spark at (${spark.cx}, ${spark.cy}) · dot ${spark.dot}`);
