import copy
import json
import sys
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_youtube import YouTube
from throughline_youtube.parsers import (
    csv_rows,
    timestamp,
    video_id,
    watch_html,
    watch_json,
)
from throughline_youtube.sources import subscriptions, ytdlp

REC = Path(__file__).parent / "recorded"


@pytest.fixture(autouse=True)
def clean_source_env(monkeypatch):
    for d in [*YouTube.manifest.settings.values(), *YouTube.manifest.secrets.values()]:
        monkeypatch.delenv(d.env, raising=False)


def test_manifest_channels_and_links():
    names = [c.name for c in YouTube.manifest.channels]
    assert names == ["youtube.history", "youtube.watch_later", "youtube.subscriptions"]
    assert "youtube.watch" not in names
    assert "youtube.transcripts" not in names
    assert "transcript_of" not in YouTube.manifest.link_types


def collect(plugin, cursor=None, mode="sync"):
    items = list(plugin.records(cursor or {}, mode))
    return (
        [i for i in items if isinstance(i, dict)],
        [i for i in items if isinstance(i, Record)],
        [i.cursor for i in items if isinstance(i, Checkpoint)][-1],
    )


def live(monkeypatch, tmp_path, data=None):
    path = tmp_path / "recording.json"
    path.write_text(
        json.dumps(data or json.loads((REC / "live.json").read_text())),
        encoding="utf-8",
    )
    monkeypatch.setenv("YOUTUBE_RECORDING", str(path))
    monkeypatch.setenv("YOUTUBE_API", "true")
    monkeypatch.setenv("YOUTUBE_YTDLP", "true")
    return path


def test_msg_2_3_duplicate_views_offsets_and_removed(monkeypatch):
    monkeypatch.setenv("YOUTUBE_TAKEOUT", str(REC / "takeout"))
    messages, links, cursor = collect(YouTube(), mode="backfill")
    views = [m for m in messages if m["channel"] == "youtube.history"]
    assert len(views) == 4
    assert len({m["external_id"] for m in views}) == 4
    assert views[0]["happened_at"].endswith("+05:30")
    assert views[1]["happened_at"].endswith("-04:00")
    assert views[-1]["fields"]["video_id"] is None
    works = [r.data["to"] for r in links if r.data["type"] == "consumed"]
    assert {w["external_ref"] for w in works} == {"youtube:FAKE_VIDEO_A"}
    assert collect(YouTube(), cursor)[0] == []


@pytest.mark.parametrize(
    "value", ["01/02/2020 13:00", "2020-01-01T12:00:00", "Jan 2, 2020, 1:00:00 PM CST"]
)
def test_msg_3_reject_ambiguous_timezone(value):
    with pytest.raises(ValueError):
        timestamp(value)


def test_msg_3_localized_explicit_date():
    assert timestamp("02.01.2020 13:00 +0530", "%d.%m.%Y %H:%M %z").endswith("+05:30")
    assert timestamp("Jan 2, 2020, 1:00:00 PM UTC").endswith("+00:00")


def test_msg_6_html_entities_raw_and_localized_action():
    block = '<div class="content-cell mdl-cell">Angesehen <a href="https://www.youtube.com/watch?v=FAKE_VIDEO_A">A &amp; B</a><br><a href="https://www.youtube.com/channel/UC_FAKE">Invented channel</a><br>2020-01-02T13:00:00+02:00</div>'
    row = list(watch_html('<div class="outer">' + block + "</div>"))[0]
    assert row["title"] == "A & B"
    assert row["at"].endswith("+02:00")
    assert row["payload"] == {"html": block}


def test_msg_3_html_unsupported_localization_fails():
    with pytest.raises(ValueError, match="Cannot parse"):
        list(
            watch_html(
                '<div class="content-cell"><a href="https://youtu.be/FAKE_A">Title</a><br>2 janvier 2020 13:00</div>'
            )
        )


def test_msg_5_6_json_localization_flags_ads_and_search():
    rows = [
        {
            "title": "Angesehen: Invented",
            "titleUrl": "https://music.youtube.com/watch?v=FAKE_A",
            "time": "2020-01-01T01:00:00Z",
            "details": [{"name": "From Google Ads"}],
        },
        {
            "title": "Search",
            "titleUrl": "https://www.youtube.com/results?search_query=fake",
        },
    ]
    actual = list(watch_json(json.dumps(rows)))
    assert len(actual) == 1
    assert actual[0]["title"] == rows[0]["title"]  # Don't guess localized prefixes.
    assert actual[0]["is_ad"] and actual[0]["is_music"]
    assert actual[0]["payload"] == rows[0]
    assert video_id("https://www.youtube.com/shorts/FAKE_A") == "FAKE_A"
    assert video_id("https://example.invalid/watch?v=FAKE_A") is None


