"""Temporary test: save raw X scraper output and Apify usage for drafting the X feed. Not for merging."""

import json
import os
import urllib.request

TOKEN = os.environ["APIFY_TOKEN"]
OUT = "preview"


def call(url: str, body: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


os.makedirs(OUT, exist_ok=True)
items = call(
    "https://api.apify.com/v2/acts/apidojo~tweet-scraper/run-sync-get-dataset-items?timeout=240",
    {"twitterHandles": ["n_kozodoi"], "maxItems": 20, "sort": "Latest"},
    timeout=280,
)
with open(f"{OUT}/x_raw.json", "w") as f:
    json.dump(items, f, indent=1, ensure_ascii=False)
print(f"saved {len(items)} raw items")

# what the account is allowed and has used this month, and what each feed's last run cost
limits = call("https://api.apify.com/v2/users/me/limits")["data"]
usage = {
    "plan_monthly_usage_limit_usd": limits.get("limits", {}).get("maxMonthlyUsageUsd"),
    "current_month_usage_usd": limits.get("current", {}).get("monthlyUsageUsd"),
    "cycle": limits.get("monthlyUsageCycle"),
}
me = call("https://api.apify.com/v2/users/me")["data"]
usage["plan"] = (me.get("plan") or {}).get("id")
for actor in ["harvestapi~linkedin-profile-posts", "apidojo~tweet-scraper"]:
    last = call(f"https://api.apify.com/v2/acts/{actor}/runs/last")["data"]
    usage[actor] = {
        "status": last.get("status"),
        "usage_usd": last.get("usageTotalUsd"),
        "charged_events": last.get("chargedEventCounts"),
        "pricing": (last.get("pricingInfo") or {}).get("pricingModel"),
    }
with open(f"{OUT}/apify_usage.json", "w") as f:
    json.dump(usage, f, indent=1)
print(json.dumps(usage, indent=1))
