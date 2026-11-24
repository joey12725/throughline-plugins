import json
from urllib.parse import parse_qs

import pytest

from throughline_youtube_feed import fetch, mapping

KEY = "invented-secret-canary-router-key"


def upload(yt, n, published, channel=None, title=None, link=None):
    channel = channel or yt.CH1
    return {
        "kind": "upload",
        "id": yt.vid(n),
        "channel_id": channel,
        "transport": "rss",
        "fetched_at": yt.NOW.isoformat(),
        "raw": {
            "video_id": yt.vid(n),
            "channel_id": channel,
            "title": title or f"Invented upload {n}",
            "link": link or f"https://www.youtube.com/watch?v={yt.vid(n)}",
            "author": {"name": "Invented One"},
            "published": published,
            "media": {"description": f"Invented description {n}"},
        },
    }


def chat(server, answers):
    """Fake OpenAI-compatible endpoint answering per video title."""

    def handler(request):
        body = json.loads(request.body)
        video = json.loads(body["messages"][1]["content"])
        answer = answers.get(video["title"], answers.get("*"))
        if isinstance(answer, tuple):
            return answer
        return (
            200,
            {},
            {
                "model": body["model"],
                "choices": [{"message": {"content": answer}}],
                "usage": {"total_tokens": 42},
            },
        )

    server.routes[("POST", "/v1/chat/completions")] = handler


def router_cfg(cfg, server, **extra):
    return cfg(
        router="1",
        router_url=server.url + "/v1",
        router_model="invented-model",
        router_api_key=KEY,
        **extra,
    )


def test_router_off_by_default_makes_no_calls(server, yt, cfg):
    picks, stats = fetch.route(cfg(), {}, [upload(yt, 1, "2026-10-01T10:00:00+00:00")])
    assert picks == [] and server.requests == []
    with pytest.raises(ValueError):
        fetch.route(cfg(router="1"), {}, [])


def test_router_scores_with_thresholds_limits_and_cache(server, yt, cfg, tmp_path):
    chat(
        server,
        {
            "Invented upload 1": 'Sure: {"watch_probability": 0.9, "progress_if_watched": 80, "junk_food_probability": 0.1, "watch_later_probability": 0.4, "reason": "Matches interests"}',
            "Invented upload 2": '{"watch_probability": 0.9, "progress_if_watched": 80, "junk_food_probability": 0.7}',
            "Invented upload 3": "no json here",
            "*": '{"watch_probability": 75, "progress_if_watched": 0.4, "junk_food_probability": 0.2}',
        },
    )
    uploads = [
        upload(yt, 1, "2026-10-01T10:00:00+00:00"),
        upload(yt, 2, "2026-10-01T09:00:00+00:00"),
        upload(yt, 3, "2026-10-01T08:00:00+00:00"),
        upload(yt, 4, "2026-10-01T07:00:00+00:00"),
        upload(yt, 5, "2026-09-20T07:00:00+00:00"),  # older than 72 h
        upload(
            yt,
            6,
            "2026-10-01T06:00:00+00:00",
            link="https://www.youtube.com/shorts/" + yt.vid(6),
        ),
    ]
    state = {}
    config = router_cfg(
        cfg, server, cache_dir=str(tmp_path / "cache"), router_per_run="3"
    )
    picks, stats = fetch.route(config, state, uploads)
    assert [p["video_id"] for p in picks] == [yt.vid(1), yt.vid(2)]
    assert stats["router_errors"] == 1 and stats["scored"] == 2
    one, two = (p["decision"] for p in picks)
    assert one["verdict"] == "pick" and one["reason"] == "Matches interests"
    assert two["verdict"] == "skip" and two["is_junk_food"] is True
    assert picks[0]["id"] == yt.vid(1) + ":invented-model"
    calls = server.hits("POST", "/v1/chat/completions")
    assert len(calls) == 3 and all(
        c.headers["Authorization"] == "Bearer " + KEY for c in calls
    )
    assert json.loads(calls[0].body)["temperature"] == 0
    assert KEY not in json.dumps(picks) + json.dumps(state)
    # Next run: scored videos are skipped, the failed one is retried once, then 4.
    picks, _ = fetch.route(config, state, uploads)
    assert [p["video_id"] for p in picks] == [yt.vid(4)]
    d = picks[0]["decision"]
    assert d["watch_probability"] == 0.75 and d["progress_if_watched"] == 40.0
    assert d["verdict"] == "skip"
    picks, _ = fetch.route(config, state, uploads)
    assert picks == [] and state["scored"][yt.vid(3)]["attempts"] == 2
    # Cache: a lost state does not pay again for answered videos.
    before = len(server.hits("POST"))
    picks, stats = fetch.route(config, {}, uploads[:2])
    assert (
        len(picks) == 2
        and stats["cache_hits"] == 2
        and len(server.hits("POST")) == before
    )