def test_msg_6_csv_bom_quoted_metadata_localized():
    text = '\ufeffPlaylist Id,Playlist Title\nFAKE_LIST,Invented\n\nIdentifiant,Titre,Ajout\nFAKE_A,"A, B",2020-01-01T12:00:00+05:45\n'
    rows = list(
        csv_rows(text, {"Identifiant": "video_id", "Titre": "title", "Ajout": "at"})
    )
    assert rows[0]["title"] == "A, B"
    assert rows[0]["payload"]["Identifiant"] == "FAKE_A"
    assert rows[0]["at"].endswith("+05:45")
    with pytest.raises(ValueError, match="Unknown Takeout"):
        list(csv_rows("Inconnu,Titre\nFAKE_A,Invented"))


def test_plg_2_5_live_first_seen_and_links(monkeypatch, tmp_path):
    path = live(monkeypatch, tmp_path)
    messages, links, cursor = collect(YouTube())
    history = [m for m in messages if m["channel"] == "youtube.history"]
    assert history == []
    assert all(r.data["method"] == "source" for r in links)
    assert {r.data["type"] for r in links} == {
        "published_by",
        "saved",
        "subscribed_to",
    }
    assert collect(YouTube(), cursor)[0] == []
    data = json.loads(path.read_text())
    data[":ythistory"].insert(0, dict(id="FAKE_NEW", title="Invented new view"))
    path.write_text(json.dumps(data))
    observed, links, _ = collect(YouTube(), cursor)
    assert len(observed) == 1
    assert observed[0]["happened_at"] == "2020-02-01T10:00:00+00:00"
    assert observed[0]["fields"]["time_precision"] == "first_seen"
    assert {r.data["type"] for r in links} == {"consumed"}


def test_msg_2_removal_reappearance_and_no_repeat_removal(monkeypatch, tmp_path):
    path = live(monkeypatch, tmp_path)
    initial, _, cursor = collect(YouTube())
    data = json.loads(path.read_text())
    data["api_pages"] = [{"items": []}]
    data[":ytwatchlater"] = []
    path.write_text(json.dumps(data))
    removed, _, cursor2 = collect(YouTube(), cursor)
    assert len(removed) == 4
    assert all(m["fields"]["removed"] for m in removed)
    assert {m["external_id"] for m in removed} <= {m["external_id"] for m in initial}
    assert collect(YouTube(), cursor2)[0] == []
    path.write_text((REC / "live.json").read_text())
    restored, _, _ = collect(YouTube(), cursor2)
    assert len(restored) == 4
    assert all(not m["fields"]["removed"] for m in restored)
    assert {m["external_id"]: m["happened_at"] for m in restored} == {
        m["external_id"]: m["happened_at"]
        for m in initial
        if m["channel"] != "youtube.history"
    }


def test_plg_4_reservation_and_every_checkpoint_resume(monkeypatch, tmp_path):
    live(monkeypatch, tmp_path)
    plugin = YouTube()
    events = list(plugin.records({}, "backfill"))
    final, _, _ = collect(plugin)
    expected = {m["external_id"]: m for m in final}
    accumulated = {}
    for event in events:
        if isinstance(event, dict):
            accumulated[event["external_id"]] = event
        if isinstance(event, Checkpoint):
            resumed, _, _ = collect(plugin, event.cursor)
            actual = {**accumulated, **{m["external_id"]: m for m in resumed}}
            assert actual == expected


def test_plg_6_fake_command_flat_output():
    rows = ytdlp(
        [sys.executable, str(REC / "fake_ytdlp.py")], "fake-browser", ":ythistory"
    )
    assert len(rows) == 2
    assert rows[0]["id"] == "FAKE_VIDEO_A"


def test_plg_6_api_http_pagination_refresh_and_scope_boundary():
    recording = json.loads((REC / "live.json").read_text())
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.host == "oauth2.googleapis.com":
            assert b"grant_type=refresh_token" in req.content
            return httpx.Response(200, json={"access_token": "fake-access-canary"})
        assert req.url.path == "/youtube/v3/subscriptions"
        assert req.url.params["mine"] == "true"
        assert req.url.params["part"] == "snippet"
        assert req.url.params["maxResults"] == "50"
        assert req.headers["Authorization"] == "Bearer fake-access-canary"
        return httpx.Response(
            200, json=recording["api_pages"][bool(req.url.params.get("pageToken"))]
        )

    rows = subscriptions(
        dict(
            client_id="fake-client",
            client_secret="fake-secret",
            refresh_token="fake-refresh",
        ),
        httpx.MockTransport(handler),
    )
    assert len(rows) == 2 and len(calls) == 3


