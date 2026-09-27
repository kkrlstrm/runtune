"""The weekly RunTune card: one mobile-width image a person can read in fifteen seconds.

Layout follows a per-client weekly report card that already works in practice:
a header with a status chip (icon + label, never colour alone), three stat tiles,
one chart, then the numbered list the reader acts on. 680px wide at 2x.

The chart answers the one question review exists for — "did the things we
adopted help?" — as bars of each measured artifact's failure-rate change beyond
its control. Left of zero is better, right is worse; the label carries the sign,
so the chart reads without colour.

PNG rendering needs Playwright + Chromium (`pip install playwright && playwright
install chromium`). Without it `render_png` returns None and the channels send
text only.
"""

from __future__ import annotations

import html
import os
import re

GOOD, BAD, NEUTRAL_C, INK, MUTED, LINE = "#0ca30c", "#d03b3b", "#898781", "#0b0b0b", "#898781", "#e1e0d9"
KIND_ICON = {"constraint": "⛔", "capability": "✚", "subagent": "◆", "route": "⇄"}


def esc(s) -> str:
    """Escape, then render `code` spans the way the text channels do."""
    return re.sub(r"`([^`]+)`", r'<code>\1</code>', html.escape(str(s)))


def build(d: dict) -> str:
    """d: {title, subtitle, chip:(label, colour, icon), tiles:[(kicker, hero, sub)],
           bars:[(label, net_change)], items:[{n, kind, title, meta}], footer}"""
    label, colour, icon = d.get("chip", ("", NEUTRAL_C, "○"))
    tiles = "".join(f'<div class="tile"><div class="k">{esc(k)}</div><div class="h">{esc(h)}</div>'
                    f'<div class="s">{esc(s)}</div></div>' for k, h, s in d.get("tiles", []))
    bars = ""
    if d.get("bars"):
        span = max(0.05, max(abs(v) for _, v in d["bars"]))
        rows = []
        for name, v in d["bars"]:
            w = abs(v) / span * 46
            side = f"right:50%;width:{w:.1f}%" if v < 0 else f"left:50%;width:{w:.1f}%"
            col = GOOD if v < -0.02 else BAD if v > 0.02 else NEUTRAL_C
            rows.append(f'<div class="br"><div class="bl">{esc(name)}</div><div class="track">'
                        f'<div class="zero"></div><div class="bar" style="{side};background:{col}"></div></div>'
                        f'<div class="bv">{v * 100:+.1f} pts</div></div>')
        bars = ('<div class="sect">Adopted artifacts · failure-rate change beyond control</div>'
                '<div class="axis"><span>better ←</span><span>→ worse</span></div>' + "".join(rows))
    items = "".join(f'<div class="it"><div class="n">{i["n"]}</div><div class="t"><div>'
                    f'<span class="ki">{KIND_ICON.get(i["kind"], "·")}</span>{esc(i["title"])}</div>'
                    f'<div class="m">{esc(i.get("meta", ""))}</div></div></div>' for i in d.get("items", []))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{width:680px;background:#fcfcfb;font:16px/1.45 -apple-system,'Segoe UI',Inter,system-ui,sans-serif;
color:{INK};padding:28px 30px 26px;display:inline-block}}
.head{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.ttl{{font-size:26px;font-weight:700;letter-spacing:-.02em}} .sub{{font-size:14px;color:{MUTED};margin-top:2px}}
.chip{{margin-left:auto;font-size:13px;font-weight:700;letter-spacing:.03em;padding:5px 11px;border-radius:999px;
color:#fff;background:{colour};white-space:nowrap}}
.tiles{{display:flex;gap:10px;margin:18px 0 20px}}
.tile{{flex:1;background:#fff;border:1px solid {LINE};border-radius:10px;padding:12px 13px}}
.k{{font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.06em;font-weight:600}}
.h{{font-size:28px;font-weight:700;letter-spacing:-.02em;margin:2px 0}} .s{{font-size:13px;color:{MUTED}}}
.sect{{font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin:6px 0 8px}}
.axis{{display:flex;justify-content:space-between;font-size:12px;color:{MUTED};margin:0 70px 4px 190px}}
.br{{display:flex;align-items:center;gap:10px;margin:5px 0}} .bl{{width:180px;font-size:13px;overflow:hidden;
white-space:nowrap;text-overflow:ellipsis}} .track{{flex:1;height:14px;position:relative}}
.zero{{position:absolute;left:50%;top:-3px;bottom:-3px;border-left:1px dashed {MUTED}}}
.bar{{position:absolute;top:0;height:14px;border-radius:3px}} .bv{{width:64px;text-align:right;font-size:13px;
font-variant-numeric:tabular-nums}}
.list{{margin-top:18px}} .it{{display:flex;gap:12px;padding:10px 0;border-top:1px solid {LINE}}}
.n{{width:26px;height:26px;border-radius:50%;background:{INK};color:#fff;font-weight:700;font-size:14px;
display:flex;align-items:center;justify-content:center;flex:none}}
.t{{font-size:15px}} code{{font:13px ui-monospace,Menlo,monospace;background:#f0efea;padding:1px 4px;border-radius:4px}} .ki{{margin-right:6px}} .m{{font-size:13px;color:{MUTED}}}
.foot{{margin-top:14px;font-size:14px;background:#fff;border:1px solid {LINE};border-radius:10px;padding:10px 13px}}
</style></head><body>
<div class="head"><div><div class="ttl">{esc(d.get("title", "RunTune"))}</div>
<div class="sub">{esc(d.get("subtitle", ""))}</div></div>
<div class="chip"><span>{esc(icon)}</span> {esc(label)}</div></div>
<div class="tiles">{tiles}</div>{bars}
<div class="list"><div class="sect">Proposals · reply with the numbers to approve</div>{items}</div>
<div class="foot">{esc(d.get("footer", ""))}</div>
</body></html>"""


def render_png(html_text: str, out_path: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    src = out_path[:-4] + ".html"
    with open(src, "w") as f:
        f.write(html_text)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox"])
            page = b.new_page(viewport={"width": 680, "height": 400}, device_scale_factor=2)
            page.goto("file://" + os.path.abspath(src))
            page.wait_for_timeout(150)
            page.locator("body").screenshot(path=out_path)
            b.close()
        return out_path
    except Exception:  # noqa: BLE001 - a card that fails to render must not stop the digest
        return None
