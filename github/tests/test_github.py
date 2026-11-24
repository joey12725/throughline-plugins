import copy
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_github import GitHubPlugin
from throughline_github import mapping as M
from throughline_github.client import GitHub, RateLimited
from throughline_github.fixture import fixture_transport

DATASET = Path(__file__).parent / "recorded" / "github.json"
TOKEN = "github_pat_INVENTED_canary_0123456789"
ME = "invented-dev"


@pytest.fixture
def env(monkeypatch):
    for key in ("GITHUB_STREAMS", "GITHUB_REPOS", "GITHUB_SINCE", "GITHUB_API_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_FIXTURE", str(DATASET))
    monkeypatch.setenv("GITHUB_PER_PAGE", "2")
    return monkeypatch


def collect(cursor=None, **kw):
    return list(GitHubPlugin(**kw).records(cursor or {}, "backfill"))


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def links(items):
    return [i.data for i in items if isinstance(i, Record) and i.kind == "link"]


def by_id(items):
    return {m["external_id"]: m for m in messages(items)}


def test_gh_full_history_every_stream(env):
    items = collect()
    counts = Counter(m["channel"] for m in messages(items))
    assert counts == {
        M.COMMITS: 10,  # 8 listed (not the other person's or the bot's) + 2 found only by search
        M.PULLS: 2, M.ISSUES: 2, M.COMMENTS: 5, M.REVIEWS: 2, M.RELEASES: 2, M.STARS: 3,
    }
    assert len(by_id(items)) == len(messages(items))
    rows = by_id(items)
    assert all(m["author"]["value"] == ME for m in rows.values() if m["channel"] != M.STARS)
    assert all(m["fields"].get("author_kind", "owner") == "owner" for m in rows.values())
    assert TOKEN not in json.dumps(messages(items))
    cursor = [i for i in items if isinstance(i, Checkpoint)][-1].cursor
    assert cursor["login"] == ME
    assert set(cursor["streams"]) == {"commit_search", "pull_requests", "issues", "comments", "reviews", "releases", "stars"}
    assert cursor["repo_commits"]["invented-dev/invented-notes"] == "2023-05-20T08:00:00+00:00"


def test_gh_commit_shapes_and_ai_coauthors(env):
    rows = by_id(collect())
    claude = next(m for m in rows.values() if m["channel"] == M.COMMITS and "parser" in m["content"])
    assert claude["fields"]["ai_coauthored"] is True
    assert claude["fields"]["co_authors"] == [{"name": "Claude", "email": "noreply@anthropic.com"}]
    assert claude["fields"]["private"] is True
    human = next(m for m in rows.values() if m["channel"] == M.COMMITS and "typo" in m["content"])
    assert human["fields"]["ai_coauthored"] is False
    search = next(m for m in rows.values() if "Contribute invented docs" in m["content"])
    assert search["happened_at"] == "2023-08-15T11:20:00+00:00"  # +02:00 offset normalised
    assert "private" not in search["fields"]
    body = next(m for m in rows.values() if "invented search" in m["content"])
    assert body["content"] == "Commit to invented-dev/invented-notes: Add invented search\n\nLonger body explaining the invented change."
    assert body["thread"] == {"external_id": "invented-dev/invented-notes:2023-03", "title": "invented-dev/invented-notes commits 2023-03"}


def test_gh_listing_and_search_normalise_identically():
    mapper = M.Mapper(ME)
    listed = json.loads(DATASET.read_text())["commits"]["invented-dev/invented-cli"][0]
    searched = copy.deepcopy(listed)
    searched["repository"] = {"full_name": "invented-dev/invented-cli", "private": False}
    searched["score"] = 1.0
    searched["commit"]["author"]["date"] = "2023-06-01T14:00:00.000+02:00"
    searched["commit"]["committer"]["date"] = "2023-06-01T14:00:00.000+02:00"
    assert mapper.commit("invented-dev/invented-cli", listed, False) == mapper.commit("invented-dev/invented-cli", searched, False)


def test_gh_issues_comments_reviews_links(env):
    items = collect()
    rows = by_id(items)
    merged = rows["example-org/foreign-lib#7"]
    assert merged["channel"] == M.PULLS and merged["fields"]["state"] == "merged" and merged["fields"]["merged"] is True
    assert merged["ended_at"] == "2023-08-20T10:00:00+00:00" and merged["fields"]["labels"] == ["docs"]
    draft = rows["invented-dev/invented-cli#3"]
    assert draft["fields"]["draft"] is True and draft["fields"]["merged"] is False
    inline = rows["reviewcomment:5006"]
    assert inline["fields"]["comment_type"] == "review_comment" and inline["fields"]["path"] == "src/invented.py"
    assert inline["fields"]["on"] == "pull_request"
    edited = rows["issuecomment:5003"]
    assert edited["fields"]["edited"] is True and edited["fields"]["on"] == "issue"
    assert "issuecomment:5002" not in rows  # someone else's comment
    approved = rows["review:9002"]
    assert approved["fields"]["state"] == "APPROVED" and approved["fields"]["own_pull_request"] is False
    assert "review:9003" not in rows and "review:9004" not in rows  # pending, and someone else's
    assert {r["fields"]["tag"] for r in messages(items) if r["channel"] == M.RELEASES} == {"v0.1.0", "v0.2.0-rc1"}
    star = rows["star:example-org/invented-db"]
    assert star["fields"]["topics"] == ["database", "invented"] and "author" not in star
    edges = {(e["type"], e["from"]["external_id"], e["to"]["external_ref"]) for e in links(items)}
    assert ("part_of", "reviewcomment:5006", "github:invented-dev/invented-cli#4") in edges
    assert ("represents", "example-org/foreign-lib#7", "github:example-org/foreign-lib#7") in edges
    assert ("part_of", "invented-dev/invented-notes@" + "1".zfill(40), "github:invented-dev/invented-notes") in edges
    assert ("saved", "star:example-org/invented-ui", "github:example-org/invented-ui") in edges
    entity_kinds = {e["to"]["kind"] for e in links(items)}
    assert entity_kinds == {"github_repo", "github_pull_request", "github_issue"}
    assert all(e["method"] == "source" for e in links(items))


