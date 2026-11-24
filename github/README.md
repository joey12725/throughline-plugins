# Throughline GitHub (`github`)

The token owner's own GitHub activity as a live Throughline source: commits
you authored, pull requests and issues you opened, your comments and
reviews, releases you published and repositories you starred. Full history
on backfill, then incremental sync from per-stream cursors. Read-only: the
plugin never writes to GitHub.

## What it captures

| Channel | One message per | `happened_at` | Thread |
| --- | --- | --- | --- |
| `github.commits` | commit you authored (default branch) | author date | repository and month |
| `github.pull_requests` | pull request you opened | created; `ended_at` merged/closed | repository |
| `github.issues` | issue you opened | created; `ended_at` closed | repository |
| `github.comments` | your issue/PR comment or inline review comment | posted | the issue or PR |
| `github.reviews` | review you submitted (approved, changes requested, commented, dismissed) | submitted | the PR |
| `github.releases` | release you published (drafts skipped) | published | repository |
| `github.stars` (reference) | starred repository | starred | one thread |

Fields (run `throughline-github manifest` for the full list):

- commits: `repo`, `sha`, `author_kind`, `ai_coauthored`, `co_authors`,
  `committed_at`, `url`, `private`
- pull requests / issues: `repo`, `number`, `title`, `state`
  (`open`, `closed`, `merged`), `draft`, `merged`, `merged_at`, `closed_at`,
  `labels`, `author_kind`, `url`
- comments: `repo`, `number`, `on` (`issue` / `pull_request`),
  `comment_type` (`issue_comment` / `review_comment`), `path`, `edited`,
  `author_kind`, `url`
- reviews: `repo`, `number`, `state`, `commit_id`, `own_pull_request`,
  `author_kind`, `url`
- releases: `repo`, `tag`, `name`, `prerelease`, `author_kind`, `url`
- stars: `repo`, `description`, `language`, `topics`, `url`

Every record has `mapping_revision`. The author is
`{system: "github", kind: "username", value: <login>}`. `author_kind` is
`owner` for the token's user (only the owner's items are collected) and
`agent` for a bot account. Commits whose `Co-authored-by:` trailers name an
AI assistant (Claude, Copilot, Codex, Cursor, Aider, Devin, Gemini, ...) get
`ai_coauthored: true`, so AI-assisted work is distinguishable.

Pull requests and issues are re-read when they change; a state change
(merged, closed, retitled, relabelled) becomes a new version of the same
record. The payload is a fixed subset of the API object (no counters or
`updated_at`), so unrelated activity does not create versions.

### Links

Source links to placeholder entities (`external_ref` prefix `github:`, or the
Enterprise host name):

- commit, pull request, issue, release → `part_of` the repository
  (`github_repo`, e.g. `github:owner/name`)
- pull request / issue message → `represents` its entity
  (`github_pull_request` / `github_issue`, `github:owner/name#12`)
- comment → `part_of` the issue or pull request; review → `part_of` the pull
  request
- star → `saved` the repository

So a comment, the review and the PR itself all meet at the same entity, and
everything in a repository meets at the repository entity.

## How it reads GitHub

The Events API only covers the last 90 days, so it is not used. Instead:

