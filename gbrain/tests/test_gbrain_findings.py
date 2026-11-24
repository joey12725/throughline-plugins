import psycopg
from throughline_sdk import Client
from throughline_sdk._server import core_server
from throughline_gbrain import GBrain


def subject_columns(record):
    """The subject columns a destination row of this record carries."""
    subject = record.get("subject") or {}
    return {
        "subject_from": subject.get("start"),
        "subject_from_precision": subject.get("start_precision"),
    }


def test_brn_4_live_metadata_and_complete_verify(source_dsn, monkeypatch):
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with psycopg.connect(source_dsn) as conn:
        conn.execute(
            "INSERT INTO page_versions VALUES (1,1,'Old apple text.','{\"rev\":1}','2020-06-01Z'),(2,1,'Apple trees.','{\"rev\":2}','2020-07-01Z')"
        )
        conn.execute(
            'UPDATE pages SET frontmatter=\'{"rev":3,"status":"final"}\' WHERE id=1'
        )
    with core_server() as (_, env):
        client = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "gbrain")
        try:
            GBrain().execute(client, "backfill")
            row = client.request(
                "POST",
                "sql",
                {
                    "statement": "SELECT fields FROM api.messages WHERE external_id='fake-source:orchard'"
                },
            )["rows"][0]
            assert row["fields"]["frontmatter"] == {"rev": 3, "status": "final"}
            assert row["fields"]["updated_at"].startswith("2021-01-01")
            assert all(a == b for a, b in GBrain().verify(client))
            # Source changes in each imported category must make verification fail.
            original_source = GBrain().source()
            import copy

            for table in ("timeline_entries", "links"):
                changed = copy.deepcopy(original_source)
                changed[table] = []
                p = GBrain()
                p.source = lambda: changed
                assert any(a != b for a, b in p.verify(client)), table
            # History is append-only in the destination: snapshots the source
            # dropped later are extra history, not a missing import.
            changed = copy.deepcopy(original_source)
            changed["page_versions"] = []
            p = GBrain()
            p.source = lambda: changed
            assert all(a == b for a, b in p.verify(client))
            GBrain().execute(client, "sync")
            assert all(a == b for a, b in GBrain().verify(client))
        finally:
            client.close()


def test_brn_4_metadata_same_body_and_verify_every_category(source_dsn, monkeypatch):
    import copy
    from throughline_sdk import Record

    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    data["page_versions"] = [
        dict(
            id=1,
            page_id=1,
            compiled_truth="Old apple text.",
            frontmatter={"rev": 1},
            snapshot_at="2020-06-01T00:00:00+00:00",
        ),
        dict(
            id=2,
            page_id=1,
            compiled_truth="Apple trees.",
            frontmatter={"rev": 2},
            snapshot_at="2020-07-01T00:00:00+00:00",
        ),
    ]
    data["pages"][0]["frontmatter"] = {"rev": 3, "status": "final"}
    p.source = lambda: data
    p.client = type("Cursor", (), {"cursor": lambda self, key: {}})()
    records = list(p.records({}, "backfill"))
    pages = [
        r
        for r in records
        if isinstance(r, dict)
        and r["channel"] == "gbrain.pages"
        and r["external_id"] == "fake-source:orchard"
    ]
    assert len(pages) == 3
    assert pages[-1]["fields"]["frontmatter"] == {"rev": 3, "status": "final"}
    assert pages[-1]["payload"]["version"] is None
    versions = [
        {
            **r,
            "thread_external_id": r["thread"]["external_id"],
            "thread_title": r["thread"].get("title"),
            **subject_columns(r),
        }
        for r in records
        if isinstance(r, dict)
    ]
    current = list({(r["channel"], r["external_id"]): r for r in versions}.values())
    links = [
        dict(
            type=r.data["type"],
            method=r.data["method"],
            evidence=r.data.get("evidence", {}),
            from_channel=r.data["from"]["channel"],
            from_external_id=r.data["from"]["external_id"],
            to_channel=r.data["to"]["channel"],
            to_external_id=r.data["to"]["external_id"],
        )
        for r in records
        if isinstance(r, Record) and r.kind == "link"
    ]

    class Destination:
        def request(self, method, path, body):
            sql = body["statement"]
            return {
                "rows": links
                if "api.links" in sql
                else versions
                if "api.message_versions" in sql
                else current
            }

    assert all(a == b for a, b in p.verify(Destination()))
    for table in ("timeline_entries", "links"):
        changed = copy.deepcopy(data)
        changed[table] = []
        p.source = lambda: changed
        assert any(a != b for a, b in p.verify(Destination())), table
    # A source snapshot whose body no stored version holds is a mismatch.
    changed = copy.deepcopy(data)
    changed["page_versions"].append(
        dict(
            id=3,
            page_id=1,
            compiled_truth="Invented body that was never imported.",
            frontmatter={},
            snapshot_at="2020-08-01T00:00:00+00:00",
        )
    )
    p.source = lambda: changed
    assert any(a != b for a, b in p.verify(Destination()))
    changed = copy.deepcopy(data)
    changed["pages"][0]["frontmatter"]["rev"] = 4
    p.source = lambda: changed
    assert any(a != b for a, b in p.verify(Destination()))