def test_plg_6_api_error_sanitized():
    def handler(req):
        return httpx.Response(400, text="fake-secret-token")

    with pytest.raises(RuntimeError) as err:
        subscriptions(
            dict(
                client_id="fake-id",
                client_secret="fake-secret-token",
                refresh_token="fake-refresh",
            ),
            httpx.MockTransport(handler),
        )
    assert "fake-secret-token" not in str(err.value)


def test_plg_4_incomplete_snapshot_never_removes(monkeypatch, tmp_path):
    path = live(monkeypatch, tmp_path)
    _, _, cursor = collect(YouTube())
    before = copy.deepcopy(cursor)
    data = json.loads(path.read_text())
    data["api_pages"][-1]["nextPageToken"] = "fake-missing-page"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Incomplete"):
        collect(YouTube(), cursor)
    assert cursor == before


def test_plg_6_contract_takeout():
    env = {
        "YOUTUBE_TAKEOUT": str(REC / "takeout"),
        "YOUTUBE_RECORDING": str(REC / "live.json"),
        "YOUTUBE_API": "false",
        "YOUTUBE_YTDLP": "false",
        "YOUTUBE_CLIENT_SECRET": "fake-takeout-canary",
    }
    evidence = check_plugin(
        "throughline_youtube:YouTube", env, secrets=["fake-takeout-canary"]
    )
    assert evidence["messages"] == 8


