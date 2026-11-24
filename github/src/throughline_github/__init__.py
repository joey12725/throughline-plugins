"""The token owner's own GitHub activity as a Throughline source.

Streams (each with its own cursor in the plugin cursor):

- commits: authored commits, from every accessible repository's commit list
  (`/repos/{repo}/commits?author=`) and from commit search
  (`author:<login>`, which also finds commits in repositories the token
  cannot list); search results over the 1,000-result cap are split by date;
- pull_requests / issues: those the owner opened (issue search, `updated:`);
- comments: the owner's issue and pull request comments and review comments
  (found through `commenter:` search, read per issue with `since`);
- reviews: the owner's pull request reviews (`reviewed-by:` search);
- releases: releases the owner published in accessible repositories;
- stars: starred repositories (reference channel).

The Events API is not used: it only covers 90 days, so listing and search
give the full history in backfill and the same normalization in sync.
"""

import copy
import logging
from datetime import datetime, timedelta, timezone

import httpx
from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, Record, coverage, run

from . import mapping as M
from .client import GitHub, RateLimited
from .fixture import _at, fixture_transport

log = logging.getLogger(__name__)

STREAMS = ("commits", "pull_requests", "issues", "comments", "reviews", "releases", "stars")
STREAM_CHANNEL = {
    "commits": M.COMMITS,
    "pull_requests": M.PULLS,
    "issues": M.ISSUES,
    "comments": M.COMMENTS,
    "reviews": M.REVIEWS,
    "releases": M.RELEASES,
    "stars": M.STARS,
}


def fields(**values):
    return [dict(name=k, type=v[0], description=v[1]) for k, v in values.items()]


AUTHOR_KIND = ("text", "owner (the token's user), agent (a bot account) or other")
REVISION = ("integer", "Mapping revision that produced this record")
ISSUE_FIELDS = dict(
    repo=("text", "owner/name"),
    number=("integer", "Issue or pull request number"),
    title=("text", "Title"),
    state=("text", "open, closed or merged"),
    draft=("boolean", "Draft pull request"),
    merged=("boolean", "The pull request was merged"),
    merged_at=("timestamp", "When it was merged"),
    closed_at=("timestamp", "When it was closed"),
    labels=("json", "Label names"),
    author_kind=AUTHOR_KIND,
    url=("text", "Web URL"),
    mapping_revision=REVISION,
)


def setting(env, default=None, description="", required=False):
    return Declaration(env=env, default=default, description=description, required=required)