- **commits**: every repository the token can list (`/user/repos`, or
  `GITHUB_REPOS`) is read with `/repos/{repo}/commits?author=<login>&since=`;
  then commit search (`author:<login>`) finds commits in repositories the
  token cannot list (contributions to other people's public projects).
  Results over search's 1,000-result cap are split by date range until each
  part fits. The same commit from either path normalises identically.
- **pull requests / issues / reviews / comments**: issue search
  (`type:pr author:`, `type:issue author:`, `type:pr reviewed-by:`,
  `commenter:`) sorted by `updated` ascending; reviews and comments are then
  read per issue (`since=` for comments) and filtered to your login.
- **releases**: per repository, newest first, stopping at the cursor.
- **stars**: `/user/starred` with `starred_at`, newest first, stopping at the
  cursor.

Each stream has its own cursor (the newest `updated_at` / committer date /
`starred_at` actually yielded; per repository for commit listings), and every
re-read starts `GITHUB_OVERLAP_H` before it because search indexing lags.
Checkpoints follow every page, so an interrupted backfill resumes where it
stopped.

### Rate limits

Every response's `X-RateLimit-Remaining` / `X-RateLimit-Reset` is honoured:
at zero, the next request waits for the reset. `403`/`429` rate-limit answers
(primary or secondary limits) wait for `Retry-After` or the reset, else back
off exponentially (60 s doubling); 5xx and transport errors are retried with a
short backoff. A required wait longer than `GITHUB_MAX_WAIT_S` ends the run at
its last checkpoint and reports the remaining streams as partial coverage
(`rate_limited`); the next run continues. Search allows 30 requests per minute,
so the first backfill of a busy account takes a while.

## Setup

Create a **fine-grained personal access token** (GitHub → Settings →
Developer settings → Fine-grained tokens):

- Resource owner: your account (add organisations you want covered, if
  they allow fine-grained tokens).
- Repository access: "All repositories" (or the ones you want).
- Repository permissions, all **read-only**: Metadata, Contents (commits,
  releases), Issues, Pull requests.
- Account permissions: Starring (read-only).

Public activity in other people's repositories (commits, PRs, comments,
reviews found by search) needs no extra permission. Private repositories of
other owners appear only when the token can access them. A classic token
with `repo` and `read:user` scopes also works.

```sh
throughline token create github --kind plugin --plugin github --json
export THROUGHLINE_URL=http://localhost:8000 THROUGHLINE_TOKEN='<plugin token>'
export GITHUB_TOKEN='<fine-grained PAT>'
uv run --package throughline-github throughline-github backfill --dry-run
uv run --package throughline-github throughline-github backfill
uv run --package throughline-github throughline-github sync --once   # hourly timer
```

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_TOKEN` | – (required, secret) | Fine-grained personal access token |
| `GITHUB_API_URL` | `https://api.github.com` | API base (GitHub Enterprise Server: `https://HOST/api/v3`) |
| `GITHUB_SINCE` | `2008-01-01` | Earliest activity read on an empty cursor |
| `GITHUB_STREAMS` | all | Comma list of `commits,pull_requests,issues,comments,reviews,releases,stars` |
| `GITHUB_REPOS` | `auto` | `auto` = every repository the token lists; or a comma list `owner/name` (empty: search only) |
| `GITHUB_OVERLAP_H` | `24` | Hours re-read before each cursor |
| `GITHUB_PER_PAGE` | `100` | Items per page (one checkpoint per page) |
| `GITHUB_MAX_WAIT_S` | `900` | Longest rate-limit wait before stopping the run |
| `GITHUB_FIXTURE` | – | Recorded dataset served instead of the API (tests only) |

The token is declared as a secret: it is redacted from logs and the runner
refuses to send records containing it. Redirects are not followed and
pagination links to another host are refused, so the token is only ever sent
to `GITHUB_API_URL`.

## Limitations

- Commits come from default branches only (that is what the commit list and
  commit search cover); commits on unmerged branches appear once merged
  (with the original author date). Commit attribution needs the commit
  email to be one of your GitHub account's emails.
- Search results are capped at 1,000 per query; the date splitting handles
  this for commits, but a single hour with more than 1,000 updated issues
  would be truncated (in practice impossible for one person).
- Unstarring, deleting a comment or deleting a release is not detected: the
  record stays as last seen.
- Comments are found through the `commenter:` qualifier; GitHub's search
  index occasionally lags by minutes to hours, which the overlap covers.
- Private contributions to repositories the token cannot access are
  invisible (GitHub only reports counts for them).
- Discussions, gists, wiki edits and project boards are not collected.
- The request/response shapes follow GitHub's REST documentation
  (API version 2022-11-28); the test suite runs against an in-process fake
  of those endpoints, not a live account.
