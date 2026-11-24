"""
Fetch the latest X posts into profile/data.json for the kozodoi.com home page.

The site shows LinkedIn and X posts as one feed. This step reads the X side:
the author's own posts, quote posts and reposts, with each repost carrying the
original post's text, image and author.

TWO SCRAPERS, ONE RESULT
------------------------
X offers no free API for reading posts, so they are read through Apify actors,
and X breaks those often: in testing, apidojo's scraper returned "no results"
for a profile it had read minutes earlier, and kaitoeasyapi's missed nothing
then but has its own outages. Both run on every call and their posts are merged
by id, so the feed only goes stale when both fail in the same week. Both cap
free Apify accounts at a handful of runs a month, so this step runs weekly
(see the workflow) while LinkedIn runs daily; a week whose scrapers are out
of quota keeps the previous posts.

FILTERING
---------
Every post is kept in data.json with a ``show`` flag; the site renders only
the ones where it is true. Claude Haiku sets it, judging the window of recent
posts in one call: posts unrelated to AI are hidden, and so is an X post that
repeats a LinkedIn post, since the LinkedIn version carries more. Haiku is only
called when the set of posts has changed since its last verdict, so most days
cost nothing; a call costs well under a tenth of a cent. Without an API key, or
when the call fails, every post is shown except those a word-overlap check
finds repeated on LinkedIn, so the feed degrades rather than breaks.

FAILURES
--------
Nothing here may hold LinkedIn back. Any X failure, an empty result, an outage
or an unexpected error, keeps the last X posts, leaves the rest of data.json
untouched, and ends the step green with a warning. A warning also appears when
no fetch has succeeded for STALE_AFTER_DAYS, so a quiet outage gets noticed.
The one exception is a rejected or out-of-credit Apify token: that fails the
step, since the LinkedIn step uses the same token and is failing too.

Environment variables:
    APIFY_TOKEN        Apify API token (required; the step fails without it)
    ANTHROPIC_API_KEY  Claude API key for the filter (optional; see FILTERING)
    X_HANDLE           X handle without the @ (default: n_kozodoi)
    X_COUNT            number of posts to keep in the window (default: 20)
    CDN_BASE           public base URL of the repo (default: raw GitHub, master)
    OUT_DIR            output directory for data.json and images (default: profile)
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from fetch_linkedin_posts import (
    CREDENTIAL_ERRORS,
    DEFAULT_CDN_BASE,
    fail,
    load_previous_posts,
    merge_posts,
    store_images,
    update_json,
)

DEFAULT_HANDLE = "n_kozodoi"
RUN_TIMEOUT_SECONDS = 240

# the actor inputs that read a profile's latest posts, reposts included
ACTORS = {
    "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest": lambda handle, n: {
        "searchTerms": [f"from:{handle} include:nativeretweets"],
        "maxItems": n,
        "queryType": "Latest",
    },
    "apidojo~tweet-scraper": lambda handle, n: {"twitterHandles": [handle], "maxItems": n, "sort": "Latest"},
}

FILTER_MODEL = "claude-haiku-5-5"

# word-overlap fallback for duplicates, used only when Haiku is unavailable
STOPWORDS = frozenset(
    """a about after again all also am an and any are as at be because been but by can could did do does
    for from had has have he her here him his how i if in into is it its just me more most my no not now of
    on one only or our out over so some such than that the their them then there these they this to too
    up us very was we were what when where which while who why will with would you your rt via amp""".split()
)
OVERLAP_THRESHOLD = 0.6
OVERLAP_MIN_WORDS = 6
MAX_DAYS_APART = 7

# X is fetched weekly, so a week plus a missed run before anyone is warned
STALE_AFTER_DAYS = 9


def run_actor(token: str, actor: str, body: dict) -> list[dict]:
    """
    Run an Apify actor synchronously and return its dataset items

    Parameters
    ----------
    token : str
        Apify API token
    actor : str
        Actor id in the ``user~name`` form
    body : dict
        Actor input

    Returns
    -------
    list[dict]
        Raw items; an actor that found nothing returns a single error item
    """
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?timeout={RUN_TIMEOUT_SECONDS}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=RUN_TIMEOUT_SECONDS + 30) as resp:
        items = json.load(resp)
    if not isinstance(items, list):
        raise ValueError(f"unexpected actor response: {str(items)[:300]}")
    return items


def iso_date(created_at: str) -> str:
    """X's 'Tue Nov 24 17:14:10 +0000 2026' as the ISO form the LinkedIn posts use"""
    dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def clean_text(tweet: dict) -> str:
    """
    Post text with HTML entities decoded, link previews expanded, and the
    t.co links that only point at the post's own media removed
    """
    text = html.unescape(tweet.get("text") or "")
    for link in (tweet.get("entities") or {}).get("urls") or []:
        if link.get("url") and link.get("display_url"):
            text = text.replace(link["url"], link["display_url"])
    for media in (tweet.get("extendedEntities") or {}).get("media") or []:
        if media.get("url"):
            text = text.replace(media["url"], "")
    return re.sub(r"\s*https://t\.co/\S+\s*$", "", text).strip()


