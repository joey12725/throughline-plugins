import copy
import json
from datetime import UTC, datetime

from throughline_notion import fetch


USER = {
    "id": "user-fixture",
    "name": "Fixture User",
    "object": "user",
    "person": {"email": "fixture@example.invalid"},
    "type": "person",
}


def page(ident, edited, title="Fixture Page", **extra):
    value = {
        "archived": False,
        "created_by": {"id": "user-fixture", "object": "user"},
        "created_time": "2024-01-01T00:00:00Z",
        "id": ident,
        "in_trash": False,
        "last_edited_by": {"id": "user-fixture", "object": "user"},
        "last_edited_time": edited,
        "object": "page",
        "parent": {"page_id": "root", "type": "page_id"},
        "properties": {"title": {"id": "title", "title": [{"plain_text": title}], "type": "title"}},
        "url": f"https://notion.invalid/{ident}",
    }
    value.update(extra)
    return value


DB = {
    "archived": False,
    "created_by": {"id": "user-fixture", "object": "user"},
    "created_time": "2024-01-01T00:00:00Z",
    "description": [],
    "id": "db-fixture",
    "in_trash": False,
    "is_inline": False,
    "last_edited_by": {"id": "user-fixture", "object": "user"},
    "last_edited_time": "2024-01-02T00:00:00Z",
    "object": "database",
    "parent": {"type": "workspace", "workspace": True},
    "properties": {"Name": {"id": "title", "name": "Name", "title": {}, "type": "title"}},
    "title": [{"plain_text": "Fixture Database"}],
    "url": "https://notion.invalid/db-fixture",
}


class FakeClient:
    def __init__(self, pages, markdown=None, fail=()):
        self.pages = pages
        self.markdown = markdown or {}
        self.fail = set(fail)
        self.calls = 0

    def call(self, tool, arguments):
        self.calls += 1
        if tool == "NOTION_WHO_AM_I":
            return {"id": "workspace-fixture", "workspace_name": "Fixture Workspace"}
        if tool == "NOTION_LIST_USERS":
            return {"results": [USER], "has_more": False, "next_cursor": None}
        if tool == "NOTION_FETCH_DATA":
            items = [DB] if arguments["fetch_type"] == "databases" else self.pages
            return {"results": copy.deepcopy(items), "has_more": False, "next_cursor": None}
        if tool == "NOTION_GET_PAGE_MARKDOWN":
            ident = arguments["page_id"]
            if ident in self.fail:
                raise fetch.FetchError("fixture page unavailable")
            return {
                "id": ident,
                "markdown": self.markdown.get(ident, f"Markdown for {ident}"),
                "truncated": False,
                "unknown_block_ids": [],
            }
        if tool == "NOTION_FETCH_ALL_BLOCK_CONTENTS":
            raise fetch.FetchError("fixture blocks unavailable")
        raise AssertionError(tool)

    def call_many(self, calls):
        self.calls += len(calls)
        result = {}
        for label, tool, arguments in calls:
            assert tool == "NOTION_GET_PAGE_MARKDOWN"
            ident = arguments["page_id"]
            if ident in self.fail:
                result[label] = {"result": None, "error": {"status": 503}}
            else:
                text = self.markdown.get(ident, f"Markdown for {ident}")
                result[label] = {
                    "result": {
                        "data": {
                            "id": ident,
                            "markdown": text,
                            "object": "page_markdown",
                            "truncated": False,
                            "unknown_block_ids": [],
                        },
                        "error": None,
                    },
                    "error": None,
                }
        if any(value["error"] for value in result.values()):
            raise fetch.FetchError("fixture transient page error")
        return result


def config(**values):
    env = {
        "COMPOSIO_MCP_TOKEN": "fixture-secret",
        "NOTION_MIN_INTERVAL_S": "0",
        "NOTION_RETRY_BASE_S": "0",
        "NOTION_CONTENT_LIMIT": "20",
    }
    env.update(values)
    return fetch.Config.from_env(env)


def test_full_then_incremental_and_missing():
    def now():
        return datetime(2024, 4, 3, 12, tzinfo=UTC)

    first_client = FakeClient(
        [page("one", "2024-04-03T10:00:00Z"), page("two", "2024-04-02T10:00:00Z")],
        {"one": "one body", "two": "two body"},
    )
    records, state = fetch.Fetcher(config(), client=first_client, now=now).run()
    assert [record["id"] for record in records if record["kind"] == "notion.page"] == ["one", "two"]
    assert state["high_water"] == "2024-04-03T10:00:00Z"
    assert records[-1]["raw"]["mode"] == "backfill"

    changed = page("one", "2024-04-03T11:00:00Z", title="Fixture Page Revised")
    second_client = FakeClient([changed], {"one": "revised body"})
    later, state2 = fetch.Fetcher(config(), state, client=second_client, now=now).run()
    pages = [record for record in later if record["kind"] == "notion.page"]
    assert [(record["id"], bool(record.get("missing"))) for record in pages] == [("one", False), ("two", True)]
    assert state2["pages"]["two"]["missing"] is True
    assert later[-1]["raw"]["mode"] == "incremental"


def test_content_cap_and_retry_state():
    def now():
        return datetime(2024, 4, 3, 12, tzinfo=UTC)

    item = page("long", "2024-04-03T10:00:00Z")
    client = FakeClient([item], {"long": "x" * 30})
    records, state = fetch.Fetcher(config(), client=client, now=now).run()
    row = next(record for record in records if record["kind"] == "notion.page")
    assert row["markdown"] == "x" * 20
    assert row["source_chars"] == 30 and row["content_truncated"] is True
    assert records[-1]["raw"]["truncated_page_ids"] == ["long"]

    failed = FakeClient([page("long", "2024-04-03T11:00:00Z")], fail={"long"})
    later, state2 = fetch.Fetcher(config(), state, client=failed, now=now).run()
    unavailable = [record for record in later if record["kind"] == "notion.page"]
    assert len(unavailable) == 1 and unavailable[0]["content_unavailable"] is True
    assert state2["pages"]["long"]["last_edited_time"] == "2024-04-03T11:00:00Z"
    assert state2["pages"]["long"]["pending_content"] is True
    assert later[-1]["raw"]["page_errors"]

    idle, _ = fetch.Fetcher(config(), state2, client=failed, now=now).run()
    assert not [record for record in idle if record["kind"] == "notion.page"]


def test_export_cli_uses_fixture_client_shape(tmp_path, monkeypatch):
    # Config validation never prints or serializes the credential.
    cfg = config()
    assert cfg.mcp_token == "fixture-secret"
    assert "fixture-secret" not in json.dumps(fetch.SETTINGS)


def test_blocks_to_text():
    data = {
        "results": [
            {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Plan"}]}},
            {"type": "to_do", "to_do": {"checked": True, "rich_text": [{"plain_text": "Ship it"}]}},
            {
                "type": "toggle",
                "toggle": {"rich_text": [{"plain_text": "Details"}]},
                "children": [
                    {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Invented detail"}]}}
                ],
            },
        ]
    }
    assert fetch.blocks_to_text(data).splitlines() == [
        "## Plan",
        "- [x] Ship it",
        "Details",
        "  Invented detail",
    ]