def test_router_daily_limit_and_threshold_file(server, yt, cfg, tmp_path):
    chat(
        server,
        {
            "*": '{"watch_probability": 0.6, "progress_if_watched": 60, "junk_food_probability": 0.1}'
        },
    )
    limits = tmp_path / "thresholds.json"
    limits.write_text(
        json.dumps(
            {
                "watch_probability": 0.5,
                "progress_if_watched": 50,
                "junk_food_probability": 0.5,
            }
        )
    )
    config = router_cfg(cfg, server, router_per_day="2", thresholds=str(limits))
    state = {}
    uploads = [upload(yt, n, f"2026-10-01T0{n}:00:00+00:00") for n in range(1, 5)]
    picks, stats = fetch.route(config, state, uploads)
    assert len(picks) == 2 and stats["limit"] == "router_per_day"
    assert all(p["decision"]["verdict"] == "pick" for p in picks)
    assert picks[0]["decision"]["thresholds"] == {
        "min_watch": 0.5,
        "min_progress": 50.0,
        "max_junk": 0.5,
    }
    assert fetch.route(config, state, uploads)[0] == []


def test_router_http_errors_are_counted_not_fatal(server, yt, cfg):
    chat(server, {"*": (401, {}, "bad key")})
    picks, stats = fetch.route(
        router_cfg(cfg, server), {}, [upload(yt, 1, "2026-10-01T10:00:00+00:00")]
    )
    assert picks == [] and stats["router_errors"] == 1


def picked(yt, n, verdict="pick", scored="2026-10-01T11:00:00+00:00"):
    return {
        "kind": "pick",
        "id": f"{yt.vid(n)}:invented-model",
        "video_id": yt.vid(n),
        "scorer": "invented-model",
        "origin": "router",
        "decision": {
            "verdict": verdict,
            "scored_at": scored,
            "video": {"video_id": yt.vid(n)},
        },
    }


def test_playlist_dry_run_by_default(server, yt, cfg):
    records, stats = fetch.playlist(
        cfg(playlist="PLinventedinventedinvented"),
        {},
        [picked(yt, 1), picked(yt, 2, "skip")],
    )
    assert records == [] and stats["planned"] == [yt.vid(1)] and server.requests == []
    assert fetch.playlist(cfg(), {}, [picked(yt, 1)])[1]["planned"] == []


