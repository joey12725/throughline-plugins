"""Map GitHub API objects to Throughline messages and links.

Every mapper builds its payload from a fixed subset of the API object, so
the same commit read from the search API and from a repository listing (or a
pull request read again after an unrelated comment bumped its updated_at)
normalises to the same message and is `unchanged` on replay.
"""

import re
from datetime import timezone

from .fixture import _at

MAPPING_REVISION = 1

COMMITS = "github.commits"
PULLS = "github.pull_requests"
ISSUES = "github.issues"
COMMENTS = "github.comments"
REVIEWS = "github.reviews"
RELEASES = "github.releases"
STARS = "github.stars"

MAX_BODY = 20000
AI_PATTERN = re.compile(
    r"claude|anthropic|copilot|openai|chatgpt|codex|gpt-|cursor\s?agent|cursoragent|aider|devin|gemini|jules",
    re.IGNORECASE,
)
TRAILER = re.compile(r"^co-authored-by:\s*(.+?)\s*<([^>]*)>\s*$", re.IGNORECASE | re.MULTILINE)


def utc(value):
    return _at(value).astimezone(timezone.utc).isoformat() if value else None


def login_of(user):
    return (user or {}).get("login") if isinstance(user, dict) else None


def is_bot(user):
    login = login_of(user) or ""
    return (user or {}).get("type") == "Bot" or login.endswith("[bot]")


def author(user, name=None):
    login = login_of(user)
    if not login:
        return None
    return {"system": "github", "kind": "username", "value": login, "display_name": name or login}


def truncate(text):
    text = text or ""
    return text if len(text) <= MAX_BODY else text[:MAX_BODY] + "\n[truncated]"


def clean(fields):
    return {k: v for k, v in fields.items() if v is not None}