def first_media(tweet: dict) -> tuple[str | None, bool]:
    """The first photo or video thumbnail of a post, and whether it is a video"""
    for media in (tweet.get("extendedEntities") or {}).get("media") or []:
        if media.get("media_url_https"):
            return media["media_url_https"], media.get("type") in ("video", "animated_gif")
    return None, False


def normalize_post(item: dict, handle: str) -> dict | None:
    """
    Map one scraped post onto the shape the site renders for LinkedIn posts

    Likes become ``reactions``, replies ``comments``, reposts and quotes
    ``shares``. A repost takes the original post's text, image, counts and
    link, keeps the date it was reposted, and names the author in
    ``repost_of``. A quote post, a repost with the author's own comment, keeps
    that comment as its text, shows the quoted post as its link preview, and
    names the quoted author in ``quote_of``.
    Replies, the author's own thread continuations included, are skipped: the
    feed shows posts, not conversations.

    Parameters
    ----------
    item : dict
        Raw item from either actor
    handle : str
        The profile's handle, to drop anything an actor returned from others

    Returns
    -------
    dict | None
        The post, or None for replies, other authors and error items
    """
    if item.get("error") or not item.get("id") or item.get("isReply"):
        return None
    if ((item.get("author") or {}).get("userName") or "").lower() != handle.lower():
        return None

    original = item.get("retweeted_tweet") or item.get("retweet")
    source = original if isinstance(original, dict) else item
    image, video = first_media(source)
    post = {
        "id": str(item["id"]),
        "url": source.get("url") or item.get("url"),
        "date": iso_date(item["createdAt"]),
        "text": clean_text(source),
        "source_image": image,
        "video": video,
        "reactions": source.get("likeCount") or 0,
        "comments": source.get("replyCount") or 0,
        "shares": (source.get("retweetCount") or 0) + (source.get("quoteCount") or 0),
        "attachment": None,
        "repost_of": None,
        "quote_of": None,
    }
    if source is not item:
        author = source.get("author") or {}
        post["repost_of"] = {"name": author.get("name") or author.get("userName"), "handle": author.get("userName")}

    quoted = source.get("quoted_tweet") or source.get("quote")
    if isinstance(quoted, dict) and quoted.get("url"):
        quoted_image, _ = first_media(quoted)
        quoted_by = quoted.get("author") or {}
        quoted_author = quoted_by.get("userName")
        if quoted_author:
            post["quote_of"] = {"name": quoted_by.get("name") or quoted_author, "handle": quoted_author}
        post["attachment"] = {
            "title": clean_text(quoted)[:280],
            "source": f"@{quoted_author} on X" if quoted_author else "x.com",
            "url": quoted["url"],
            "source_image": quoted_image,
        }
    return post