def test_playlist_apply_refreshes_token_resolves_title_and_limits(server, yt, cfg):
    server.route(
        "POST",
        "/token",
        (200, {}, {"access_token": "invented-access-token", "expires_in": 3600}),
    )

    def playlists(request):
        assert request.headers["Authorization"] == "Bearer invented-access-token"
        if "pageToken" not in parse_qs(request.query):
            return (
                200,
                {},
                {
                    "items": [{"id": "PLother", "snippet": {"title": "Other"}}],
                    "nextPageToken": "p2",
                },
            )
        return (
            200,
            {},
            {
                "items": [
                    {"id": "PLinventedpicks0001", "snippet": {"title": "Feed Picks"}}
                ]
            },
        )

    server.routes[("GET", "/youtube/v3/playlists")] = playlists
    server.route(
        "POST",
        "/youtube/v3/playlistItems",
        (200, {}, {"id": "invented-item"}),
        (403, {}, {"error": "quota"}),
    )
    config = cfg(
        playlist="feed picks",
        playlist_apply="1",
        playlist_per_day="2",
        client_id="invented-client",
        client_secret="invented-secret-canary-client",
        refresh_token="invented-secret-canary-refresh",
    )
    state = {}
    picks = [
        picked(yt, 1),
        picked(yt, 2),
        picked(yt, 3),
        picked(yt, 4, scored="2026-09-01T00:00:00+00:00"),
    ]
    records, stats = fetch.playlist(config, state, picks)
    assert [r["video_id"] for r in records] == [yt.vid(1)]
    assert (
        records[0]["playlist_id"] == "PLinventedpicks0001"
        and records[0]["item_id"] == "invented-item"
    )
    assert stats["playlist_errors"] == 1 and stats["limit"] == "playlist_per_day"
    token_body = parse_qs(server.hits("POST", "/token")[0].body.decode())
    assert token_body["grant_type"] == ["refresh_token"]
    insert = json.loads(server.hits("POST", "/youtube/v3/playlistItems")[0].body)
    assert insert == {
        "snippet": {
            "playlistId": "PLinventedpicks0001",
            "resourceId": {"kind": "youtube#video", "videoId": yt.vid(1)},
        }
    }
    assert "invented-secret-canary" not in json.dumps(records) + json.dumps(state)
    # Added or failed videos are not retried; the daily limit is spent.
    assert fetch.playlist(config, state, picks)[0] == []
    with pytest.raises(ValueError):
        fetch.playlist(cfg(playlist="WL", playlist_apply="1"), {}, [picked(yt, 9)])


def test_import_predictions_and_flagged_merge(yt, tmp_path):
    lines = [
        yt.prediction(1, scored="2026-09-20T10:00:00+00:00", watch=0.2, flagged=False),
        yt.prediction(
            1, scored="2026-09-21T10:00:00+00:00", watch=0.9
        ),  # re-scored later
        yt.prediction(2, watch=0.9, progress=80, flagged=False),
    ]
    (tmp_path / "predictions.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines)
    )
    flagged = [
        dict(yt.prediction(2, watch=0.9, progress=80, flagged=False), flagged=True)
    ]
    (tmp_path / "flagged.json").write_text(json.dumps(flagged))
    records = fetch.read_records([tmp_path], "jev")
    assert {r["origin"] for r in records} == {"predictions", "flagged"}
    groups = {}
    for r in records:
        groups.setdefault(r["id"], []).append(r)
    one = fetch.pick_decision(groups[yt.vid(1) + ":jev"])
    assert one["scored_at"] == "2026-09-21T10:00:00+00:00" and one["verdict"] == "pick"
    two = fetch.pick_decision(groups[yt.vid(2) + ":jev"])
    assert (
        two["verdict"] == "pick"
        and two["thresholds"] is None
        and two["origin"] == "predictions"
    )
    assert (
        two["video"]["duration_s"] == 602
        and two["video"]["channel_title"] == "Invented Channel One"
    )
    # Order of files does not change the decision.
    assert fetch.pick_decision(list(reversed(groups[yt.vid(2) + ":jev"]))) == two
    # Without a flagged field the configured thresholds decide.
    bare = {
        k: v
        for k, v in yt.prediction(3, watch=0.75, progress=55, junk=0.2).items()
        if k != "flagged"
    }
    decision = fetch.pick_decision([fetch.prediction_record(bare, "jev")])
    assert decision["verdict"] == "pick" and decision["thresholds"]["min_watch"] == 0.7


# Independent checks: credential safety, playlist robustness, subject precision.

OAUTH = dict(
    client_id="invented-client",
    client_secret="invented-secret-canary-client",
    refresh_token="invented-secret-canary-refresh",
)


def test_router_never_follows_a_redirect_with_its_key(server, yt, cfg):
    server.route(
        "POST",
        "/v1/chat/completions",
        (307, {"Location": server.url + "/elsewhere"}, ""),
    )
    picks, stats = fetch.route(
        router_cfg(cfg, server), {}, [upload(yt, 1, "2026-10-01T10:00:00+00:00")]
    )
    assert picks == [] and stats["router_errors"] == 1
    assert server.hits(path="/elsewhere") == []


