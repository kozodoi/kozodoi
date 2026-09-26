"""
Copyright © Amazon.com and Affiliates
This code is being licensed under the terms of the Amazon Software License available at https://aws.amazon.com/asl/

Fetch the latest LinkedIn posts into profile/data.json for the kozodoi.com home page.

LinkedIn offers no API for reading your own posts, so the posts are read through
an Apify actor that scrapes the public profile while logged out (no cookies, no
LinkedIn account involved). The actor sits behind one function, `normalize_post`,
so switching to another actor means changing APIFY_ACTOR and that mapping only.

Each post's image is saved into profile/linkedin/ and served from jsDelivr,
because the signed media.licdn.com URLs the actor returns expire after a few
weeks. Images are never deleted, so a fallback snapshot baked into the site keeps
resolving after the posts it shows have rotated out of the feed.

If the run fails or returns nothing usable, the script exits without touching
data.json, so the last good posts stay on the site.

Environment variables:
    APIFY_TOKEN        Apify API token (required; the step is skipped without it)
    APIFY_ACTOR        actor id (default: harvestapi~linkedin-profile-posts)
    LINKEDIN_PROFILE   public profile URL (default: https://www.linkedin.com/in/kozodoi/)
    LINKEDIN_COUNT     number of posts to keep (default: 3)
    CDN_BASE           public base URL of the repo (default: jsDelivr, kozodoi@master)
    OUT_DIR            output directory for data.json and images (default: profile)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

DEFAULT_ACTOR = "harvestapi~linkedin-profile-posts"
DEFAULT_PROFILE = "https://www.linkedin.com/in/kozodoi/"
DEFAULT_CDN_BASE = "https://cdn.jsdelivr.net/gh/kozodoi/kozodoi@master"

# the actor is asked for a few more posts than are kept, so that reposts it
# still returns or posts without text do not leave the row short
FETCH_MARGIN = 3

RUN_TIMEOUT_SECONDS = 280


def run_actor(token: str, actor: str, profile: str, count: int) -> list[dict]:
    """
    Run the Apify actor synchronously and return its dataset items

    Parameters
    ----------
    token : str
        Apify API token
    actor : str
        Actor id in the ``user~name`` form
    profile : str
        Public LinkedIn profile URL to read posts from
    count : int
        Number of posts to request

    Returns
    -------
    list[dict]
        Raw items produced by the actor run
    """
    url = (
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        f"?timeout={RUN_TIMEOUT_SECONDS}"
    )
    body = {
        "targetUrls": [profile],
        "maxPosts": count,
        "includeReposts": False,
        "includeQuotePosts": True,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=RUN_TIMEOUT_SECONDS + 30) as resp:
        items = json.load(resp)
    if not isinstance(items, list):
        raise ValueError(f"unexpected actor response: {str(items)[:300]}")
    return items


def normalize_post(item: dict) -> dict | None:
    """
    Map one actor item onto the fields the site renders

    Parameters
    ----------
    item : dict
        Raw item from the harvestapi LinkedIn profile posts actor

    Returns
    -------
    dict | None
        The normalized post, or None when the item is not a usable post
    """
    text = (item.get("content") or "").strip()
    posted = item.get("postedAt") or {}
    if item.get("type") != "post" or not text or not posted.get("date"):
        return None

    images = item.get("postImages") or []
    video = item.get("postVideo") or {}
    engagement = item.get("engagement") or {}
    return {
        "id": str(item.get("entityId") or item["id"]),
        "url": item["linkedinUrl"].split("?")[0],
        "date": posted["date"],
        "text": text,
        "source_image": images[0]["url"] if images else video.get("thumbnailUrl"),
        "video": bool(video),
        "reactions": int(engagement.get("likes") or 0),
        "comments": int(engagement.get("comments") or 0),
        "shares": int(engagement.get("shares") or 0),
    }


def save_image(source_url: str, post_id: str, out_dir: str) -> str:
    """
    Download a post image into the repo, unless it is already there

    Parameters
    ----------
    source_url : str
        Signed media.licdn.com URL returned by the actor
    post_id : str
        LinkedIn post id, used as the file name
    out_dir : str
        Output directory; the image is written to ``<out_dir>/linkedin/``

    Returns
    -------
    str
        Path of the image relative to the repo root
    """
    rel_path = f"{out_dir}/linkedin/{post_id}.jpg"
    if not os.path.exists(rel_path):
        os.makedirs(os.path.dirname(rel_path), exist_ok=True)
        req = urllib.request.Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(rel_path, "wb") as f:
            f.write(resp.read())
    return rel_path


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
        json.dump(data, f, indent=2, ensure_ascii=False)


def main() -> None:
    """Fetch the latest posts, save their images, and write them to data.json"""
    token = os.environ.get("APIFY_TOKEN", "")
    if not token:
        print("WARNING: APIFY_TOKEN is not set, keeping existing LinkedIn posts")
        sys.exit(0)

    actor = os.environ.get("APIFY_ACTOR") or DEFAULT_ACTOR
    profile = os.environ.get("LINKEDIN_PROFILE") or DEFAULT_PROFILE
    count = int(os.environ.get("LINKEDIN_COUNT") or 3)
    cdn_base = (os.environ.get("CDN_BASE") or DEFAULT_CDN_BASE).rstrip("/")
    out_dir = os.environ.get("OUT_DIR", "profile")

    try:
        items = run_actor(token, actor, profile, count + FETCH_MARGIN)
        posts = [p for p in (normalize_post(i) for i in items) if p]
        posts.sort(key=lambda p: p["date"], reverse=True)
        posts = posts[:count]
        if not posts:
            raise ValueError(f"no usable posts among {len(items)} items")

        for post in posts:
            source = post.pop("source_image")
            post["image"] = None
            if source:
                try:
                    rel_path = save_image(source, post["id"], out_dir)
                    post["image"] = f"{cdn_base}/{rel_path}"
                except (
                    Exception
                ) as exc:  # noqa: BLE001 - a missing image must not drop the post
                    print(f"WARNING: could not save image for post {post['id']}: {exc}")
    except (
        Exception
    ) as exc:  # noqa: BLE001 - any failure must not clobber the last good posts
        print(f"WARNING: keeping existing LinkedIn posts, fetch failed: {exc}")
        sys.exit(0)

    for post in posts:
        print(f"{post['date']} | {post['reactions']} reactions | {post['text'][:60]!r}")

    update_json(out_dir, "linkedin", {"profile": profile, "posts": posts})


if __name__ == "__main__":
    main()