def fetch_posts(token: str, handle: str, count: int) -> list[dict]:
    """
    Read the profile with every actor and merge what they return by post id

    Parameters
    ----------
    token : str
        Apify API token
    handle : str
        X handle without the @
    count : int
        Number of posts to ask each actor for

    Returns
    -------
    list[dict]
        Normalized posts from all actors that succeeded, newest first
    """
    posts: dict[str, dict] = {}
    for actor, make_input in ACTORS.items():
        try:
            items = run_actor(token, actor, make_input(handle, count))
        except urllib.error.HTTPError as exc:
            if exc.code in CREDENTIAL_ERRORS:
                fail(f"{CREDENTIAL_ERRORS[exc.code]} (HTTP {exc.code})", title="X posts not updated")
            print(f"WARNING: {actor} failed: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one actor failing must not stop the other
            print(f"WARNING: {actor} failed: {exc}")
            continue
        found = []
        for item in items:
            try:
                post = normalize_post(item, handle)
            except Exception as exc:  # noqa: BLE001 - one odd item must not cost the rest
                print(f"WARNING: skipping an item {actor} returned in an unexpected shape: {exc}")
                continue
            if post:
                found.append(post)
        print(f"{actor}: {len(found)} posts from {len(items)} items")
        if items and not found:
            # say what came back instead, so a changed or placeholder response is visible in the log
            first = items[0]
            author = (first.get("author") or {}).get("userName") if isinstance(first.get("author"), dict) else None
            summary = {k: first.get(k) for k in ("type", "id", "isReply", "message", "code") if k in first}
            print(f"  first item: author={author!r} {summary} keys={sorted(first)[:12]}")
        for post in found:
            posts.setdefault(post["id"], post)
    return sorted(posts.values(), key=lambda p: p["date"], reverse=True)


def content_words(text: str) -> set[str]:
    """Lower-cased content words, without links, handles, hashtags or stopwords"""
    text = re.sub(r"https?://\S+|\b\w+\.\w+/\S*", " ", text.lower())
    text = re.sub(r"[@#]\w+", " ", text)
    return {w for w in re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", text) if w not in STOPWORDS and len(w) > 1}


def days_apart(a: str, b: str) -> float:
    """Days between two ISO timestamps"""
    return abs(
        (datetime.fromisoformat(a.replace("Z", "+00:00")) - datetime.fromisoformat(b.replace("Z", "+00:00"))).days
    )


def overlap_duplicate(post: dict, linkedin: list[dict]) -> str | None:
    """
    Id of the LinkedIn post an X post repeats, judged on shared words

    Only the author's own posts are compared. A match needs most of the X post's
    content words to appear in a LinkedIn post from the same week; the share is
    taken of the X post because the X version is usually the cut-down one.
    """
    if post.get("repost_of"):
        return None
    words = content_words(post["text"])
    if len(words) < OVERLAP_MIN_WORDS:
        return None
    for li in linkedin:
        if days_apart(post["date"], li["date"]) > MAX_DAYS_APART:
            continue
        if len(words & content_words(li["text"])) / len(words) >= OVERLAP_THRESHOLD:
            return li["id"]
    return None


FILTER_SYSTEM = """You curate the "Latest posts" feed on the personal website of an applied AI scientist, \
which mixes his LinkedIn and X posts. For each X post, decide whether it is shown.

Hide an X post when either is true:
- not_ai: it is not about AI, machine learning, data science, or the author's own research, talks, papers \
and conferences. Politics, space photos, sport and general news are not_ai, even when posted by the author.
- duplicate: it is the author's own post (not a repost) announcing the same thing as one of his LinkedIn \
posts, such as the same paper, talk or event. Give that LinkedIn post's id. A repost of someone else is never \
a duplicate.

Everything else is shown with reason "ai". Return exactly one decision per X post id."""


def haiku_filter(posts: list[dict], linkedin: list[dict]) -> dict[str, dict]:
    """
    Ask Claude Haiku which X posts to show

    Parameters
    ----------
    posts : list[dict]
        X posts in the window
    linkedin : list[dict]
        Current LinkedIn posts, for spotting duplicates

    Returns
    -------
    dict[str, dict]
        Decision per X post id: ``show``, ``reason`` and ``duplicate_of``
    """
    from typing import Literal

    import anthropic
    from pydantic import BaseModel

    class Decision(BaseModel):
        id: str
        reason: Literal["ai", "not_ai", "duplicate"]
        duplicate_of: str | None

    class Decisions(BaseModel):
        decisions: list[Decision]

    def describe(post: dict) -> dict:
        item = {"id": post["id"], "date": post["date"][:10], "text": post["text"][:600]}
        if post.get("repost_of"):
            item["repost_of"] = "@" + str(post["repost_of"]["handle"])
        if post.get("attachment"):
            item["quotes"] = post["attachment"]["title"][:300]
        return item

    prompt = json.dumps(
        {
            "linkedin_posts": [{"id": p["id"], "date": p["date"][:10], "text": p["text"][:600]} for p in linkedin],
            "x_posts": [describe(p) for p in posts],
        },
        ensure_ascii=False,
    )
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    response = client.messages.parse(
        model=FILTER_MODEL,
        max_tokens=4096,
        output_config={"effort": "low"},
        system=FILTER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=Decisions,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"no usable verdict (stop_reason={response.stop_reason})")
    usage = response.usage
    print(f"Haiku filter: {usage.input_tokens} input / {usage.output_tokens} output tokens")
    linkedin_ids = {p["id"] for p in linkedin}
    decisions = {}
    for d in response.parsed_output.decisions:
        duplicate_of = d.duplicate_of if d.reason == "duplicate" and d.duplicate_of in linkedin_ids else None
        reason = "ai" if d.reason == "duplicate" and not duplicate_of else d.reason
        decisions[d.id] = {"show": reason == "ai", "reason": reason, "duplicate_of": duplicate_of}
    return decisions


def window_signature(posts: list[dict], linkedin: list[dict]) -> str:
    """Fingerprint of the posts a verdict was made on, to skip unchanged days"""
    ids = sorted(p["id"] for p in posts) + ["li:" + p["id"] for p in sorted(linkedin, key=lambda p: p["id"])]
    return hashlib.sha256("|".join(ids).encode()).hexdigest()[:16]


def apply_filter(posts: list[dict], linkedin: list[dict], previous: dict, out_dir: str) -> dict:
    """
    Set ``show``, ``reason`` and ``duplicate_of`` on every post, in place

    Reuses the previous verdicts when the window has not changed, asks Haiku
    when it has, and falls back to the word-overlap check without it.

    Parameters
    ----------
    posts : list[dict]
        X posts in the window
    linkedin : list[dict]
        Current LinkedIn posts
    previous : dict
        The ``filter`` section of the last snapshot
    out_dir : str
        Output directory holding data.json, for the previous verdicts

    Returns
    -------
    dict
        The ``filter`` section to store: which method ran and on what window
    """
    signature = window_signature(posts, linkedin)
    previous_by_id = {p["id"]: p for p in load_previous_posts(out_dir, "x")}
    if previous.get("signature") == signature and previous.get("method") == "haiku":
        for post in posts:
            old = previous_by_id.get(post["id"], {})
            post.update({k: old.get(k) for k in ("show", "reason", "duplicate_of")})
        if all(p.get("show") is not None for p in posts):
            print("Haiku filter: window unchanged, reusing verdicts")
            return previous

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            decisions = haiku_filter(posts, linkedin)
            for post in posts:
                # a post the model skipped is shown, rather than silently dropped
                post.update(decisions.get(post["id"], {"show": True, "reason": "ai", "duplicate_of": None}))
            return {"method": "haiku", "model": FILTER_MODEL, "signature": signature}
        except Exception as exc:  # noqa: BLE001 - the feed must not depend on the filter
            print(f"WARNING: Haiku filter failed, falling back to word overlap: {exc}")
    else:
        print("WARNING: ANTHROPIC_API_KEY is not set, filtering duplicates by word overlap only")

    for post in posts:
        duplicate_of = overlap_duplicate(post, linkedin)
        post.update(
            {
                "show": duplicate_of is None,
                "reason": "duplicate" if duplicate_of else "ai",
                "duplicate_of": duplicate_of,
            }
        )
    return {"method": "overlap", "signature": signature}


def warn_if_stale(out_dir: str) -> None:
    """Raise a run warning when X posts have not refreshed for STALE_AFTER_DAYS"""
    try:
        with open(os.path.join(out_dir, "data.json")) as f:
            fetched_at = (json.load(f).get("x") or {}).get("fetched_at")
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    if age.days >= STALE_AFTER_DAYS:
        print(f"::warning title=X posts are stale::No X fetch has succeeded for {age.days} days")


def main() -> None:
    """Update the X posts, or keep the last ones on any failure but a bad Apify token"""
    out_dir = os.environ.get("OUT_DIR", "profile")
    try:
        update_posts()
    except Exception as exc:  # noqa: BLE001 - X must never stop the LinkedIn feed
        print(f"::warning title=X posts not updated::{type(exc).__name__}: {exc}. Keeping existing posts.")
    warn_if_stale(out_dir)


def update_posts() -> None:
    """Fetch the latest posts, filter them, save their images, and write them to data.json"""
    token = os.environ.get("APIFY_TOKEN", "")
    if not token:
        fail("APIFY_TOKEN is not set, so X posts cannot be fetched", title="X posts not updated")

    handle = os.environ.get("X_HANDLE") or DEFAULT_HANDLE
    count = int(os.environ.get("X_COUNT") or 20)
    cdn_base = (os.environ.get("CDN_BASE") or DEFAULT_CDN_BASE).rstrip("/")
    out_dir = os.environ.get("OUT_DIR", "profile")

    fresh = fetch_posts(token, handle, count)
    if not fresh:
        print("WARNING: keeping existing X posts, no actor returned any")
        return

    posts = merge_posts(fresh, load_previous_posts(out_dir, "x"), count)
    try:
        with open(os.path.join(out_dir, "data.json")) as f:
            previous_filter = (json.load(f).get("x") or {}).get("filter") or {}
    except (OSError, json.JSONDecodeError):
        previous_filter = {}
    linkedin = load_previous_posts(out_dir, "linkedin")
    filter_info = apply_filter(posts, linkedin, previous_filter, out_dir)
    # hidden posts get their images too, so a post shown on a later verdict has one
    store_images(posts, cdn_base, out_dir, folder="x")

    for post in posts:
        mark = "show" if post["show"] else f"hide ({post['reason']})"
        print(f"{post['date']} | {mark:<18} | {post['text'][:60]!r}")

    update_json(
        out_dir,
        "x",
        {
            "profile": f"https://x.com/{handle}",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "filter": filter_info,
            "posts": posts,
        },
    )


if __name__ == "__main__":
    main()
