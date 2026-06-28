"""
Generate a Google Scholar SVG card (algolia-themed) with total citations,
h-index, and a citations-per-year bar chart.

Scrapes the public Scholar profile page (as the referenced PHP script does).
Google Scholar may rate-limit or serve a CAPTCHA to datacenter IPs (e.g. CI
runners); in that case the script exits without overwriting the existing card,
so the last good version is preserved.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

THEME = {
    "title_color": "00AEFF",
    "icon_color": "2DDE98",
    "text_color": "FFFFFF",
    "bg_color": "050F2C",
    "border_color": "1F3A68",
    "muted_color": "8AA0C0",
    "bar_color": "2EA8FF",
}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch_profile(user_id: str, lang: str) -> str:
    """
    Fetch the raw HTML of a Google Scholar profile page

    Parameters
    ----------
    user_id : str
        Google Scholar user id (the `user=` query parameter)
    lang : str
        Interface language code (the `hl=` query parameter)
    """
    url = f"https://scholar.google.com/citations?user={user_id}&hl={lang}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_profile(html: str) -> dict:
    """
    Extract total citations, h-index, and per-year citation counts from HTML

    Parameters
    ----------
    html : str
        Raw HTML of the Scholar profile page
    """
    if re.search(r"captcha|unusual traffic|not a robot", html, re.I):
        raise RuntimeError("Google Scholar returned a CAPTCHA / rate-limit page")

    std = [int(x) for x in re.findall(r'gsc_rsb_std">(\d+)', html)]
    if len(std) < 3:
        raise RuntimeError("could not parse the citation summary table")
    citations, h_index = std[0], std[2]

    years = [int(y) for y in re.findall(r"gsc_g_t[^>]*>(\d{4})", html)]
    counts = [int(c) for c in re.findall(r'gsc_g_al">(\d+)', html)]
    # align series lengths; missing leading years are zero-citation years
    if len(counts) < len(years):
        counts = [0] * (len(years) - len(counts)) + counts
    elif len(counts) > len(years):
        counts = counts[-len(years):]

    if not years:
        raise RuntimeError("could not parse the per-year histogram")

    return {"citations": citations, "h_index": h_index, "years": years, "counts": counts}


def render_scholar_svg(data: dict, title: str, width: int = 320, height: int = 165) -> str:
    """
    Render the Google Scholar SVG card with a citations-per-year bar chart

    Parameters
    ----------
    data : dict
        Parsed profile data with keys 'citations', 'h_index', 'years', 'counts'
    title : str
        Card title
    width : int
        Card width in pixels
    height : int
        Card height in pixels
    """
    t = THEME
    years, counts = data["years"], data["counts"]

    left, right = 26, width - 18
    baseline_y, top_y = height - 37, 60  # year labels land at baseline+14, matching other cards
    chart_w = right - left
    n = len(years)
    slot = chart_w / n
    bar_w = min(34, slot * 0.7)
    max_count = max(counts) or 1
    usable_h = baseline_y - top_y - 12  # leave headroom for count labels

    bars = []
    for i, (year, count) in enumerate(zip(years, counts)):
        bar_h = count / max_count * usable_h
        cx = left + slot * (i + 0.5)
        bx = cx - bar_w / 2
        by = baseline_y - bar_h
        bars.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
            f'rx="2.5" fill="#{t["bar_color"]}"/>'
        )
        bars.append(
            f'<text x="{cx:.1f}" y="{by - 4:.1f}" fill="#{t["text_color"]}" font-size="10" '
            f'text-anchor="middle">{count}</text>'
        )
        bars.append(
            f'<text x="{cx:.1f}" y="{baseline_y + 14:.1f}" fill="#{t["muted_color"]}" font-size="10" '
            f'text-anchor="middle">{year}</text>'
        )

    # one-line summary in the top-right corner
    summary = (
        f'<text x="{right}" y="38" text-anchor="end" font-size="11">'
        f'<tspan fill="#{t["muted_color"]}">Total: </tspan>'
        f'<tspan font-weight="700" fill="#{t["icon_color"]}">{data["citations"]}</tspan>'
        f'<tspan fill="#{t["muted_color"]}">  |  h-index: </tspan>'
        f'<tspan font-weight="700" fill="#{t["icon_color"]}">{data["h_index"]}</tspan>'
        f"</text>"
    )

    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" font-family="\'Segoe UI\',Ubuntu,Sans-Serif">'
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" '
        f'fill="#{t["bg_color"]}" stroke="#{t["border_color"]}"/>'
        f'<text x="22" y="40" fill="#{t["title_color"]}" font-weight="600" font-size="18">{title}</text>'
        f"{summary}"
        f'<line x1="{left}" y1="{baseline_y}" x2="{right}" y2="{baseline_y}" stroke="#{t["border_color"]}"/>'
        + "".join(bars)
        + "</svg>"
    )


def update_json(out_dir: str, key: str, payload: dict) -> None:
    """
    Merge a section into the shared profile/data.json snapshot file

    Parameters
    ----------
    out_dir : str
        Output directory holding data.json
    key : str
        Top-level key to write the payload under (e.g. 'github' or 'scholar')
    payload : dict
        Data to store under the given key
    """
    path = os.path.join(out_dir, "data.json")
    data: dict = {}
    if os.path.exists(path):
        with open(path) as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}
    data[key] = payload
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def main() -> None:
    """Fetch the Scholar profile, render the card, and write the SVG (with fallback)"""
    user_id = os.environ["SCHOLAR_ID"]
    lang = os.environ.get("SCHOLAR_LANG", "en")
    out_dir = os.environ.get("OUT_DIR", "profile")
    out_path = os.path.join(out_dir, "scholar.svg")

    try:
        html = fetch_profile(user_id, lang)
        data = parse_profile(html)
    except Exception as exc:  # noqa: BLE001 - any failure must not clobber the last good card
        print(f"WARNING: keeping existing Scholar card, fetch/parse failed: {exc}")
        sys.exit(0)

    print(f"citations: {data['citations']} | h-index: {data['h_index']} | years: {data['years']}")
    print(f"counts: {data['counts']}")

    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        f.write(render_scholar_svg(data, "Citations"))

    update_json(
        out_dir,
        "scholar",
        {
            "citations": data["citations"],
            "h_index": data["h_index"],
            "citations_per_year": dict(zip((str(y) for y in data["years"]), data["counts"])),
        },
    )


if __name__ == "__main__":
    main()