def test_token_endpoint_redirect_is_refused(server, yt, cfg):
    server.route("POST", "/token", (302, {"Location": server.url + "/steal"}, ""))
    config = cfg(playlist="PLinventedinventedinvented", playlist_apply="1", **OAUTH)
    records, stats = fetch.playlist(config, {}, [picked(yt, 1)])
    assert records == [] and stats["playlist_errors"] == 1
    assert "redirect refused" in stats["playlist_setup_error"]
    assert server.hits(path="/steal") == []
    assert "invented-secret-canary" not in json.dumps(stats)


def test_credentials_only_over_https_or_loopback(yt, cfg):
    with pytest.raises(ValueError, match="ROUTER_URL"):
        fetch.route(
            cfg(
                router="1",
                router_url="http://router.invalid/v1",
                router_model="m",
                router_api_key=KEY,
            ),
            {},
            [upload(yt, 1, "2026-10-01T10:00:00+00:00")],
        )
    with pytest.raises(ValueError, match="TOKEN_URL"):
        fetch.check_playlist(
            cfg(
                playlist="WL",
                playlist_apply="1",
                token_url="http://token.invalid/t",
                **OAUTH,
            )
        )
    assert (
        fetch.secure_url({"u": "https://api.invalid/v1"}, "u")
        == "https://api.invalid/v1"
    )
    # A dry run needs no credentials and no URL checks.
    fetch.check_playlist(cfg(playlist="WL", token_url="http://token.invalid/t"))


def test_playlist_setup_failure_keeps_picks_and_does_not_loop(server, yt, cfg):
    server.route("POST", "/token", (200, {}, {"access_token": "invented-access-token"}))
    # A broken API that always returns the same next page token.
    server.route(
        "GET",
        "/youtube/v3/playlists",
        (200, {}, {"items": [], "nextPageToken": "same"}),
    )
    config = cfg(playlist="Invented title", playlist_apply="1", **OAUTH)
    state = {}
    records, stats = fetch.playlist(config, state, [picked(yt, 1)])
    assert records == [] and stats["playlist_errors"] == 1
    assert len(server.hits("GET", "/youtube/v3/playlists")) == 2
    # Nothing is marked added, so the next run tries again.
    assert state.get("added", {}) == {}


def test_cli_checks_playlist_setup_before_any_request(
    server, yt, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "YOUTUBE_FEED_RSS_URL", server.url + "/feeds/videos.xml?channel_id={channel_id}"
    )
    with pytest.raises(ValueError, match="playlist writes need"):
        fetch.main(
            [
                "export",
                "--out",
                str(tmp_path / "o.jsonl"),
                "--channel-id",
                yt.CH1,
                "--playlist",
                "WL",
                "--apply",
            ]
        )
    assert server.requests == []


def test_pick_subject_keeps_published_precision(server, yt, cfg):
    page = upload(yt, 1, None)
    page.update(transport="page", estimated_published_at="2026-10-01T09:00:00+00:00")
    page["raw"] = {
        "video_id": yt.vid(1),
        "title": "Invented upload 1",
        "channel_title": "Invented One",
        "short": False,
    }
    chat(
        server,
        {
            "*": '{"watch_probability": 0.9, "progress_if_watched": 80, "junk_food_probability": 0.1}'
        },
    )
    picks, _ = fetch.route(router_cfg(cfg, server), {}, [page])
    decision = fetch.pick_decision(picks)
    message, _ = mapping.pick_message(picks[0]["id"], decision, picks, [], True)
    assert message["subject"] == {
        "start": "2026-10-01T09:00:00+00:00",
        "start_precision": "approximate",
    }

    line = yt.prediction(2, published="2026-09-20")
    record = fetch.prediction_record(line, "jev")
    decision = fetch.pick_decision([record])
    message, _ = mapping.pick_message(record["id"], decision, [record], [], True)
    assert message["subject"] == {
        "start": "2026-09-20T00:00:00+00:00",
        "start_precision": "day",
    }
    # Exact published times keep the default precision and the same hash as before.
    exact = fetch.prediction_record(yt.prediction(3), "jev")
    message, _ = mapping.pick_message(
        exact["id"], fetch.pick_decision([exact]), [exact], [], True
    )
    assert message["subject"] == {"start": "2026-09-20T08:00:00+00:00"}