def test_gh_sync_rereads_only_the_overlap(env):
    calls = []
    first = list(GitHubPlugin(transport=fixture_transport(DATASET, calls=calls)).records({}, "backfill"))
    cursor = [i for i in first if isinstance(i, Checkpoint)][-1].cursor
    calls.clear()
    again = list(GitHubPlugin(transport=fixture_transport(DATASET, calls=calls)).records(cursor, "sync"))
    original = by_id(first)
    repeated = by_id(again)
    assert set(repeated) < set(original)
    assert all(repeated[k] == original[k] for k in repeated)  # identical: unchanged in the core
    assert len(calls) < 25


def test_gh_streams_and_repos_settings(env):
    env.setenv("GITHUB_STREAMS", "commits")
    env.setenv("GITHUB_REPOS", "invented-dev/invented-cli")
    rows = by_id(collect())
    assert {m["channel"] for m in rows.values()} == {M.COMMITS}
    env.setenv("GITHUB_STREAMS", "commits,bogus")
    with pytest.raises(ValueError):
        GitHubPlugin()


def test_gh_secondary_rate_limit_is_retried(env):
    sleeps = []
    items = collect(transport=fixture_transport(DATASET, rate_limit_once=True), sleep=sleeps.append)
    assert sleeps[0] == 3.0
    assert len(messages(items)) == 26


def test_gh_primary_limit_waits_then_stops_cleanly(env):
    now = [1_700_000_000.0]
    sleeps = []
    good = fixture_transport(DATASET)
    count = {"n": 0}

    def limited(request):
        count["n"] += 1
        response = good.handle_request(request)
        if count["n"] == 3:
            response.headers["x-ratelimit-remaining"] = "0"
            response.headers["x-ratelimit-reset"] = str(int(now[0] + 30))
        if count["n"] == 8:
            return httpx.Response(403, json={"message": "API rate limit exceeded"}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now[0] + 7200))})
        return response

    items = collect(transport=httpx.MockTransport(limited), sleep=sleeps.append, clock=lambda: now[0])
    assert sleeps[0] == 31  # waited for the reset of the primary limit
    partial = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert partial and all(p["reasons"] == ["rate_limited"] for p in partial)
    assert partial[0]["channel"] == M.COMMITS
    assert isinstance(items[-1], Record)  # no checkpoint after the stop


def test_gh_client_retries_server_errors_and_refuses_foreign_links():
    seen = []

    def flaky(request):
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(502)
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[{"full_name": "a/b"}], headers={"link": '<https://evil.example.com/user/repos?page=2>; rel="next"'})
        return httpx.Response(200, json={"login": ME})

    gh = GitHub(TOKEN, transport=httpx.MockTransport(flaky), sleep=lambda s: None)
    assert gh.get("/user") == {"login": ME}
    with pytest.raises(ValueError):
        list(gh.paginate("/user/repos"))
    gh2 = GitHub(TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "Resource not accessible"})))
    with pytest.raises(httpx.HTTPStatusError):
        gh2.get("/user")
    gh3 = GitHub(TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "5000"})), max_wait_s=60)
    with pytest.raises(RateLimited):
        gh3.get("/user")


def test_gh_search_splits_over_the_cap(env):
    env.setenv("GITHUB_STREAMS", "commits")
    env.setenv("GITHUB_REPOS", "")
    env.setenv("GITHUB_SINCE", "2020-01-01")
    seen = []

    def handler(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": ME})
        q = request.url.params["q"]
        lo, hi = q.split("committer-date:")[1].split("..")
        total = (M._at(hi) - M._at(lo)).days * 3
        seen.append((request.url.params["per_page"], total))
        return httpx.Response(200, json={"total_count": total, "incomplete_results": False, "items": []})

    collect(transport=httpx.MockTransport(handler))
    probes = [t for p, t in seen if p == "1"]
    pages = [t for p, t in seen if p == "2"]
    assert max(probes) > 1000 and pages
    assert all(t <= 1000 for t in pages)


def test_gh_unreachable_raises(env):
    def down(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(httpx.ConnectError):
        collect(transport=httpx.MockTransport(down), sleep=lambda s: None)


def test_gh_contract_kit():
    result = check_plugin(
        "throughline_github:GitHubPlugin",
        {"GITHUB_TOKEN": TOKEN, "GITHUB_FIXTURE": str(DATASET), "GITHUB_PER_PAGE": "2"},
        secrets=[TOKEN],
    )
    assert result["messages"] == 26
