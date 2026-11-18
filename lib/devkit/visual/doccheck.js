// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// The checks of a document page or a slide (R32): the capture driver runs them on a PDF page it rendered, from the
// page's description and its picture, and gets signals in the shape checks.js gives a screen.
//
//   min-text-size  Text too small to read where the page is read. A slide (a landscape page) is projected: text
//                  under 18 pt of a 960 pt wide slide (PowerPoint's 16:9), which is 1.875% of the page's width, 36 px
//                  of the 1920 px the capture renders it at. A document (a portrait page) is read in print: text under
//                  9 pt, the 12 CSS px of the screen rule at print size. A line's text size is its box's height:
//                  PDFKit gives each line the height of its font (its largest, when the line mixes sizes), which the
//                  capture rounds out to whole picture pixels, so a size may read up to a pixel large.
//   safe-margin    A line closer to an edge than 5% of the page's width (left and right) or height (top and bottom),
//                  the action-safe area where a projector or a printer may cut it; the signal names the nearest edge.
//   overlap        Two lines whose boxes cross by more than 2 px both ways: text drawn over text. The capture rounds
//                  each box out to whole pixels, so two lines that only touch can share up to 2 px. The anchor is the
//                  line that comes first in the description, and `other` the line it crosses; nielsen:8 and the
//                  layout pass, as the screen check of the same name.
//   words-per-slide  A slide (a landscape page) with more than 40 words, the most the visual-diagnosis skill allows a
//                  slide. Words are counted as a reader reads them (Intl.Segmenter's word-like segments), so a bullet
//                  or a dash is not a word and a number is one. The description cuts a line at 120 characters (its
//                  last one an ellipsis) and holds `[redacted: <rule>]` for a line with a secret, which counts as one
//                  word: with such a line the count is a floor, measured as "at least N words". The anchor is the
//                  region the slide's lines cover; the content pass.
//   contrast       From the picture: the background is the most common color in the line's box, and the text the
//                  color farthest from it in contrast (the third such pixel, so one stray pixel does not decide).
//                  Under 4.5:1 fails, or 3:1 for large text (18 pt and up; the description holds no weight, so bold
//                  text counts by its size alone). The formula is the diagram validator's (lib/devkit/diagram.py).
//                  A line with no ink (invisible text, as a scan's text layer is) is not judged. When the most common
//                  color covers under 40% of the box, the text sits on a picture or a gradient, which has no one
//                  ratio: contrast-unknown, for review.
//
// Interface:
//
//   check(description, page) -> Signal[]
//     description  the capture's document description (devkit.visual.description/1): its `text` elements are the
//                  page's lines, each with `box` [x, y, width, height] in picture px
//     page         {size: [width, height] of the picture in px, scale: picture px per PDF point,
//                   image: {width, height, data: RGBA bytes} of the rendered picture, optional: without it, contrast
//                   is not checked}
//
// A signal is {check, pass, rule[], anchor: {element, region, selector: null}, measured, threshold}, with
// `advice: true` on contrast-unknown and `other` (the second line, as an anchor) on overlap; its anchor is the line's
// element and box (words-per-slide: no element, the region of the lines), and it holds no text of the page. A
// redacted line is painted over whole in the picture, so it shows no ink and its contrast is not judged; its size and
// place still are.
'use strict';

const SLIDE_MIN = 18 / 960; // of the page's width
const PRINT_MIN = 9; // pt
const LARGE = 18; // pt: large text needs 3:1
const MARGIN = 0.05; // of the page's width or height
const BACKGROUND_SHARE = 0.4;
const CROSS = 2; // px two line boxes may share, from rounding, before they overlap
const SLIDE_WORDS = 40;
const TEXT_LIMIT = 120; // characters of a line's text the description keeps
const CUT = '\u2026';

const segmenter = new Intl.Segmenter('und', { granularity: 'word' });
const wordsIn = (text) => {
  let n = 0;
  for (const s of segmenter.segment(text)) if (s.isWordLike) n++;
  return n;
};

const isBox = (b) => Array.isArray(b) && b.length === 4 && b.every((n) => typeof n === 'number' && Number.isFinite(n));
const round1 = (n) => +n.toFixed(1);

// The relative luminance of an [r, g, b] color and the contrast of two, as lib/devkit/diagram.py computes them.
function luminance(rgb) {
  const ch = (c) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2]);
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// The background and text colors in a box of the picture: {ratio}, {unknown: true}, or null when the box holds no ink
// or lies outside the picture.
function measureContrast(image, box) {
  const x0 = Math.max(0, Math.floor(box[0]));
  const y0 = Math.max(0, Math.floor(box[1]));
  const x1 = Math.min(image.width, Math.ceil(box[0] + box[2]));
  const y1 = Math.min(image.height, Math.ceil(box[1] + box[3]));
  if (x1 <= x0 || y1 <= y0) return null;
  const counts = new Map();
  for (let y = y0; y < y1; y++) {
    for (let x = x0; x < x1; x++) {
      const i = (y * image.width + x) * 4;
      const key = (image.data[i] << 16) | (image.data[i + 1] << 8) | image.data[i + 2];
      counts.set(key, (counts.get(key) || 0) + 1);
    }
  }
  const rgb = (key) => [(key >> 16) & 255, (key >> 8) & 255, key & 255];
  let background = null;
  let most = 0;
  for (const [key, n] of counts) {
    if (n > most) [background, most] = [key, n];
  }
  if (counts.size === 1) return null; // no ink
  if (most / ((x1 - x0) * (y1 - y0)) < BACKGROUND_SHARE) return { unknown: true };
  const behind = rgb(background);
  // each other color by its contrast with the background, as often as it occurs, highest first
  const ink = [];
  for (const [key, n] of counts) if (key !== background) ink.push([contrast(rgb(key), behind), n]);
  ink.sort((a, b) => b[0] - a[0]);
  let seen = 0;
  for (const [ratio, n] of ink) {
    seen += n;
    if (seen >= 3) return { ratio };
  }
  return { ratio: ink[ink.length - 1][0] };
}

