"""Temporary: run the Haiku filter on X posts saved by an earlier preview, without scraping."""

import json
import sys

import fetch_x_posts as fx

data = json.load(open(sys.argv[1]))
posts, linkedin = data["x"]["posts"], data["linkedin"]["posts"]
decisions = fx.haiku_filter(posts, linkedin)
titles = {p["id"]: p["text"].splitlines()[0][:70] for p in linkedin}
for post in posts:
    d = decisions.get(post["id"], {"show": True, "reason": "ai (no verdict)", "duplicate_of": None})
    who = f"repost @{post['repost_of']['handle']}" if post.get("repost_of") else "own post"
    verdict = "SHOW" if d["show"] else f"HIDE {d['reason']}"
    print(f"{verdict:<15} | {post['date'][:10]} | {who:<22} | {post['text'][:70]!r}")
    if d.get("duplicate_of"):
        print(f"{'':15} |   duplicate of LinkedIn: {titles.get(d['duplicate_of'])!r}")