def test_brn_4_verify_rejects_missing_timeline_links_history_and_changed_fields(
    source_dsn, monkeypatch
):
    import copy
    from throughline_sdk import Record

    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    data["page_versions"] = [
        dict(
            id=1,
            page_id=1,
            compiled_truth="Old invented text.",
            frontmatter={},
            snapshot_at="2019-01-01T00:00:00+00:00",
        )
    ]
    p.source = lambda: data
    p.client = type("Cursor", (), {"cursor": lambda self, key: {}})()
    records = list(p.records({}, "backfill"))
    original = [
        {
            **r,
            "thread_external_id": r["thread"]["external_id"],
            "thread_title": r["thread"].get("title"),
            **subject_columns(r),
        }
        for r in records
        if isinstance(r, dict)
    ]
    original_current = list(
        {(r["channel"], r["external_id"]): r for r in original}.values()
    )
    original_links = [
        dict(
            type=r.data["type"],
            method=r.data["method"],
            evidence=r.data.get("evidence", {}),
            from_channel=r.data["from"]["channel"],
            from_external_id=r.data["from"]["external_id"],
            to_channel=r.data["to"]["channel"],
            to_external_id=r.data["to"]["external_id"],
        )
        for r in records
        if isinstance(r, Record) and r.kind == "link"
    ]
    for missing in ("timeline", "links", "history", "fields", "content"):
        versions, current, links = (
            copy.deepcopy(original),
            copy.deepcopy(original_current),
            copy.deepcopy(original_links),
        )
        if missing == "timeline":
            current = [r for r in current if r["channel"] != "gbrain.timeline"]
        elif missing == "links":
            links = []
        elif missing == "history":
            versions = versions[1:]
        elif missing == "fields":
            current[0]["fields"]["tags"] = ["corrupt-invented-tag"]
        else:
            current[0]["content"] = "Corrupt invented content"

        class Destination:
            def request(self, method, path, body):
                sql = body["statement"]
                if "count(*) AS pages" in sql:
                    # The original verifier sees matching page and alias counts.
                    return {
                        "rows": [
                            {
                                "pages": 2,
                                "alias_groups": [["fruit trees", "old-orchard"], []],
                            }
                        ]
                    }
                return {
                    "rows": links
                    if "api.links" in sql
                    else versions
                    if "api.message_versions" in sql
                    else current
                }

        assert any(a != b for a, b in p.verify(Destination())), missing


