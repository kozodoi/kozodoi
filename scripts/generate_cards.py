"""
Generate a combined GitHub stats + top-languages SVG card.

Renders one algolia-themed SVG (stats on the left, top languages on the right)
that replicates github-readme-stats but with full control over the data, so that
stars and top languages can include repos from organizations the user
contributed to (e.g. aws-samples) in addition to their own repositories. Commits
and PRs already span org contributions via GitHub's contributionsCollection, so
they need no special handling. The card width matches the citations card so the
two render as an aligned pair.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
GRAPHQL = f"{API}/graphql"

# algolia theme (mirrors github-readme-stats/themes/index.js)
THEME = {
    "title_color": "00AEFF",
    "icon_color": "2DDE98",
    "text_color": "FFFFFF",
    "bg_color": "050F2C",
    "border_color": "1F3A68",
}

# octicon paths (16x16 viewBox) copied from github-readme-stats/src/common/icons.js
ICONS = {
    "star": '<path fill-rule="evenodd" d="M8 .25a.75.75 0 01.673.418l1.882 3.815 4.21.612a.75.75 0 01.416 1.279l-3.046 2.97.719 4.192a.75.75 0 01-1.088.791L8 12.347l-3.766 1.98a.75.75 0 01-1.088-.79l.72-4.194L.818 6.374a.75.75 0 01.416-1.28l4.21-.611L7.327.668A.75.75 0 018 .25zm0 2.445L6.615 5.5a.75.75 0 01-.564.41l-3.097.45 2.24 2.184a.75.75 0 01.216.664l-.528 3.084 2.769-1.456a.75.75 0 01.698 0l2.77 1.456-.53-3.084a.75.75 0 01.216-.664l2.24-2.183-3.096-.45a.75.75 0 01-.564-.41L8 2.694v.001z"/>',
    "commits": '<path fill-rule="evenodd" d="M1.643 3.143L.427 1.927A.25.25 0 000 2.104V5.75c0 .138.112.25.25.25h3.646a.25.25 0 00.177-.427L2.715 4.215a6.5 6.5 0 11-1.18 4.458.75.75 0 10-1.493.154 8.001 8.001 0 101.6-5.684zM7.75 4a.75.75 0 01.75.75v2.992l2.028.812a.75.75 0 01-.557 1.392l-2.5-1A.75.75 0 017 8.25v-3.5A.75.75 0 017.75 4z"/>',
    "prs": '<path fill-rule="evenodd" d="M7.177 3.073L9.573.677A.25.25 0 0110 .854v4.792a.25.25 0 01-.427.177L7.177 3.427a.25.25 0 010-.354zM3.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.25.75a2.25 2.25 0 113 2.122v5.256a2.251 2.251 0 11-1.5 0V5.372A2.25 2.25 0 011.5 3.25zM11 2.5h-1V4h1a1 1 0 011 1v5.628a2.251 2.251 0 101.5 0V5A2.5 2.5 0 0011 2.5zm1 10.25a.75.75 0 111.5 0 .75.75 0 01-1.5 0zM3.75 12a.75.75 0 100 1.5.75.75 0 000-1.5z"/>',
}

# distinct palette assigned by rank so adjacent languages stay easy to tell apart
# (GitHub's own colors make Python/TypeScript/R all blue)
PALETTE = ["#4C9AFF", "#FF9F40", "#36D399", "#F472B6", "#A78BFA", "#FBBF24", "#F87171", "#22D3EE"]


def gql(query: str, variables: dict, token: str) -> dict:
    """
    Run a GraphQL query against the GitHub API

    Parameters
    ----------
    query : str
        GraphQL query string
    variables : dict
        Variables passed to the query
    token : str
        GitHub token used for authentication
    """
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GRAPHQL,
        data=payload,
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def rest(path: str, token: str) -> dict:
    """
    Run a GET request against the GitHub REST API

    Parameters
    ----------
    path : str
        API path including query string (without the API base URL)
    token : str
        GitHub token used for authentication
    """
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def fetch_owned_repos(login: str, token: str) -> dict:
    """
    Fetch the user's own non-fork repositories with stars and language bytes

    Parameters
    ----------
    login : str
        GitHub username
    token : str
        GitHub token used for authentication
    """
    query = """
    query($login:String!,$after:String){
      user(login:$login){
        repositories(first:100, ownerAffiliations:OWNER, isFork:false, after:$after,
                     orderBy:{direction:DESC,field:STARGAZERS}){
          nodes{
            nameWithOwner
            stargazerCount
            languages(first:20, orderBy:{direction:DESC,field:SIZE}){
              edges{ size node{ name color } }
            }
          }
          pageInfo{ hasNextPage endCursor }
        }
      }
    }
    """
    repos: dict = {}
    after = None
    while True:
        data = gql(query, {"login": login, "after": after}, token)
        block = data["user"]["repositories"]
        for node in block["nodes"]:
            repos[node["nameWithOwner"]] = node
        if not block["pageInfo"]["hasNextPage"]:
            break
        after = block["pageInfo"]["endCursor"]
    return repos


def discover_org_repos(login: str, org: str, token: str) -> set:
    """
    Find repositories in an organization where the user authored commits

    Parameters
    ----------
    login : str
        GitHub username
    org : str
        Organization login to search within
    token : str
        GitHub token used for authentication
    """
    found: set = set()
    if not org:
        return found
    for page in range(1, 11):
        q = urllib.parse.quote(f"org:{org} author:{login}")
        try:
            data = rest(f"/search/commits?q={q}&per_page=100&page={page}", token)
        except urllib.error.HTTPError:
            break
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            found.add(item["repository"]["full_name"])
    return found


def fetch_repo(full_name: str, token: str) -> dict | None:
    """
    Fetch a single repository's stars and language bytes by full name

    Parameters
    ----------
    full_name : str
        Repository in 'owner/name' form
    token : str
        GitHub token used for authentication
    """
    owner, name = full_name.split("/", 1)
    query = """
    query($owner:String!,$name:String!){
      repository(owner:$owner,name:$name){
        nameWithOwner
        stargazerCount
        isFork
        languages(first:20, orderBy:{direction:DESC,field:SIZE}){
          edges{ size node{ name color } }
        }
      }
    }
    """
    data = gql(query, {"owner": owner, "name": name}, token)
    repo = data.get("repository")
    if repo is None or repo["isFork"]:
        return None
    return repo


def fetch_total_commits(login: str, token: str) -> int:
    """
    Sum all-time commit contributions across the user's contribution years

    Parameters
    ----------
    login : str
        GitHub username
    token : str
        GitHub token used for authentication
    """
    created = gql("query($login:String!){user(login:$login){createdAt}}", {"login": login}, token)
    start_year = int(created["user"]["createdAt"][:4])
    now_year = datetime.now(timezone.utc).year
    query = """
    query($login:String!,$from:DateTime!,$to:DateTime!){
      user(login:$login){
        contributionsCollection(from:$from,to:$to){ totalCommitContributions restrictedContributionsCount }
      }
    }
    """
    total = 0
    for year in range(start_year, now_year + 1):
        frm = f"{year}-01-01T00:00:00Z"
        to = f"{year}-12-31T23:59:59Z"
        data = gql(query, {"login": login, "from": frm, "to": to}, token)
        cc = data["user"]["contributionsCollection"]
        total += cc["totalCommitContributions"] + cc["restrictedContributionsCount"]
    return total


def fetch_total_prs(login: str, token: str) -> int:
    """
    Fetch the total number of pull requests authored by the user

    Parameters
    ----------
    login : str
        GitHub username
    token : str
        GitHub token used for authentication
    """
    data = gql(
        "query($login:String!){user(login:$login){pullRequests(first:1){totalCount}}}",
        {"login": login},
        token,
    )
    return data["user"]["pullRequests"]["totalCount"]


def aggregate_languages(repos: dict, hide: set) -> list:
    """
    Aggregate language byte counts across a set of repositories

    Parameters
    ----------
    repos : dict
        Mapping of repo full name to repo node with a 'languages' field
    hide : set
        Lowercased language names to exclude
    """
    totals: dict = {}
    colors: dict = {}
    for repo in repos.values():
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name.lower() in hide:
                continue
            totals[name] = totals.get(name, 0) + edge["size"]
            colors[name] = edge["node"]["color"]
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return [(name, size, colors[name]) for name, size in ranked]


def k_formatter(num: int) -> str:
    """
    Format an integer using the short notation used by github-readme-stats

    Parameters
    ----------
    num : int
        Number to format
    """
    if abs(num) >= 1000:
        s = f"{num / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    return str(num)


def render_github_svg(
    stars: int,
    commits: int,
    prs: int,
    langs: list,
    count: int,
    title: str,
    width: int = 400,
    height: int = 165,
) -> str:
    """
    Render a combined GitHub card: stats on the left, top languages on the right

    The two columns are separated by a vertical divider. Languages are shown as a
    color-dot legend with percentages (no progress bar).

    Parameters
    ----------
    stars : int
        Total stars earned
    commits : int
        Total commits
    prs : int
        Total pull requests
    langs : list
        List of (name, byte_size, color) tuples ordered by size
    count : int
        Maximum number of languages to display in the right column
    title : str
        Card title
    width : int
        Card width in pixels (shared with the citations card so both align)
    height : int
        Card height in pixels (shared with the citations card so both align)
    """
    t = THEME
    w, h = width, height
    divider_x = 214  # pushed right to give the stats column more room

    # left column: stat rows with octicons, values right-aligned before the divider
    rows = [
        ("star", "Total Stars", k_formatter(stars)),
        ("commits", "Total Commits", k_formatter(commits)),
        ("prs", "Total PRs", k_formatter(prs)),
    ]
    stats_body = []
    y = 82
    for icon, label, value in rows:
        stats_body.append(
            f'<g transform="translate(22,{y - 13})">'
            f'<svg width="16" height="16" viewBox="0 0 16 16" fill="#{t["icon_color"]}">{ICONS[icon]}</svg></g>'
        )
        stats_body.append(
            f'<text x="46" y="{y}" fill="#{t["text_color"]}" font-weight="600" font-size="14">{label}</text>'
        )
        stats_body.append(
            f'<text x="{divider_x - 22}" y="{y}" fill="#{t["text_color"]}" font-weight="700" '
            f'font-size="14" text-anchor="end">{value}</text>'
        )
        y += 30

    # divider spans only the three bullet rows (first cap height to last baseline)
    divider = f'<line x1="{divider_x}" y1="70" x2="{divider_x}" y2="143" stroke="#{t["border_color"]}"/>'

    # right column: top languages as a color-dot legend, matching the stat rows'
    # font, baselines, and spacing (no header, no bar)
    top = langs[:count]
    total = sum(size for _, size, _ in top) or 1
    dot_x, text_x = divider_x + 20, divider_x + 32
    lang_body = []
    ly = 82
    for i, (name, size, _color) in enumerate(top):
        pct = size / total * 100
        fill = PALETTE[i % len(PALETTE)]
        lang_body.append(f'<circle cx="{dot_x}" cy="{ly - 5}" r="5" fill="{fill}"/>')
        lang_body.append(
            f'<text x="{text_x}" y="{ly}" fill="#{t["text_color"]}" font-weight="600" font-size="14">{name}</text>'
        )
        lang_body.append(
            f'<text x="{w - 22}" y="{ly}" fill="#{t["text_color"]}" font-weight="700" '
            f'font-size="14" text-anchor="end">{pct:.0f}%</text>'
        )
        ly += 30

    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
        f'font-family="\'Segoe UI\',Ubuntu,Sans-Serif">'
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" fill="#{t["bg_color"]}" '
        f'stroke="#{t["border_color"]}"/>'
        f'<text x="22" y="40" fill="#{t["title_color"]}" font-weight="600" font-size="18">{title}</text>'
        + "".join(stats_body)
        + divider
        + "".join(lang_body)
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
    """Fetch data, aggregate stats and languages, and write the two SVG cards"""
    token = os.environ["GH_TOKEN"]
    login = os.environ["USERNAME"]
    org = os.environ.get("ORG", "aws-samples")
    include_extra = os.environ.get("INCLUDE_EXTRA", "").split()
    exclude = set(os.environ.get("EXCLUDE_REPOS", "").split())
    hide = {x.strip().lower() for x in os.environ.get("LANGS_HIDE", "").split(",") if x.strip()}
    langs_count = int(os.environ.get("LANGS_COUNT", "3"))
    card_width = int(os.environ.get("CARD_WIDTH", "400"))
    out_dir = os.environ.get("OUT_DIR", "profile")

    repos = fetch_owned_repos(login, token)

    org_full_names = (discover_org_repos(login, org, token) | set(include_extra)) - exclude
    org_full_names -= set(repos.keys())  # avoid double counting owned repos
    for full_name in sorted(org_full_names):
        repo = fetch_repo(full_name, token)
        if repo is not None:
            repos[repo["nameWithOwner"]] = repo

    total_stars = sum(r["stargazerCount"] for r in repos.values())
    total_commits = fetch_total_commits(login, token)
    total_prs = fetch_total_prs(login, token)
    langs = aggregate_languages(repos, hide)

    print(f"repos counted: {len(repos)} | stars: {total_stars} | commits: {total_commits} | prs: {total_prs}")
    print("top languages:", [(n, f"{s}") for n, s, _ in langs[:langs_count]])

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "github.svg"), "w") as f:
        f.write(
            render_github_svg(total_stars, total_commits, total_prs, langs, langs_count, "GitHub Stats", card_width)
        )

    total_bytes = sum(s for _, s, _ in langs) or 1
    update_json(
        out_dir,
        "github",
        {
            "stars": total_stars,
            "commits": total_commits,
            "prs": total_prs,
            "repos_counted": len(repos),
            "languages": [
                {"name": n, "bytes": s, "percentage": round(s / total_bytes * 100, 2)} for n, s, _ in langs
            ],
        },
    )


if __name__ == "__main__":
    main()