class GitHubPlugin(Plugin):
    manifest = Manifest(
        name="github",
        version="0.1.0",
        channels=[
            Channel(
                name=M.COMMITS,
                description="Commits authored by the token owner, in every repository GitHub can find them",
                fields=fields(
                    repo=("text", "owner/name"),
                    sha=("text", "Commit SHA"),
                    author_kind=AUTHOR_KIND,
                    ai_coauthored=("boolean", "A Co-authored-by trailer names an AI assistant (Claude, Copilot, Codex, ...)"),
                    co_authors=("json", "Co-authored-by trailers: [{name, email}]"),
                    committed_at=("timestamp", "Committer date (rebases change it; happened_at is the author date)"),
                    url=("text", "Web URL"),
                    private=("boolean", "The repository is private (known when read from the repository list)"),
                    mapping_revision=REVISION,
                ),
                conversation_gap_s=3 * 3600,
            ),
            Channel(name=M.PULLS, description="Pull requests opened by the token owner; a new version when the state changes (merged, closed)", fields=fields(**ISSUE_FIELDS)),
            Channel(name=M.ISSUES, description="Issues opened by the token owner", fields=fields(**{k: v for k, v in ISSUE_FIELDS.items() if k not in ("draft", "merged", "merged_at")})),
            Channel(
                name=M.COMMENTS,
                description="The token owner's comments on issues and pull requests, including inline review comments",
                fields=fields(
                    repo=("text", "owner/name"),
                    number=("integer", "Issue or pull request number"),
                    on=("text", "issue or pull_request"),
                    comment_type=("text", "issue_comment or review_comment (inline on a diff)"),
                    path=("text", "File of an inline review comment"),
                    edited=("boolean", "The comment was edited after posting"),
                    author_kind=AUTHOR_KIND,
                    url=("text", "Web URL"),
                    mapping_revision=REVISION,
                ),
            ),
            Channel(
                name=M.REVIEWS,
                description="Pull request reviews submitted by the token owner",
                fields=fields(
                    repo=("text", "owner/name"),
                    number=("integer", "Pull request number"),
                    state=("text", "APPROVED, CHANGES_REQUESTED, COMMENTED or DISMISSED"),
                    commit_id=("text", "Commit the review was made on"),
                    own_pull_request=("boolean", "The reviewed pull request is the owner's own"),
                    author_kind=AUTHOR_KIND,
                    url=("text", "Web URL"),
                    mapping_revision=REVISION,
                ),
            ),
            Channel(
                name=M.RELEASES,
                description="Releases published by the token owner",
                fields=fields(
                    repo=("text", "owner/name"),
                    tag=("text", "Tag name"),
                    name=("text", "Release title"),
                    prerelease=("boolean", "Marked as a pre-release"),
                    author_kind=AUTHOR_KIND,
                    url=("text", "Web URL"),
                    mapping_revision=REVISION,
                ),
            ),
            Channel(
                name=M.STARS,
                description="Repositories the token owner starred",
                fields=fields(
                    repo=("text", "owner/name"),
                    description=("text", "Repository description"),
                    language=("text", "Main language"),
                    topics=("json", "Repository topics"),
                    url=("text", "Web URL"),
                    mapping_revision=REVISION,
                ),
                is_reference=True,
            ),
        ],
        settings={
            "api_url": setting("GITHUB_API_URL", "https://api.github.com", "REST API base URL (GitHub Enterprise: https://HOST/api/v3)"),
            "fixture": setting("GITHUB_FIXTURE", None, "Recorded dataset served instead of the API (tests)"),
            "since": setting("GITHUB_SINCE", "2008-01-01", "Earliest activity read on an empty cursor"),
            "streams": setting("GITHUB_STREAMS", ",".join(STREAMS), "Comma list of streams to read"),
            "repos": setting("GITHUB_REPOS", "auto", "auto (every repository the token can list) or a comma list of owner/name for per-repo commits and releases"),
            "overlap_h": setting("GITHUB_OVERLAP_H", "24", "Hours re-read before each stream's cursor (search indexing lags)"),
            "per_page": setting("GITHUB_PER_PAGE", "100", "Items per API page; one checkpoint per page"),
            "max_wait_s": setting("GITHUB_MAX_WAIT_S", "900", "Longest rate-limit wait; longer stops the run at its last checkpoint"),
        },
        secrets={
            "token": setting("GITHUB_TOKEN", None, "Fine-grained personal access token (read-only)", required=True),
        },
        run_mode="poll",
        default_interval_s=3600,
        link_types=["part_of", "represents", "saved"],
    )

    def __init__(self, transport=None, sleep=None, clock=None):
        super().__init__()
        if transport is None and self.settings["fixture"]:
            transport = fixture_transport(self.settings["fixture"])
        self.transport = transport
        self.sleep = sleep
        self.clock = clock
        self.overlap = timedelta(hours=float(self.settings["overlap_h"]))
        self.streams = [s.strip() for s in self.settings["streams"].split(",") if s.strip()]
        unknown = set(self.streams) - set(STREAMS)
        if unknown:
            raise ValueError("unknown GITHUB_STREAMS: " + ", ".join(sorted(unknown)))
        host = httpx.URL(self.settings["api_url"]).host
        self.ref_prefix = "github" if host in ("api.github.com", "github.com") else host

    def client(self):
        kwargs = {}
        if self.sleep:
            kwargs["sleep"] = self.sleep
        if self.clock:
            kwargs["clock"] = self.clock
        return GitHub(
            self.secrets["token"],
            self.settings["api_url"],
            transport=self.transport,
            max_wait_s=float(self.settings["max_wait_s"]),
            per_page=int(self.settings["per_page"]),
            **kwargs,
        )

    def records(self, cursor, mode):
        state = copy.deepcopy(cursor) if cursor else {}
        gh = self.client()
        try:
            user = gh.get("/user")  # unreachable or bad token: raise
            login = user["login"]
            if state.get("login") and state["login"] != login:
                raise ValueError("GITHUB_TOKEN belongs to a different user than this cursor")
            state["login"] = login
            self.mapper = M.Mapper(login, self.ref_prefix)
            self.state = state
            self.gh = gh
            self.seen = set()
            self._repos = None
            for stream in self.streams:
                try:
                    yield from getattr(self, "read_" + stream)()
                except RateLimited as error:
                    log.warning("stopping at the last checkpoint: %s", error)
                    for rest in self.streams[self.streams.index(stream):]:
                        yield coverage(STREAM_CHANNEL[rest], status="partial", reasons=["rate_limited"])
                    return
        finally:
            gh.close()

    # --------------------------------------------------------------- helpers

    def since(self, key):
        value = self.state.setdefault("streams", {}).get(key)
        if value:
            return _at(value) - self.overlap
        start = _at(self.settings["since"] + ("" if "T" in self.settings["since"] else "T00:00:00+00:00"))
        return start

    def advance(self, key, value):
        streams = self.state.setdefault("streams", {})
        if value and (not streams.get(key) or _at(value) > _at(streams[key])):
            streams[key] = M.utc(value)

    def checkpoint(self):
        return Checkpoint(cursor=copy.deepcopy(self.state))

    def emit(self, message, links):
        """Yield one message (once per run) and its links."""
        if message is None:
            return
        key = (message["channel"], message["external_id"])
        if key in self.seen:
            return
        self.seen.add(key)
        yield message
        for link in links:
            yield Record(kind="link", data=link)

    def repos(self):
        if self._repos is None:
            setting = self.settings["repos"].strip()
            if setting == "auto":
                self._repos = {
                    r["full_name"]: r
                    for r, _ in self.gh.paginate("/user/repos", {"affiliation": "owner,collaborator,organization_member", "sort": "full_name"})
                }
            else:
                self._repos = {name.strip(): {"full_name": name.strip()} for name in setting.split(",") if name.strip()}
        return self._repos

    def search_ranges(self, kind, q, field, start, end):
        """Split [start, end] until each search has at most 1,000 results."""
        query = f"{q} {field}:{_q(start)}..{_q(end)}"
        total = self.gh.search_total(kind, query)
        if total > 1000 and end - start > timedelta(hours=1):
            middle = start + (end - start) / 2
            yield from self.search_ranges(kind, q, field, start, middle)
            yield from self.search_ranges(kind, q, field, middle + timedelta(seconds=1), end)
        elif total:
            yield query

    def search(self, kind, q, field, key):
        """Search items sorted by `field` ascending, from the stream cursor;
        yields (item, page_done)."""
        start = self.since(key)
        end = datetime.now(timezone.utc) + timedelta(days=1)
        sort = "committer-date" if field == "committer-date" else "updated"
        for query in self.search_ranges(kind, q, field, start, end):
            yield from self.gh.paginate(f"/search/{kind}", {"q": query, "sort": sort, "order": "asc"})

    # --------------------------------------------------------------- streams

    def read_commits(self):
        login = self.mapper.login
        repo_cursor = self.state.setdefault("repo_commits", {})
        for name, repo in sorted(self.repos().items()):
            last = repo_cursor.get(name)
            since = _at(last) - self.overlap if last else self.since("commits_listing")
            pushed = repo.get("pushed_at")
            if pushed and last and _at(pushed) < since:
                continue
            newest = None
            try:
                for item, _ in self.gh.paginate(f"/repos/{name}/commits", {"author": login, "since": _q(since)}):
                    message, links = self.mapper.commit(name, item, repo.get("private"))
                    yield from self.emit(message, links)
                    date = ((item.get("commit") or {}).get("committer") or {}).get("date")
                    if date and (newest is None or _at(date) > _at(newest)):
                        newest = date
            except httpx.HTTPStatusError as error:
                if error.response.status_code in (404, 409, 451):  # gone, empty or blocked repository
                    log.info("skipping commits of %s (%s)", name, error.response.status_code)
                    continue
                raise
            if newest:
                repo_cursor[name] = M.utc(newest)
                yield self.checkpoint()
        for item, page_done in self.search("commits", f"author:{login}", "committer-date", "commit_search"):
            repo = (item.get("repository") or {}).get("full_name")
            if repo:
                private = self.repos().get(repo, {}).get("private")
                message, links = self.mapper.commit(repo, item, private)
                yield from self.emit(message, links)
            self.advance("commit_search", ((item.get("commit") or {}).get("committer") or {}).get("date"))
            if page_done:
                yield self.checkpoint()

    def _issues(self, q, key):
        for issue, page_done in self.search("issues", q, "updated", key):
            message, links = self.mapper.issue(issue)
            yield from self.emit(message, links)
            self.advance(key, issue.get("updated_at"))
            if page_done:
                yield self.checkpoint()

    def read_pull_requests(self):
        yield from self._issues(f"type:pr author:{self.mapper.login}", "pull_requests")

    def read_issues(self):
        yield from self._issues(f"type:issue author:{self.mapper.login}", "issues")

    def read_comments(self):
        login = self.mapper.login
        since = self.since("comments")
        for issue, page_done in self.search("issues", f"commenter:{login}", "updated", "comments"):
            repo, number = M.Mapper.repo_of_issue(issue), issue.get("number")
            paths = [(f"/repos/{repo}/issues/{number}/comments", "issue_comment")]
            if "pull_request" in issue:
                paths.append((f"/repos/{repo}/pulls/{number}/comments", "review_comment"))
            for path, comment_type in paths:
                for comment, _ in self.gh.paginate(path, {"since": _q(since)}):
                    if M.login_of(comment.get("user")) == login:
                        message, links = self.mapper.comment(issue, comment, comment_type)
                        yield from self.emit(message, links)
            self.advance("comments", issue.get("updated_at"))
            if page_done:
                yield self.checkpoint()

    def read_reviews(self):
        login = self.mapper.login
        for issue, page_done in self.search("issues", f"type:pr reviewed-by:{login}", "updated", "reviews"):
            repo, number = M.Mapper.repo_of_issue(issue), issue.get("number")
            for review, _ in self.gh.paginate(f"/repos/{repo}/pulls/{number}/reviews"):
                if M.login_of(review.get("user")) == login:
                    message, links = self.mapper.review(issue, review)
                    yield from self.emit(message, links)
            self.advance("reviews", issue.get("updated_at"))
            if page_done:
                yield self.checkpoint()

    def read_releases(self):
        login = self.mapper.login
        since = self.since("releases")
        newest = None
        for name, repo in sorted(self.repos().items()):
            pushed = repo.get("pushed_at")
            if pushed and _at(pushed) < since:
                continue
            try:
                for release, _ in self.gh.paginate(f"/repos/{name}/releases"):
                    created = release.get("created_at")
                    if created and _at(created) < since:
                        break  # newest first
                    if M.login_of(release.get("author")) != login:
                        continue
                    message, links = self.mapper.release(name, release)
                    yield from self.emit(message, links)
                    if message and (newest is None or message["happened_at"] > newest):
                        newest = message["happened_at"]
            except httpx.HTTPStatusError as error:
                if error.response.status_code in (404, 451):
                    continue
                raise
        if newest:
            self.advance("releases", newest)
            yield self.checkpoint()

    def read_stars(self):
        since = self.since("stars")
        newest = None
        for star, _ in self.gh.paginate(
            "/user/starred",
            {"sort": "created", "direction": "desc"},
            headers={"Accept": "application/vnd.github.star+json"},
        ):
            starred = star.get("starred_at")
            if starred and _at(starred) < since:
                break
            message, links = self.mapper.star(star)
            yield from self.emit(message, links)
            if message and (newest is None or message["happened_at"] > newest):
                newest = message["happened_at"]
        if newest:
            self.advance("stars", newest)
            yield self.checkpoint()


def _q(at):
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["GitHubPlugin", "main"]


def main(argv=None):
    run(GitHubPlugin, argv)