def test_brn_4_verify_pages_beyond_one_sql_response(source_dsn, monkeypatch):
    import copy
    import re

    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    template = data["pages"][0]
    data["pages"] = [
        dict(copy.deepcopy(template), id=i, slug=f"fake-page-{i}")
        for i in range(1, 126)
    ]
    for key in (
        "page_versions",
        "page_aliases",
        "slug_aliases",
        "tags",
        "timeline_entries",
        "links",
    ):
        data[key] = []
    p.source = lambda: data
    p.client = type("Cursor", (), {"cursor": lambda self, key: {}})()
    rows = [
        {
            **r,
            "id": i,
            "thread_external_id": r["thread"]["external_id"],
            "thread_title": r["thread"].get("title"),
            **subject_columns(r),
        }
        for i, r in enumerate(
            (r for r in p.records({}, "backfill") if isinstance(r, dict)), 1
        )
    ]

    class Destination:
        calls = []

        def request(self, method, path, body):
            statement = body["statement"]
            self.calls.append(statement)
            if "api.links" in statement:
                return {"rows": []}
            after = int(re.search(r"id>(\d+)", statement)[1])
            return {"rows": [r for r in rows if r["id"] > after][:100]}

    client = Destination()
    assert all(a == b for a, b in p.verify(client))
    assert any("id>100" in q for q in client.calls)
    rows[-1]["content"] = "Corrupt final invented page"
    assert any(a != b for a, b in p.verify(client))


def test_verify_matches_after_sync_captured_source_edits(source_dsn, monkeypatch):
    """The earlier live version stays in the history; that is not a mismatch."""
    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    with core_server() as (_, env):
        client = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "gbrain")
        try:
            GBrain().execute(client, "backfill")
            assert all(a == b for a, b in GBrain().verify(client))
            with psycopg.connect(source_dsn) as conn:
                # GBrain snapshots the old body, then edits the page.
                conn.execute(
                    "INSERT INTO page_versions VALUES (1,1,'Apple trees.','{\"invented\":true}','2021-06-01Z')"
                )
                conn.execute(
                    "UPDATE pages SET compiled_truth='Pear trees.', title='Invented pears', timeline='A pear was planted.', updated_at='2021-06-01Z' WHERE id=1"
                )
                conn.execute("INSERT INTO tags VALUES (2,1,'edited')")
                conn.execute(
                    "UPDATE timeline_entries SET detail='An invented pear tree.' WHERE id=1"
                )
            GBrain().execute(client, "sync")
            history = client.request(
                "POST",
                "sql",
                {
                    "statement": "SELECT count(*) n FROM api.message_versions WHERE external_id='fake-source:orchard'"
                },
            )["rows"][0]["n"]
            # First import, the snapshot under the new title, the edited page.
            assert history == 3
            assert all(a == b for a, b in GBrain().verify(client))
            GBrain().execute(client, "sync")
            assert all(a == b for a, b in GBrain().verify(client))
            # A snapshot that was never imported is still reported.
            with psycopg.connect(source_dsn) as conn:
                conn.execute(
                    "INSERT INTO page_versions VALUES (2,1,'Plum trees.','{}','2021-07-01Z')"
                )
            assert any(a != b for a, b in GBrain().verify(client))
        finally:
            client.close()


def empty_progress(p, data, cursors=None):
    p.source = lambda: data
    p.client = type(
        "Cursor", (), {"cursor": lambda self, key: (cursors or {}).get(key, {})}
    )()
    return p


