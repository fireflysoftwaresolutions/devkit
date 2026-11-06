#!/usr/bin/env python3
# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Render devkit's identity: app icon, plugin logo and the README hero banners (dark and light).

Built from the Firefly brand's own elements (brand-kit: palette, Manrope, the spark and the light-trail system);
it never redraws or types the Firefly logo. Requires rsvg-convert (brew librsvg), cwebp (brew webp) and the Manrope
font (brew install --cask font-manrope). Run from anywhere:  python3 tools/brand/render.py
Outputs: assets/src/*.svg (sources), plugin/assets/{icon,logo}.png, docs/images/hero-{dark,light}.{svg,webp}
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHARCOAL, PAPER, AMBER, INK, MUTED, GOLD, STONE, GRAPHITE, LINE = (
    "#10110F", "#F3F1EB", "#FFB34A", "#272820", "#62645B", "#855414", "#BFB8AB", "#474A42", "#D8D4CA")


def spark(cx, cy, s, color=AMBER, glow=True, glow_r=None):
    """The brand spark (24-unit grid) scaled by s and centred on (cx, cy), with an optional soft glow."""
    def p(x, y):
        return f"{cx + (x - 12) * s:.1f} {cy + (y - 12) * s:.1f}"
    w = 1.5 * s
    g = (f'<circle cx="{cx}" cy="{cy}" r="{glow_r or 6.5 * s:.1f}" fill="url(#glow)"/>' if glow else "")
    rays = "".join(f'<path d="M{p(*a)} L{p(*b)}"/>' for a, b in
                   (((12, 2.5), (12, 6)), ((12, 18), (12, 21.5)), ((2.5, 12), (6, 12)), ((18, 12), (21.5, 12))))
    dots = "".join(f'<path d="M{p(x, y)} L{p(x + 0.01, y)}"/>' for x, y in ((8.5, 8.5), (15.5, 8.5), (8.5, 15.5), (15.5, 15.5)))
    return (g + f'<g fill="none" stroke="{color}" stroke-width="{w:.1f}" stroke-linecap="round" stroke-linejoin="round">'
            f'<circle cx="{cx}" cy="{cy}" r="{2.5 * s:.1f}"/>{rays}{dots}</g>')


DEFS = f"""<defs>
  <radialGradient id="glow"><stop offset="0" stop-color="{AMBER}" stop-opacity="0.45"/>
    <stop offset="0.45" stop-color="{AMBER}" stop-opacity="0.12"/><stop offset="1" stop-color="{AMBER}" stop-opacity="0"/></radialGradient>
  <linearGradient id="trail" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="{AMBER}" stop-opacity="0.2"/>
    <stop offset="1" stop-color="{AMBER}" stop-opacity="1"/></linearGradient>
  <linearGradient id="trailsoft" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="{AMBER}" stop-opacity="0"/>
    <stop offset="1" stop-color="{AMBER}" stop-opacity="0.55"/></linearGradient>
  <filter id="blur6" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter>
  <filter id="blur14" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="14"/></filter>
</defs>"""


def wordmark(x, baseline, size, ink, glow=True, spark_factor=0.125):
    """The devkit wordmark from Manrope outlines (assets/src/wordmark.json, made by wordmark.mjs), its i dotted
    by the amber spark exactly at the centre of the original dot. Returns (svg, width_px)."""
    import json
    wm = json.loads((ROOT / "assets" / "src" / "wordmark.json").read_text())
    k = size / wm["unitsPerEm"]
    sp = wm["spark"]
    sx, sy = x + sp["cx"] * k, baseline + sp["cy"] * k
    s = spark_factor * sp["dot"] * k
    svg = (f'<path transform="translate({x:.1f},{baseline:.1f}) scale({k:.5f})" d="{wm["d"]}" fill="{ink}"/>'
           + spark(round(sx, 1), round(sy, 1), round(s, 2), glow=glow, glow_r=round(9 * s, 1)))
    return svg, wm["width"] * k


def a11y(title, desc):
    """<title> and <desc> for an SVG: screen readers announce them and `dev docs check` requires them."""
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    return f"<title>{esc(title)}</title>\n<desc>{esc(desc)}</desc>"


def icon_svg(size=512):
    """App icon: a terminal prompt in paper on charcoal; the cursor's light trail ends in the amber spark."""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 512 512">
{a11y("devkit", "The devkit icon: a terminal prompt in paper on charcoal, with the cursor's light trail ending in the amber spark.")}
{DEFS}
<rect width="512" height="512" rx="116" fill="{CHARCOAL}"/>
<rect x="1.5" y="1.5" width="509" height="509" rx="115" fill="none" stroke="{GRAPHITE}" stroke-width="3"/>
<path d="M118 206 L196 268 L118 330" fill="none" stroke="{PAPER}" stroke-width="32" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M232 334 H318" stroke="{PAPER}" stroke-width="32" stroke-linecap="round"/>
<path d="M300 334 C 352 330, 382 280, 380 204" fill="none" stroke="url(#trail)" stroke-width="11" stroke-linecap="round"/>
{spark(372, 150, 7.2, glow_r=120)}
</svg>"""


def hero_svg(theme="dark", W=1600, H=520):
    bg, ink, sub, label, hair = ((CHARCOAL, PAPER, STONE, AMBER, GRAPHITE) if theme == "dark"
                                 else (PAPER, CHARCOAL, MUTED, GOLD, LINE))
    m = 96                       # left margin
    base = 300                   # wordmark baseline
    size = 188                   # wordmark size
    trails = []
    for i, (w, op, dy, bend) in enumerate(((7, 1.0, 0, 0), (4, 0.75, 46, 40), (2.5, 0.5, 92, 70), (1.6, 0.35, 140, 95))):
        trails.append(f'<path d="M{W * 0.60:.0f} {H + 6 - dy * 0.05:.0f} C {W * 0.76:.0f} {H - 50 - bend:.0f}, '
                      f'{W * 0.83:.0f} {300 - dy:.0f}, {W * 0.885:.0f} {150 + dy * 0.25:.0f}" fill="none" '
                      f'stroke="url(#trail)" stroke-opacity="{op}" stroke-width="{w}" stroke-linecap="round"/>')
    bokeh = "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{AMBER}" opacity="{o}" filter="url(#blur{b})"/>'
                    for x, y, r, o, b in ((1240, 108, 16, 0.35, 6), (1452, 300, 26, 0.18, 14), (1090, 210, 9, 0.30, 6),
                                          (1520, 120, 10, 0.25, 6), (1330, 400, 20, 0.12, 14)))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
{a11y("devkit", "devkit: Every repo in its place. Every agent on the same page. By Firefly Software Solutions Inc.")}
{DEFS}
<rect width="{W}" height="{H}" fill="{bg}"/>
{bokeh}
{''.join(trails)}
{spark(int(W * 0.885), 150, 4.2, glow_r=150)}
{wordmark(m, base, size, ink)[0]}
<text x="{m + 6}" y="{base + 78}" font-family="Manrope" font-size="38" font-weight="400" letter-spacing="-0.6" fill="{sub}">Every repo in its place. Every agent on the same page.</text>
<line x1="{m + 6}" y1="{H - 74}" x2="{m + 66}" y2="{H - 74}" stroke="{hair}" stroke-width="2"/>
<text x="{m + 6}" y="{H - 40}" font-family="Manrope" font-size="22" font-weight="650" letter-spacing="0.6" fill="{label}">by Firefly Software Solutions Inc.</text>
</svg>"""


def lockup_svg(theme="dark"):
    """Transparent lockup for terminal art: the prompt-and-spark mark + the devkıt wordmark with its spark."""
    ink = PAPER if theme == "dark" else CHARCOAL
    size, base, m = 200, 236, 300
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="300" viewBox="0 0 1100 300">
{a11y("devkit", "The devkit lockup: the prompt-and-spark mark next to the devkit wordmark.")}
{DEFS}
<g transform="translate(-6,-14) scale(0.62)">
<path d="M118 206 L196 268 L118 330" fill="none" stroke="{ink}" stroke-width="38" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M232 334 H318" stroke="{ink}" stroke-width="38" stroke-linecap="round"/>
<path d="M300 334 C 352 330, 382 280, 380 204" fill="none" stroke="url(#trail)" stroke-width="16" stroke-linecap="round"/>
{spark(372, 150, 8.5, glow=False)}
</g>
{wordmark(m, base, size, ink, glow=False, spark_factor=0.24)[0]}
</svg>"""


def run(*args):
    subprocess.run([str(a) for a in args], check=True)


def main():
    src = ROOT / "assets" / "src"
    imgs = ROOT / "docs" / "images"
    passets = ROOT / "plugin" / "assets"
    for d in (src, imgs, passets):
        d.mkdir(parents=True, exist_ok=True)
    (src / "icon.svg").write_text(icon_svg())
    run("rsvg-convert", "-w", 512, "-h", 512, src / "icon.svg", "-o", passets / "logo.png")
    run("rsvg-convert", "-w", 128, "-h", 128, src / "icon.svg", "-o", passets / "icon.png")
    run("rsvg-convert", "-w", 256, "-h", 256, src / "icon.svg", "-o", imgs / "icon.png")
    for theme in ("dark", "light"):
        svg = imgs / f"hero-{theme}.svg"
        svg.write_text(hero_svg(theme))
        png = src / f"hero-{theme}.png"
        run("rsvg-convert", "-w", 1600, "-h", 520, svg, "-o", png)
        run("cwebp", "-quiet", "-q", "92", png, "-o", imgs / f"hero-{theme}.webp")
    print("rendered:", ", ".join(str(p.relative_to(ROOT)) for p in
                                 [passets / "logo.png", passets / "icon.png", imgs / "hero-dark.webp", imgs / "hero-light.webp"]))


if __name__ == "__main__":
    main()