function check(description, page) {
  if (!description || !Array.isArray(description.elements)) throw new TypeError('check takes a document description.');
  const size = page && Array.isArray(page.size) ? page.size : null;
  const scale = page && typeof page.scale === 'number' && page.scale > 0 ? page.scale : null;
  if (!size || !(size[0] > 0 && size[1] > 0) || !scale) {
    throw new TypeError('check takes the page: {size: [width, height] in px, scale: px per point}.');
  }
  const image = page.image && page.image.data && page.image.width > 0 && page.image.height > 0 ? page.image : null;
  const [width, height] = size;
  const slide = width > height;
  const lines = description.elements.filter((e) => e && e.role === 'text' && typeof e.id === 'string' && isBox(e.box));
  const signals = [];
  const signal = (check, pass, rule, e, measured, threshold, extra) => signals.push({ check, pass, rule,
    anchor: { element: e.id, region: e.box.map(Math.round), selector: null }, measured, threshold, ...extra });

  for (const e of lines) {
    const high = e.box[3];
    if (slide && high < SLIDE_MIN * width) {
      signal('min-text-size', 'typography', ['ds:min-text-size'], e, `${Math.round(high)} px at ${width} px wide`,
        `${round1(SLIDE_MIN * width)} px at ${width} px wide (18 pt on a 960 pt wide slide)`);
    } else if (!slide && high / scale < PRINT_MIN) {
      signal('min-text-size', 'typography', ['ds:min-text-size'], e, `${round1(high / scale)} pt`, `${PRINT_MIN} pt`);
    }
  }

  const across = Math.round(MARGIN * width);
  const down = Math.round(MARGIN * height);
  for (const e of lines) {
    const [x, y, w, h] = e.box;
    const gaps = [['left', x, across, 'width'], ['right', width - (x + w), across, 'width'],
      ['top', y, down, 'height'], ['bottom', height - (y + h), down, 'height']];
    const inside = gaps.filter(([, gap, margin]) => gap < margin).sort((a, b) => a[1] - b[1]);
    if (!inside.length) continue;
    const [edge, gap, margin, side] = inside[0];
    signal('safe-margin', 'layout', ['ds:safe-margin'], e,
      gap < 0 ? `past the ${edge} edge` : `${Math.round(gap)} px from the ${edge} edge`,
      `${margin} px (5% of the page ${side})`);
  }

  const crossings = [];
  const byTop = lines.map((e, i) => ({ e, i })).sort((a, b) => a.e.box[1] - b.e.box[1] || a.i - b.i);
  for (let p = 0; p < byTop.length; p++) {
    const [ax, ay, aw, ah] = byTop[p].e.box;
    for (let q = p + 1; q < byTop.length && byTop[q].e.box[1] < ay + ah; q++) {
      const [bx, by, bw, bh] = byTop[q].e.box;
      const w = Math.min(ax + aw, bx + bw) - Math.max(ax, bx);
      const h = Math.min(ay + ah, by + bh) - by;
      if (w > CROSS && h > CROSS) crossings.push([byTop[p].i, byTop[q].i].sort((a, b) => a - b).concat([w, h]));
    }
  }
  crossings.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  for (const [first, second, w, h] of crossings) {
    const other = lines[second];
    signal('overlap', 'layout', ['nielsen:8'], lines[first], `${Math.round(w)}×${Math.round(h)} px intersection`,
      `${CROSS} px`, { other: { element: other.id, region: other.box.map(Math.round), selector: null } });
  }

  if (slide && lines.length) {
    let words = 0;
    let floor = false;
    for (const e of lines) {
      const text = typeof e.text === 'string' ? e.text : '';
      if (e.redacted) {
        words += 1;
        floor = true;
      } else {
        words += wordsIn(text);
        if (Array.from(text).length >= TEXT_LIMIT && text.endsWith(CUT)) floor = true;
      }
    }
    if (words > SLIDE_WORDS) {
      let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
      for (const [x, y, w, h] of lines.map((e) => e.box)) {
        [x0, y0, x1, y1] = [Math.min(x0, x), Math.min(y0, y), Math.max(x1, x + w), Math.max(y1, y + h)];
      }
      [x0, y0, x1, y1] = [Math.floor(x0), Math.floor(y0), Math.ceil(x1), Math.ceil(y1)];
      signals.push({ check: 'words-per-slide', pass: 'content', rule: ['ds:words-per-slide'],
        anchor: { element: null, region: [x0, y0, x1 - x0, y1 - y0], selector: null },
        measured: `${floor ? 'at least ' : ''}${words} words`, threshold: `${SLIDE_WORDS} words` });
    }
  }

  if (image) {
    for (const e of lines) {
      const found = measureContrast(image, e.box);
      if (!found) continue;
      const large = e.box[3] / scale >= LARGE;
      const threshold = large ? '3:1' : '4.5:1';
      if (found.unknown) {
        signal('contrast-unknown', 'color', ['wcag:1.4.3'], e, 'no one background color behind the text', threshold,
          { advice: true });
      } else if (found.ratio < (large ? 3 : 4.5)) {
        signal('contrast', 'color', ['wcag:1.4.3'], e, `${Math.floor(found.ratio * 100) / 100}:1`, threshold);
      }
    }
  }
  return signals;
}

module.exports = { check };