def test_verify_reports_lost_snapshot_of_a_page_that_grows_by_appending(
    source_dsn, monkeypatch
):
    """An earlier snapshot is a prefix of every later body; that is not present."""
    from throughline_sdk import Record

    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    page = data["pages"][0]
    first = "Invented journal, day one."
    second = first + "\n\nDay two."
    page["compiled_truth"] = second + "\n\nDay three."
    page["timeline"] = "2020-01-01: invented timeline entry"
    snapshots = [
        dict(
            id=n,
            page_id=page["id"],
            compiled_truth=body,
            frontmatter={},
            snapshot_at=f"2020-0{n}-01T00:00:00+00:00",
        )
        for n, body in ((1, first), (2, second))
    ]
    data["page_versions"] = snapshots
    p.source = lambda: data
    p.client = type("Cursor", (), {"cursor": lambda self, key: {}})()

    def destination(records):
        versions = [
            {
                **r,
                "thread_external_id": r["thread"]["external_id"],
                "thread_title": r["thread"].get("title"),
                **subject_columns(r),
            }
            for r in records
            if isinstance(r, dict)
        ]
        current = list({(r["channel"], r["external_id"]): r for r in versions}.values())
        links = [
            dict(
                type=r.data["type"],
                method=r.data["method"],
                evidence=r.data.get("evidence", {}),
                from_channel=r.data["from"]["channel"],
                from_external_id=r.data["from"]["external_id"],
                to_channel=r.data["to"]["channel"],
                to_external_id=r.data["to"]["external_id"],
            )
            for r in records
            if isinstance(r, Record) and r.kind == "link"
        ]

        class Destination:
            def request(self, method, path, body):
                sql = body["statement"]
                return {
                    "rows": links
                    if "api.links" in sql
                    else versions
                    if "api.message_versions" in sql
                    else current
                }

        return Destination()

    complete = list(p.records({}, "backfill"))
    assert all(a == b for a, b in p.verify(destination(complete)))
    # Neither snapshot was ever stored: only the current page is there, and its
    # text starts with both snapshot bodies.
    data["page_versions"] = []
    live_only = destination(list(p.records({}, "backfill")))
    data["page_versions"] = snapshots
    pairs = p.verify(live_only)
    assert pairs[3] == (set(), {1, 2})
    # One stored, one lost: only the lost one is reported.
    data["page_versions"] = snapshots[1:]
    second_only = destination(list(p.records({}, "backfill")))
    data["page_versions"] = snapshots
    assert p.verify(second_only)[3] == (set(), {1})


def test_stored_body_matches_whole_parts_only():
    from throughline_gbrain import CONTENT_BUDGET, fit, stored_body, text

    short, long = "Invented.", "Invented.\n\nMore."
    for timeline in (None, "2020-01-01: invented"):
        assert stored_body(text("T", short, timeline), "T", {short, long}) == short
        assert stored_body(text("T", long, timeline), "T", {short, long}) == long
        assert stored_body(text("", long, timeline), "", {short}) == short
    assert stored_body(text("T", "Invented. More."), "T", {short}) is None
    assert stored_body(text("T", long), "Other", {short, long}) is None
    # Text cut for indexing is compared up to the cut.
    huge = "Invented long text. " * 20_000
    cut = fit(text("T", huge, "2020-01-01: invented"))
    assert len(cut.encode()) <= CONTENT_BUDGET
    assert stored_body(cut, "T", {huge, short}) == huge
    assert stored_body(cut, "T", {huge[:1000] + "x" + huge[1001:]}) is None