def test_plg_6_contract_api_and_fake_command(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    history = next((REC / "takeout").rglob("watch-history.json"))
    (archive / "watch-history.json").write_text(
        history.read_text(encoding="utf-8"), encoding="utf-8"
    )
    data = json.loads((REC / "live.json").read_text())
    data["use_command"] = True
    path = tmp_path / "fake-command-recording.json"
    path.write_text(json.dumps(data))
    env = {
        "YOUTUBE_TAKEOUT": str(archive),
        "YOUTUBE_RECORDING": str(path),
        "YOUTUBE_COMMAND": json.dumps([sys.executable, str(REC / "fake_ytdlp.py")]),
        "YOUTUBE_API": "true",
        "YOUTUBE_YTDLP": "true",
        "YOUTUBE_BROWSER": "fake-browser-canary",
        "YOUTUBE_REFRESH_TOKEN": "fake-refresh-canary",
    }
    evidence = check_plugin(
        "throughline_youtube:YouTube",
        env,
        secrets=["fake-browser-canary", "fake-refresh-canary"],
    )
    assert evidence["messages"] == 8


def test_msg_2_playlist_duplicate_items_and_localized_folders(monkeypatch, tmp_path):
    folder = tmp_path / "listes"
    folder.mkdir()
    (folder / "Plus tard-videos.csv").write_text(
        "Identifiant,Ajout\nFAKE_A,2020-01-01T12:00:00Z\nFAKE_A,2020-01-02T12:00:00Z\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("YOUTUBE_TAKEOUT", str(tmp_path))
    monkeypatch.setenv("YOUTUBE_PLAYLIST_DIRS", '["listes"]')
    monkeypatch.setenv("YOUTUBE_WATCH_LATER_NAMES", '["Plus tard"]')
    monkeypatch.setenv("YOUTUBE_CSV_HEADERS", '{"Identifiant":"video_id","Ajout":"at"}')
    messages, _, cursor = collect(YouTube())
    assert len(messages) == 2
    assert len({m["external_id"] for m in messages}) == 2
    assert all(m["fields"]["playlist"] == "WL" for m in messages)
    assert collect(YouTube(), cursor)[0] == []


def test_plg_4_archive_removal_completion_checkpoint(monkeypatch, tmp_path):
    folder = tmp_path / "playlists"
    folder.mkdir()
    path = folder / "WL.csv"
    path.write_text(
        "Video ID,Time Added\nFAKE_A,2020-01-01T12:00:00Z\nFAKE_B,2020-01-02T12:00:00Z\n"
    )
    monkeypatch.setenv("YOUTUBE_TAKEOUT", str(tmp_path))
    _, _, cursor = collect(YouTube())
    path.write_text("Video ID,Time Added\n")
    events = list(YouTube().records(cursor, "sync"))
    removed = [i for i in events if isinstance(i, dict)]
    assert len(removed) == 2 and all(m["fields"]["removed"] for m in removed)
    # At every acknowledged checkpoint, remaining removals are still replayed.
    seen = {}
    for event in events:
        if isinstance(event, dict):
            seen[event["external_id"]] = event
        elif isinstance(event, Checkpoint):
            resumed, _, _ = collect(YouTube(), event.cursor)
            assert len({**seen, **{m["external_id"]: m for m in resumed}}) == 2


def test_plg_4_6_api_retry_bounded(monkeypatch):
    from throughline_youtube import sources

    attempts = []
    delays = []
    monkeypatch.setattr(sources.time, "sleep", delays.append)

    def handler(req):
        attempts.append(req)
        return httpx.Response(429, json={"error": "invented quota failure"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="OAuth/API request failed"):
            sources.request(client, "GET", "https://example.invalid/invented")
    assert len(attempts) == 5 and len(delays) == 4


def test_plg_6_ytdlp_failed_or_partial_output_never_becomes_empty_snapshot(monkeypatch):
    from types import SimpleNamespace
    from throughline_youtube import sources

    for result in [
        SimpleNamespace(returncode=1, stdout="fake-private-stderr"),
        SimpleNamespace(returncode=0, stdout='{"entries":[null]}'),
        SimpleNamespace(returncode=0, stdout='{"entries":null}'),
    ]:
        monkeypatch.setattr(sources.subprocess, "run", lambda *a, **k: result)
        with pytest.raises((RuntimeError, ValueError)) as err:
            ytdlp(["fake-command"], "fake-browser", ":ytwatchlater")
        assert "fake-private-stderr" not in str(err.value)


def test_plg_6_auth_pkce_token_file_no_stdout_secret(monkeypatch, tmp_path, capsys):
    import base64
    import hashlib
    import io
    from urllib.parse import parse_qs, urlparse
    from throughline_youtube import auth

    browser = {}
    target = tmp_path / "private-token.json"
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setattr(sys, "argv", ["auth", "--output", str(target)])

    def open_browser(url):
        browser.update(parse_qs(urlparse(url).query))
        return True

    class Server:
        server_port = 12345

        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 0)
            self.handler = handler

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def handle_request(self):
            h = object.__new__(self.handler)
            h.path = "/?state=" + browser["state"][0] + "&code=fake-auth-code"
            h.send_response = lambda status: None
            h.end_headers = lambda: None
            h.wfile = io.BytesIO()
            h.do_GET()

    class Response:
        status_code = 200

        def json(self):
            return {"refresh_token": "fake-refresh-token-private"}

    class HTTP:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, data):
            assert url == "https://oauth2.googleapis.com/token"
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(data["code_verifier"].encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            assert challenge == browser["code_challenge"][0]
            assert browser["scope"] == [auth.SCOPE]
            assert browser["access_type"] == ["offline"]
            assert browser["prompt"] == ["consent"]
            assert data["redirect_uri"] == browser["redirect_uri"][0]
            return Response()

    monkeypatch.setattr(auth.webbrowser, "open", open_browser)
    monkeypatch.setattr(auth, "HTTPServer", Server)
    monkeypatch.setattr(auth.httpx, "Client", HTTP)
    auth.main()
    assert json.loads(target.read_text()) == {
        "refresh_token": "fake-refresh-token-private"
    }
    assert "fake-refresh-token-private" not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        auth.main()
    assert (
        json.loads(target.read_text())["refresh_token"] == "fake-refresh-token-private"
    )


def test_msg_2_takeout_then_live_history_is_silent_baseline(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_TAKEOUT", str(REC / "takeout"))
    _, _, cursor = collect(YouTube())
    path = live(monkeypatch, tmp_path)
    rows, _, cursor = collect(YouTube(), cursor)
    assert not [r for r in rows if r["channel"] == "youtube.history"]
    data = json.loads(path.read_text())
    data[":ythistory"].insert(0, dict(id="FAKE_NEW_VIEW", title="Invented new view"))
    path.write_text(json.dumps(data))
    rows, _, cursor = collect(YouTube(), cursor)
    views = [r for r in rows if r["channel"] == "youtube.history"]
    assert len(views) == 1 and views[0]["fields"]["video_id"] == "FAKE_NEW_VIEW"
    assert views[0]["fields"]["time_precision"] == "first_seen"
    assert not collect(YouTube(), cursor)[0]


def test_msg_2_watch_later_sibling_position_does_not_version(monkeypatch, tmp_path):
    path = live(monkeypatch, tmp_path)
    _, _, cursor = collect(YouTube())
    data = json.loads(path.read_text())
    data[":ytwatchlater"].insert(0, dict(id="FAKE_NEW_SAVE", title="Invented new save"))
    path.write_text(json.dumps(data))
    rows, _, cursor = collect(YouTube(), cursor)
    assert len(rows) == 1 and rows[0]["fields"]["video_id"] == "FAKE_NEW_SAVE"
    data[":ytwatchlater"].pop(0)
    path.write_text(json.dumps(data))
    rows, _, _ = collect(YouTube(), cursor)
    assert len(rows) == 1 and rows[0]["fields"]["removed"]
