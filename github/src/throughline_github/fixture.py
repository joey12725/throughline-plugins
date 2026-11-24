"""An in-memory GitHub API over a recorded JSON dataset (GITHUB_FIXTURE).

It implements the subset of REST endpoints the plugin calls, with GitHub's
query semantics (search qualifiers, since filters, Link pagination), so tests
and the contract kit run the real code paths without the network.
"""

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

BASE = "https://api.github.com"


def _at(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _range(value):
    """A search date qualifier value: >=X, >X, <=X, A..B -> (lo, hi)."""
    if value.startswith(">="):
        return _at(value[2:]), None
    if value.startswith(">"):
        return _at(value[1:]), None
    if value.startswith("<="):
        return None, _at(value[2:])
    if ".." in value:
        lo, hi = value.split("..", 1)
        return (_at(lo) if lo != "*" else None), (_at(hi) if hi != "*" else None)
    return _at(value), _at(value)


def _within(value, bounds):
    lo, hi = bounds
    at = _at(value)
    return (lo is None or at >= lo) and (hi is None or at <= hi)


def _qualifiers(q):
    out = {}
    for token in q.split():
        if ":" in token:
            key, value = token.split(":", 1)
            out[key] = value
    return out


def fixture_transport(path, *, calls=None, rate_limit_once=False):
    with open(path) as f:
        data = json.load(f)
    login = data["user"]["login"]
    repos = {r["full_name"]: r for r in data.get("repos", [])}
    state = {"limited": rate_limit_once}

    def all_commits():
        for repo, commits in data.get("commits", {}).items():
            for commit in commits:
                yield repo, commit
        for commit in data.get("foreign_commits", []):
            yield commit["repository"]["full_name"], commit

    def page(request, items, wrap=None):
        params = request.url.params
        per_page = int(params.get("per_page", 30))
        number = int(params.get("page", 1))
        chunk = items[(number - 1) * per_page : number * per_page]
        headers = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "0"}
        if number * per_page < len(items):
            query = dict(params)
            query["page"] = str(number + 1)
            headers["link"] = f'<{BASE}{request.url.path}?{urlencode(query)}>; rel="next"'
        body = wrap(chunk) if wrap else chunk
        return httpx.Response(200, json=body, headers=headers)

    def search(request, items):
        capped = items[:1000]
        return page(request, capped, lambda chunk: {"total_count": len(items), "incomplete_results": False, "items": chunk})

    def issue_key(issue):
        return f"{issue['repository']}#{issue['number']}"

    def public_issue(issue):
        out = {k: v for k, v in issue.items() if k != "repository"}
        out["repository_url"] = f"{BASE}/repos/{issue['repository']}"
        return out

    def handler(request):
        if calls is not None:
            calls.append(request)
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"message": "Requires authentication"})
        if state["limited"]:
            state["limited"] = False
            return httpx.Response(403, json={"message": "You have exceeded a secondary rate limit."}, headers={"retry-after": "3"})
        path = request.url.path
        params = request.url.params
        parts = path.strip("/").split("/")
        if path == "/user":
            return httpx.Response(200, json=data["user"])
        if path == "/user/repos":
            return page(request, sorted(repos.values(), key=lambda r: r["full_name"]))
        if path == "/user/starred":
            stars = sorted(data.get("stars", []), key=lambda s: s["starred_at"], reverse=True)
            if "star+json" not in request.headers.get("accept", ""):
                stars = [s["repo"] for s in stars]
            return page(request, stars)
        if path == "/search/commits":
            q = _qualifiers(params["q"])
            bounds = _range(q.get("committer-date", ">=1970-01-01T00:00:00Z"))
            items = [
                dict(commit, repository={"full_name": repo, "private": repos.get(repo, {}).get("private", False)}, score=1.0)
                for repo, commit in all_commits()
                if (commit.get("author") or {}).get("login") == q.get("author")
                and _within(commit["commit"]["committer"]["date"], bounds)
            ]
            items.sort(key=lambda c: _at(c["commit"]["committer"]["date"]), reverse=params.get("order") != "asc")
            return search(request, items)
        if path == "/search/issues":
            q = _qualifiers(params["q"])
            bounds = _range(q.get("updated", ">=1970-01-01T00:00:00Z"))
            items = []
            for issue in data.get("issues", []):
                is_pr = "pull_request" in issue
                if q.get("type") == "pr" and not is_pr or q.get("type") == "issue" and is_pr:
                    continue
                if not _within(issue["updated_at"], bounds):
                    continue
                key = issue_key(issue)
                if "author" in q and issue["user"]["login"] != q["author"]:
                    continue
                if "reviewed-by" in q and not any(r["user"]["login"] == q["reviewed-by"] for r in data.get("reviews", {}).get(key, [])):
                    continue
                if "commenter" in q and not any(
                    c["user"]["login"] == q["commenter"]
                    for c in data.get("issue_comments", {}).get(key, []) + data.get("review_comments", {}).get(key, [])
                ):
                    continue
                items.append(public_issue(issue))
            items.sort(key=lambda i: _at(i["updated_at"]), reverse=params.get("order") != "asc")
            return search(request, items)
        if len(parts) >= 4 and parts[0] == "repos":
            repo = f"{parts[1]}/{parts[2]}"
            rest = parts[3:]
            since = _at(params["since"]) if "since" in params else None
            if rest == ["commits"]:
                if repo not in repos:
                    return httpx.Response(404, json={"message": "Not Found"})
                items = [
                    c for c in data.get("commits", {}).get(repo, [])
                    if ("author" not in params or (c.get("author") or {}).get("login") == params["author"])
                    and (since is None or _at(c["commit"]["committer"]["date"]) >= since)
                ]
                items.sort(key=lambda c: _at(c["commit"]["committer"]["date"]), reverse=True)
                return page(request, items)
            if rest == ["releases"]:
                items = sorted(data.get("releases", {}).get(repo, []), key=lambda r: r["created_at"], reverse=True)
                return page(request, items)
            if len(rest) == 3 and rest[0] in ("issues", "pulls"):
                key = f"{repo}#{rest[1]}"
                source = {"comments": "issue_comments" if rest[0] == "issues" else "review_comments", "reviews": "reviews"}.get(rest[2])
                if source is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                items = data.get(source, {}).get(key, [])
                if since is not None:
                    items = [i for i in items if _at(i.get("updated_at") or i.get("submitted_at")) >= since]
                return page(request, items)
        return httpx.Response(404, json={"message": "Not Found", "path": path, "login": login})

    return httpx.MockTransport(handler)
