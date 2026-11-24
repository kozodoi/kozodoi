"""Temporary test: inspect and run Apify X scrapers for @n_kozodoi. Not for merging."""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

TOKEN = os.environ["APIFY_TOKEN"]
HANDLE = "n_kozodoi"
ACTORS = ["apidojo~tweet-scraper", "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest"]
RUN = {"apidojo~tweet-scraper": {"twitterHandles": [HANDLE], "maxItems": 20, "sort": "Latest"}}


def call(url: str, body: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def show_input_schema(actor: str) -> None:
    info = call(f"https://api.apify.com/v2/acts/{actor}")["data"]
    print(f"\n===== {actor}: {info.get('title')} | users 30d: {info.get('stats', {}).get('totalUsers30Days')}")
    build_id = (info.get("taggedBuilds") or {}).get("latest", {}).get("buildId")
    build = call(f"https://api.apify.com/v2/actor-builds/{build_id}")["data"]
    schema = build.get("inputSchema") or (build.get("actorDefinition") or {}).get("input")
    if isinstance(schema, str):
        schema = json.loads(schema)
    for name, prop in (schema or {}).get("properties", {}).items():
        desc = (prop.get("description") or "").replace("\n", " ")[:110]
        print(f"  {name} ({prop.get('type')}, default={prop.get('default')!r}): {desc}")
    print(f"  required: {(schema or {}).get('required')}")


def run(actor: str, body: dict) -> None:
    print(f"\n===== RUN {actor} with {body}")
    try:
        items = call(f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?timeout=240", body, timeout=280)
    except urllib.error.HTTPError as exc:
        print(f"  HTTP {exc.code}: {exc.read()[:400]!r}")
        return
    print(f"  items: {len(items)}")
    if items:
        print(f"  keys of first item: {sorted(items[0].keys())}")
    newest = None
    for it in items:
        created = it.get("createdAt") or it.get("created_at")
        author = (it.get("author") or {}).get("userName")
        rt = it.get("isRetweet") or it.get("retweeted_tweet") or it.get("retweet")
        retweet = it.get("retweet") if isinstance(it.get("retweet"), dict) else {}
        orig = (retweet.get("author") or {}).get("userName")
        text = (it.get("text") or it.get("fullText") or "").replace("\n", " ")[:80]
        print(f"  {created} | by @{author} | repost={bool(rt)} orig=@{orig} | {text!r}")
        try:
            ts = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y")
            newest = max(newest, ts) if newest else ts
        except (TypeError, ValueError):
            pass
    if newest:
        print(f"  newest post age: {datetime.now(timezone.utc) - newest}")
    if items:
        sample = json.dumps(items[0], ensure_ascii=False)
        print(f"  first item (truncated): {sample[:1500]}")


for actor in ACTORS:
    try:
        show_input_schema(actor)
    except Exception as exc:  # noqa: BLE001 - a test script reports and moves on
        print(f"  could not read schema for {actor}: {exc}")
for actor, body in RUN.items():
    run(actor, body)
