"""
Generate a Google Scholar SVG card (algolia-themed) with total citations,
h-index, and a citations-per-year bar chart.

Google Scholar aggressively rate-limits or 403-blocks datacenter IPs (e.g. CI
runners). To get a reliable read the script prefers the SerpApi Google Scholar
Author API when a SERPAPI_KEY is set, and otherwise scrapes the public profile
page directly (rotating User-Agents, retrying with backoff, and falling back to
a pool of free public HTTP proxies). If every attempt still fails the script
exits without overwriting the existing card, so the last good version is kept.

Environment variables:
    SCHOLAR_ID    Google Scholar user id (required)
    SCHOLAR_LANG  interface language code (default: en)
    SERPAPI_KEY   SerpApi key; enables the reliable API path (optional)
    OUT_DIR       output directory for the SVG + data.json (default: profile)
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.parse
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

# rotate a handful of realistic desktop User-Agents to look less like a bot
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

# overall wall-clock budget for the proxy fallback loop (dead proxies can hang);
# generous because this runs once a day and reliability matters more than speed
PROXY_BUDGET_SECONDS = 900

# free proxy list endpoints (plain text, one host:port per line)
PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]


def _build_request(url: str) -> urllib.request.Request:
    """Build a Scholar request with a random User-Agent and browser-like headers"""
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _looks_valid(html: str) -> bool:
    """Return True if the HTML is a real profile page and not a CAPTCHA/block page"""
    if re.search(r"captcha|unusual traffic|not a robot", html, re.I):
        return False
    return 'gsc_rsb_std">' in html


def _fetch(url: str, proxy: str | None, timeout: int) -> str:
    """
    Fetch a URL, optionally through an HTTP proxy

    Parameters
    ----------
    url : str
        URL to fetch
    proxy : str | None
        Proxy as ``host:port``, or None for a direct connection
    timeout : int
        Socket timeout in seconds
    """
    handler = (
        urllib.request.ProxyHandler({"http": f"http://{proxy}", "https": f"http://{proxy}"})
        if proxy
        else urllib.request.ProxyHandler({})
    )
    opener = urllib.request.build_opener(handler)
    with opener.open(_build_request(url), timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _get_free_proxies(limit: int = 120) -> list[str]:
    """
    Collect a shuffled pool of free HTTP proxies from public list endpoints

    Parameters
    ----------
    limit : int
        Maximum number of proxies to return
    """
    proxies: set[str] = set()
    for src in PROXY_SOURCES:
        try:
            with urllib.request.urlopen(_build_request(src), timeout=20) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - a dead proxy source must not abort the run
            print(f"  proxy source failed ({src.split('/')[2]}): {exc}")
            continue
        for line in text.splitlines():
            host = line.strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}:\d{2,5}", host):
                proxies.add(host)
    pool = list(proxies)
    random.shuffle(pool)
    return pool[:limit]


def fetch_profile(user_id: str, lang: str) -> str:
    """
    Fetch the raw HTML of a Google Scholar profile page, resiliently

    Tries a direct connection first (a few retries with backoff), then rotates
    through a pool of free HTTP proxies until one returns a valid profile page.

    Parameters
    ----------
    user_id : str
        Google Scholar user id (the `user=` query parameter)
    lang : str
        Interface language code (the `hl=` query parameter)
    """
    url = f"https://scholar.google.com/citations?user={user_id}&hl={lang}"

    # direct connection first: cheap and works from residential IPs
    for attempt in range(3):
        try:
            html = _fetch(url, proxy=None, timeout=60)
            if _looks_valid(html):
                print(f"fetched directly (attempt {attempt + 1})")
                return html
            print(f"direct attempt {attempt + 1}: blocked / CAPTCHA page")
        except Exception as exc:  # noqa: BLE001 - fall through to proxy retries
            print(f"direct attempt {attempt + 1} failed: {exc}")
        time.sleep(2 + random.random() * 3)

    # proxy fallback for datacenter IPs (CI runners) that Scholar 403-blocks
    proxies = _get_free_proxies()
    print(f"trying {len(proxies)} free proxies (up to {PROXY_BUDGET_SECONDS}s budget)")
    deadline = time.monotonic() + PROXY_BUDGET_SECONDS
    for i, proxy in enumerate(proxies):
        if time.monotonic() > deadline:
            print(f"proxy time budget exhausted after {i} proxies")
            break
        try:
            html = _fetch(url, proxy=proxy, timeout=15)
            if _looks_valid(html):
                print(f"fetched via proxy {proxy} (proxy {i + 1}/{len(proxies)})")
                return html
        except Exception:  # noqa: BLE001 - dead/slow proxies are expected; keep going
            continue

    raise RuntimeError("all direct and proxy fetch attempts failed")


def fetch_via_serpapi(user_id: str, lang: str, api_key: str) -> dict:
    """
    Fetch citation stats via the SerpApi Google Scholar Author API

    Returns the same shape as ``parse_profile``. SerpApi runs the Scholar query
    from its own residential-grade infrastructure, so it is not subject to the
    datacenter-IP 403s that block direct scraping from CI runners.

    Parameters
    ----------
    user_id : str
        Google Scholar user id (the SerpApi `author_id` parameter)
    lang : str
        Interface language code (the `hl` parameter)
    api_key : str
        SerpApi private API key
    """
    params = urllib.parse.urlencode(
        {
            "engine": "google_scholar_author",
            "author_id": user_id,
            "hl": lang,
            "num": "0",  # metadata only; we don't need the article list
            "api_key": api_key,
        }
    )
    url = f"https://serpapi.com/search.json?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": random.choice(USER_AGENTS)})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))

    if payload.get("error"):
        raise RuntimeError(f"SerpApi error: {payload['error']}")

    cited_by = payload.get("cited_by", {})
    table = cited_by.get("table", [])

    def _all(metric: str) -> int | None:
        # the table is a list of single-key rows, e.g. {"citations": {"all": 599, ...}}
        for row in table:
            if metric in row:
                return row[metric].get("all")
        return None

    citations, h_index = _all("citations"), _all("h_index")
    graph = cited_by.get("graph", [])
    years = [int(g["year"]) for g in graph]
    counts = [int(g["citations"]) for g in graph]

    if citations is None or h_index is None or not years:
        raise RuntimeError("SerpApi response missing expected citation fields")

    return {"citations": citations, "h_index": h_index, "years": years, "counts": counts}


def load_profile_data(user_id: str, lang: str) -> dict:
    """
    Load citation stats, preferring SerpApi when a key is set, else scraping

    Parameters
    ----------
    user_id : str
        Google Scholar user id
    lang : str
        Interface language code
    """
    api_key = os.environ.get("SERPAPI_KEY")
    if api_key:
        try:
            data = fetch_via_serpapi(user_id, lang, api_key)
            print("fetched via SerpApi")
            return data
        except Exception as exc:  # noqa: BLE001 - fall back to scraping on any SerpApi issue
            print(f"SerpApi fetch failed, falling back to scraping: {exc}")

    return parse_profile(fetch_profile(user_id, lang))


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
        data = load_profile_data(user_id, lang)
    except Exception as exc:  # noqa: BLE001 - any failure must not clobber the last good card
        print(f"WARNING: keeping existing Scholar card, fetch/parse failed: {exc}")
        sys.exit(0)

    print(f"citations: {data['citations']} | h-index: {data['h_index']} | years: {data['years']}")
    print(f"counts: {data['counts']}")

    card_width = int(os.environ.get("CARD_WIDTH", "400"))
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        f.write(render_scholar_svg(data, "Citations", width=card_width))

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