def test_oversized_or_unsafe_snapshot_costs_only_itself(
    source_dsn, monkeypatch, caplog
):
    from throughline_gbrain import CONTENT_BUDGET, TRUNCATED

    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    huge = "Invented long snapshot. " * 20_000  # 480,000 bytes
    data["page_versions"] = [
        dict(
            id=1,
            page_id=1,
            compiled_truth=huge,
            frontmatter={},
            snapshot_at="2020-06-01T00:00:00+00:00",
        ),
        dict(
            id=2,
            page_id=1,
            compiled_truth="Bad\x00snapshot",
            frontmatter={},
            snapshot_at="2020-07-01T00:00:00+00:00",
        ),
        dict(
            id=3,
            page_id=1,
            compiled_truth="Middle apple text.",
            frontmatter={},
            snapshot_at="2020-08-01T00:00:00+00:00",
        ),
    ]
    p.quarantined = 0
    records = list(empty_progress(p, data).records({}, "backfill"))
    pages = [
        r
        for r in records
        if isinstance(r, dict) and r["external_id"] == "fake-source:orchard"
    ]
    # The oversized snapshot is indexed truncated and kept whole in the payload;
    # the NUL snapshot is dropped alone; the later snapshot and live page import.
    assert [
        r["payload"]["version"] and r["payload"]["version"]["id"] for r in pages
    ] == [1, 3, None]
    assert len(pages[0]["content"].encode()) <= CONTENT_BUDGET
    assert pages[0]["content"].endswith(TRUNCATED)
    assert pages[0]["payload"]["version"]["compiled_truth"] == huge
    assert pages[2]["content"].startswith("Invented orchard\n\nApple trees.")
    assert p.quarantined == 1
    assert "version id=2" in caplog.text
    assert "Bad" not in caplog.text
    # The quarantined snapshot is not marked done, so a later run reports it again.
    cursor = [
        r.cursor for r in records if str(getattr(r, "key", "")).startswith("page_")
    ]
    orchard = [c for c in cursor if len(c["snapshots"]) == 2]
    assert orchard and all(len(c["snapshots"]) <= 2 for c in cursor)
    # A live body above the budget is truncated too, never dropped.
    data["pages"][1]["compiled_truth"] = huge
    garden = [
        r
        for r in empty_progress(GBrain(), data).records({}, "backfill")
        if isinstance(r, dict) and r["external_id"] == "fake-source:garden"
    ]
    assert len(garden) == 1 and garden[0]["content"].endswith(TRUNCATED)


def test_pages_stored_by_older_mapping_are_sent_again_once(source_dsn, monkeypatch):
    """A destination imported before source_hash existed must converge by sync."""
    from throughline_gbrain import digest

    monkeypatch.setenv("GBRAIN_DSN", source_dsn)
    p = GBrain()
    data = p.source()
    page = next(x for x in data["pages"] if x["slug"] == "garden")
    key = "page_" + digest("fake-source:garden")
    old = {key: {"snapshots": [], "content": None, "live": digest([page, [], []])}}
    sent = [
        r
        for r in empty_progress(p, data, old).records({}, "sync")
        if isinstance(r, dict) and r["external_id"] == "fake-source:garden"
    ]
    assert len(sent) == 1 and sent[0]["fields"]["source_hash"]
    checkpoint = next(
        r.cursor
        for r in empty_progress(p, data, old).records({}, "sync")
        if getattr(r, "key", None) == key
    )
    assert not [
        r
        for r in empty_progress(p, data, {key: checkpoint}).records({}, "sync")
        if isinstance(r, dict) and r["external_id"] == "fake-source:garden"
    ]


def test_verify_credits_each_cut_version_with_one_oversized_body():
    from throughline_gbrain import CONTENT_BUDGET, fit, held_bodies, text

    base = "word " * (CONTENT_BUDGET // 5 + 50)
    grown = [base + f" tail {i}" for i in range(3)]

    def row(body):
        return {
            "external_id": "s:p",
            "content": fit(text("T", body)),
            "fields": {"title": "T"},
        }

    known = {"s:p": set(grown) | {"small"}}
    all_rows = [row(b) for b in grown] + [row("small")]
    assert held_bodies(all_rows, known) == {("s:p", b) for b in known["s:p"]}
    # One oversized snapshot lost: two cut versions prove only two of three.
    lost = held_bodies(all_rows[1:], known)
    assert len([h for h in lost if h[1] in grown]) == 2


def test_verify_credits_body_that_reads_like_a_longer_snapshot():
    from throughline_gbrain import held_bodies, text

    short, timeline = "Body.", "2026-01-01 note"
    longer = text(short, timeline)

    def row(content):
        return {"external_id": "s:p", "content": content, "fields": {"title": "T"}}

    known = {"s:p": {short, longer}}
    rows = [row(text("T", short, timeline)), row(text("T", longer, timeline))]
    assert held_bodies(rows, known) == {("s:p", short), ("s:p", longer)}
    assert held_bodies(rows[:1], known) == {("s:p", longer)}