class Mapper:
    def __init__(self, login, ref_prefix="github"):
        self.login = login
        self.prefix = ref_prefix

    # ------------------------------------------------------------- entities

    def repo_entity(self, repo):
        return {"kind": "github_repo", "external_ref": f"{self.prefix}:{repo}", "name": repo}

    def issue_entity(self, repo, number, is_pr):
        kind = "github_pull_request" if is_pr else "github_issue"
        return {"kind": kind, "external_ref": f"{self.prefix}:{repo}#{number}", "name": f"{repo}#{number}"}

    @staticmethod
    def link(type, channel, external_id, to):
        return {
            "type": type,
            "from": {"channel": channel, "external_id": external_id},
            "to": to,
            "confidence": 1,
            "method": "source",
        }

    def author_kind(self, user):
        if is_bot(user):
            return "agent"
        return "owner" if login_of(user) == self.login else "other"

    # -------------------------------------------------------------- commits

    def commit(self, repo, item, private=None):
        detail = item.get("commit") or {}
        sha = item.get("sha")
        written = detail.get("author") or {}
        committed = detail.get("committer") or {}
        at = utc(written.get("date") or committed.get("date"))
        if not sha or not at:
            return None, []
        message = detail.get("message") or ""
        co_authors = [{"name": n, "email": e} for n, e in TRAILER.findall(message)]
        ai = any(AI_PATTERN.search(c["name"] + " " + c["email"]) for c in co_authors)
        payload = {
            "repo": repo,
            "sha": sha,
            "message": message,
            "author": {"name": written.get("name"), "email": written.get("email"), "date": utc(written.get("date"))},
            "committer": {"name": committed.get("name"), "email": committed.get("email"), "date": utc(committed.get("date"))},
            "author_login": login_of(item.get("author")),
            "committer_login": login_of(item.get("committer")),
            "html_url": item.get("html_url"),
            "parents": [p.get("sha") for p in item.get("parents") or [] if isinstance(p, dict)],
        }
        lines = message.strip().splitlines()
        first_line = lines[0] if lines else "(empty commit message)"
        rest = "\n".join(lines[1:]).strip()
        external_id = f"{repo}@{sha}"
        out = {
            "channel": COMMITS,
            "external_id": external_id,
            "thread": {"external_id": f"{repo}:{at[:7]}", "title": f"{repo} commits {at[:7]}"},
            "happened_at": at,
            "author": author(item.get("author") or {"login": self.login}, written.get("name")),
            "content": f"Commit to {repo}: {first_line}" + ("\n\n" + truncate(rest) if rest else ""),
            "fields": clean(
                {
                    "repo": repo,
                    "sha": sha,
                    "author_kind": self.author_kind(item.get("author") or {"login": self.login}),
                    "ai_coauthored": ai,
                    "co_authors": co_authors or None,
                    "committed_at": payload["committer"]["date"],
                    "url": item.get("html_url"),
                    "private": private,
                    "mapping_revision": MAPPING_REVISION,
                }
            ),
            "payload": payload,
        }
        return out, [self.link("part_of", COMMITS, external_id, self.repo_entity(repo))]

    # ---------------------------------------------------- issues and pulls

    @staticmethod
    def repo_of_issue(issue):
        url = issue.get("repository_url") or ""
        return "/".join(url.rstrip("/").split("/")[-2:]) if url else None

    def issue(self, issue):
        repo = self.repo_of_issue(issue)
        number = issue.get("number")
        is_pr = "pull_request" in issue
        channel = PULLS if is_pr else ISSUES
        at = utc(issue.get("created_at"))
        if not repo or number is None or not at:
            return None, []
        pr = issue.get("pull_request") or {}
        payload = {
            "repo": repo,
            "number": number,
            "title": issue.get("title"),
            "body": issue.get("body"),
            "state": issue.get("state"),
            "state_reason": issue.get("state_reason"),
            "draft": issue.get("draft") if is_pr else None,
            "merged_at": utc(pr.get("merged_at")) if is_pr else None,
            "closed_at": utc(issue.get("closed_at")),
            "created_at": at,
            "labels": sorted(label.get("name") for label in issue.get("labels") or [] if isinstance(label, dict)),
            "user": login_of(issue.get("user")),
            "html_url": issue.get("html_url"),
        }
        state = issue.get("state")
        if is_pr and payload["merged_at"]:
            state = "merged"
        kind = "Pull request" if is_pr else "Issue"
        external_id = f"{repo}#{number}"
        out = {
            "channel": channel,
            "external_id": external_id,
            "thread": {"external_id": repo, "title": repo},
            "happened_at": at,
            "ended_at": payload["merged_at"] or payload["closed_at"],
            "author": author(issue.get("user")),
            "content": f"{kind} {repo}#{number} ({state}): {issue.get('title') or ''}".rstrip()
            + ("\n\n" + truncate(issue.get("body")) if issue.get("body") else ""),
            "fields": clean(
                {
                    "repo": repo,
                    "number": number,
                    "title": issue.get("title"),
                    "state": state,
                    "draft": payload["draft"],
                    "merged": bool(payload["merged_at"]) if is_pr else None,
                    "merged_at": payload["merged_at"],
                    "closed_at": payload["closed_at"],
                    "labels": payload["labels"] or None,
                    "author_kind": self.author_kind(issue.get("user")),
                    "url": issue.get("html_url"),
                    "mapping_revision": MAPPING_REVISION,
                }
            ),
            "payload": payload,
        }
        links = [
            self.link("part_of", channel, external_id, self.repo_entity(repo)),
            self.link("represents", channel, external_id, self.issue_entity(repo, number, is_pr)),
        ]
        return out, links

    # ------------------------------------------------------------- comments

    def comment(self, issue, comment, comment_type):
        repo = self.repo_of_issue(issue)
        number = issue.get("number")
        is_pr = "pull_request" in issue
        at = utc(comment.get("created_at"))
        if not comment.get("id") or not at:
            return None, []
        prefix = "issuecomment" if comment_type == "issue_comment" else "reviewcomment"
        external_id = f"{prefix}:{comment['id']}"
        title = issue.get("title") or ""
        payload = {
            "id": comment["id"],
            "type": comment_type,
            "repo": repo,
            "number": number,
            "issue_title": title,
            "body": comment.get("body"),
            "created_at": at,
            "edited_at": utc(comment.get("updated_at")) if comment.get("updated_at") != comment.get("created_at") else None,
            "path": comment.get("path"),
            "line": comment.get("line") or comment.get("original_line"),
            "in_reply_to_id": comment.get("in_reply_to_id"),
            "user": login_of(comment.get("user")),
            "html_url": comment.get("html_url"),
        }
        where = f" on {comment['path']}" if comment.get("path") else ""
        out = {
            "channel": COMMENTS,
            "external_id": external_id,
            "thread": {"external_id": f"{repo}#{number}", "title": f"{repo}#{number} {title}".strip()},
            "happened_at": at,
            "author": author(comment.get("user")),
            "content": f"Comment on {repo}#{number} ({title}){where}: {truncate(comment.get('body'))}",
            "fields": clean(
                {
                    "repo": repo,
                    "number": number,
                    "on": "pull_request" if is_pr else "issue",
                    "comment_type": comment_type,
                    "path": comment.get("path"),
                    "edited": payload["edited_at"] is not None,
                    "author_kind": self.author_kind(comment.get("user")),
                    "url": comment.get("html_url"),
                    "mapping_revision": MAPPING_REVISION,
                }
            ),
            "payload": payload,
        }
        return out, [self.link("part_of", COMMENTS, external_id, self.issue_entity(repo, number, is_pr))]

    def review(self, issue, review):
        repo = self.repo_of_issue(issue)
        number = issue.get("number")
        at = utc(review.get("submitted_at"))
        state = review.get("state")
        if not review.get("id") or not at or state == "PENDING":
            return None, []
        external_id = f"review:{review['id']}"
        title = issue.get("title") or ""
        verb = (state or "reviewed").lower().replace("_", " ")
        payload = {
            "id": review["id"],
            "repo": repo,
            "number": number,
            "pull_title": title,
            "state": state,
            "body": review.get("body"),
            "submitted_at": at,
            "commit_id": review.get("commit_id"),
            "user": login_of(review.get("user")),
            "html_url": review.get("html_url"),
        }
        out = {
            "channel": REVIEWS,
            "external_id": external_id,
            "thread": {"external_id": f"{repo}#{number}", "title": f"{repo}#{number} {title}".strip()},
            "happened_at": at,
            "author": author(review.get("user")),
            "content": f"Review ({verb}) of {repo}#{number}: {title}" + ("\n\n" + truncate(review["body"]) if review.get("body") else ""),
            "fields": clean(
                {
                    "repo": repo,
                    "number": number,
                    "state": state,
                    "commit_id": review.get("commit_id"),
                    "own_pull_request": login_of(issue.get("user")) == self.login,
                    "author_kind": self.author_kind(review.get("user")),
                    "url": review.get("html_url"),
                    "mapping_revision": MAPPING_REVISION,
                }
            ),
            "payload": payload,
        }
        return out, [self.link("part_of", REVIEWS, external_id, self.issue_entity(repo, number, True))]

    # --------------------------------------------------- releases and stars

    def release(self, repo, release):
        at = utc(release.get("published_at") or release.get("created_at"))
        if not release.get("id") or not at or release.get("draft"):
            return None, []
        external_id = f"release:{release['id']}"
        name = release.get("name") or release.get("tag_name") or ""
        payload = {
            "id": release["id"],
            "repo": repo,
            "tag_name": release.get("tag_name"),
            "name": release.get("name"),
            "body": release.get("body"),
            "prerelease": bool(release.get("prerelease")),
            "created_at": utc(release.get("created_at")),
            "published_at": utc(release.get("published_at")),
            "author": login_of(release.get("author")),
            "html_url": release.get("html_url"),
        }
        out = {
            "channel": RELEASES,
            "external_id": external_id,
            "thread": {"external_id": repo, "title": f"{repo} releases"},
            "happened_at": at,
            "author": author(release.get("author")),
            "content": f"Released {repo} {name}".rstrip() + ("\n\n" + truncate(release["body"]) if release.get("body") else ""),
            "fields": clean(
                {
                    "repo": repo,
                    "tag": release.get("tag_name"),
                    "name": release.get("name"),
                    "prerelease": payload["prerelease"],
                    "author_kind": self.author_kind(release.get("author")),
                    "url": release.get("html_url"),
                    "mapping_revision": MAPPING_REVISION,
                }
            ),
            "payload": payload,
        }
        return out, [self.link("part_of", RELEASES, external_id, self.repo_entity(repo))]

    def star(self, star):
        repo = star.get("repo") or {}
        name = repo.get("full_name")
        at = utc(star.get("starred_at"))
        if not name or not at:
            return None, []
        external_id = f"star:{name}"
        payload = {
            "repo": name,
            "description": repo.get("description"),
            "language": repo.get("language"),
            "topics": sorted(repo.get("topics") or []),
            "html_url": repo.get("html_url"),
            "owner": login_of(repo.get("owner")),
            "starred_at": at,
        }
        out = {
            "channel": STARS,
            "external_id": external_id,
            "thread": {"external_id": "stars", "title": "Starred repositories"},
            "happened_at": at,
            "content": f"Starred {name}" + (f": {repo['description']}" if repo.get("description") else ""),
            "fields": clean(
                {
                    "repo": name,
                    "description": repo.get("description"),
                    "language": repo.get("language"),
                    "topics": payload["topics"] or None,
                    "url": repo.get("html_url"),
                    "mapping_revision": MAPPING_REVISION,
                }
            ),
            "payload": payload,
        }
        return out, [self.link("saved", STARS, external_id, self.repo_entity(name))]
