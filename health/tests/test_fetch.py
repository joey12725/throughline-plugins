"""fetch.py: the standard-library fetcher, against the local fake API only."""

import ast
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from throughline_health import fetch

FETCH = Path(fetch.__file__)


def http(**kw):
    kw.setdefault("backoff", 0)
    kw.setdefault("sleep", lambda s: None)
    return fetch.Http(**kw)


OURA_TOKEN = "fake-oura-access-canary"  # the fake server's accepted token


def oura_client(server, env=None, **kw):
    client = http(**kw)
    auth = fetch.OuraAuth(env or {"OURA_ACCESS_TOKEN": OURA_TOKEN}, client)
    return fetch.Oura(client, auth, server.url), client


def read_jsonl(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_fetch_is_standard_library_only():
    tree = ast.parse(FETCH.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "fetch.py must not use relative imports"
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names), modules - set(
        sys.stdlib_module_names
    )


def test_retries_429_and_5xx_with_backoff(server):
    slept = []
    oura, client = oura_client(server)
    client.sleep = slept.append
    client.backoff = 0.5
    server.failures["/v2/usercollection/daily_sleep"] = [429, 503, 500]
    docs = oura.documents("daily_sleep", date(2024, 3, 29), date(2024, 3, 29))
    assert [d["id"] for d in docs] == ["fake-daily-sleep-0"]
    assert slept == [0.5, 1.0, 2.0]  # Retry-After 0 never shortens the backoff
    assert client.count == 4


def test_gives_up_after_retries_and_does_not_retry_client_errors(server):
    oura, client = oura_client(server, retries=2)
    server.failures["/v2/usercollection/daily_sleep"] = [500, 500, 500, 500]
    with pytest.raises(fetch.HttpError) as error:
        oura.get("daily_sleep", {"start_date": "2024-03-29", "end_date": "2024-03-30"})
    assert error.value.status == 500 and client.count == 3
    server.failures["/v2/usercollection/daily_sleep"] = [400, 200]
    before = client.count
    with pytest.raises(fetch.HttpError):
        oura.get("daily_sleep", {"start_date": "2024-03-29", "end_date": "2024-03-30"})
    assert client.count == before + 1


def test_request_budget_is_enforced(server):
    oura, client = oura_client(server, max_requests=2)
    with pytest.raises(fetch.BudgetExhausted):
        # 5 days at page size 2 needs 3 pages.
        oura.documents("daily_sleep", date(2024, 3, 29), date(2024, 4, 2))
    assert client.count == 2


def test_redirects_are_refused(server):
    server.redirects.add("/v2/usercollection/workout")
    oura, _ = oura_client(server)
    with pytest.raises(fetch.HttpError) as error:
        oura.get("workout", {})
    assert error.value.status == 302
    assert not [r for r in server.requests if r["path"] == "/elsewhere"]


def test_base_urls_must_be_https_unless_loopback():
    assert (
        fetch.check_base("https://api.example.invalid/", "X")
        == "https://api.example.invalid"
    )
    assert fetch.check_base("http://127.0.0.1:9", "X") == "http://127.0.0.1:9"
    for bad in (
        "http://api.example.invalid",
        "https://user:pw@api.example.invalid",
        "https://a.invalid/?q=1",
        "ftp://a.invalid",
    ):
        with pytest.raises(fetch.FetchError):
            fetch.check_base(bad, "X")


def test_pagination_day_filter_and_inclusive_window(server):
    oura, _ = oura_client(server)
    docs = oura.documents("daily_activity", date(2024, 3, 30), date(2024, 4, 1))
    assert [d["day"] for d in docs] == ["2024-03-30", "2024-03-31", "2024-04-01"]
    query = [
        r["query"] for r in server.requests if r["path"].endswith("daily_activity")
    ][0]
    assert query == {"start_date": "2024-03-30", "end_date": "2024-04-02"}


def test_heartrate_is_chunked_to_30_days_and_grouped_by_utc_day(server):
    oura, _ = oura_client(server)
    items = oura.documents("heartrate", date(2024, 2, 1), date(2024, 4, 15))
    windows = [
        r["query"]
        for r in server.requests
        if r["path"].endswith("heartrate") and "next_token" not in r["query"]
    ]
    assert len(windows) == 3
    assert windows[0]["start_datetime"] == "2024-02-01T00:00:00+00:00"
    assert windows[-1]["end_datetime"] == "2024-04-16T00:00:00+00:00"
    days = fetch.heartrate_days(items)
    assert list(days) == ["2024-03-30", "2024-03-31"]
    assert len(days["2024-03-30"]) == 12
    assert fetch.heartrate_days(
        [{"bpm": 60, "timestamp": "2024-03-30T23:30:00-05:00", "source": "awake"}]
    ).keys() == {"2024-03-31"}


def test_oura_window_skips_refused_collection_and_marks_window(server):
    oura, _ = oura_client(server)
    server.failures["/v2/usercollection/daily_spo2"] = [403]
    skipped = set()
    out = fetch.oura_window(
        oura,
        ("daily_spo2", "daily_cardiovascular_age", "heartrate"),
        date(2024, 3, 30),
        date(2024, 3, 31),
        skipped,
    )
    kinds = [r["kind"] for r in out]
    assert "oura.daily_spo2" not in kinds
    assert kinds.count("oura.heartrate") == 2
    cv = [r for r in out if r["kind"] == "oura.daily_cardiovascular_age"]
    assert [r["id"] for r in cv] == [
        "daily_cardiovascular_age:2024-03-30",
        "daily_cardiovascular_age:2024-03-31",
    ]
    marker = out[-1]
    assert marker["kind"] == "export.window" and marker["payload"]["skipped"] == [
        "daily_spo2"
    ]
    assert skipped == {"daily_spo2"}
    assert len({r["fetched_at"] for r in out}) == 1


def test_401_without_refresh_never_calls_token_endpoint(server, tmp_path):
    env = {
        "OURA_ACCESS_TOKEN": "fake-stale-token",
        "OURA_REFRESH_TOKEN": "fake-refresh-canary",
        "OURA_CLIENT_ID": "fake-client",
        "OURA_CLIENT_SECRET": "fake-client-secret-canary",
        "OURA_TOKEN_URL": server.url + "/oauth/token",
    }
    oura, _ = oura_client(server, env)
    with pytest.raises(fetch.AuthError) as error:
        oura.get("daily_sleep", {})
    assert "refresh is disabled" in str(error.value)
    assert "fake-stale-token" not in str(error.value)
    assert not server.paths("/oauth")


def test_token_file_is_reread_after_401(server, tmp_path):
    token_file = tmp_path / "oura-tokens.json"
    token_file.write_text(
        json.dumps(
            {"access_token": "fake-old", "refresh_token": "fake-owned-elsewhere"}
        )
    )
    oura, _ = oura_client(
        server,
        {"OURA_TOKEN_FILE": str(token_file), "OURA_ACCESS_TOKEN": "fake-ignored"},
    )
    assert oura.auth.token() == "fake-old"
    # The owning app rotates the pair; this fetcher only reads the new token.
    token_file.write_text(
        json.dumps({"access_token": OURA_TOKEN, "refresh_token": "fake-rotated"})
    )
    assert oura.get(
        "daily_sleep", {"start_date": "2024-03-29", "end_date": "2024-03-30"}
    )["data"]
    assert json.loads(token_file.read_text())["refresh_token"] == "fake-rotated"
    assert not server.paths("/oauth")
    plain = tmp_path / "token.txt"
    plain.write_text(OURA_TOKEN + "\n")
    oura, _ = oura_client(server, {"OURA_TOKEN_FILE": str(plain)})
    assert oura.get("personal_info", None) == {"id": "fake-user"}


def test_refresh_only_with_opt_in_and_own_client(server, tmp_path):
    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "fake-expired",
                "refresh_token": "fake-refresh-1",
                "provider": "oura",
            }
        )
    )
    server.refresh = {
        "refresh_token": "fake-refresh-1",
        "access_token": "fake-new-access",
        "new_refresh_token": "fake-refresh-2",
    }
    env = {
        "OURA_TOKEN_FILE": str(token_file),
        "OURA_ALLOW_REFRESH": "1",
        "OURA_TOKEN_URL": server.url + "/oauth/token",
    }
    oura, _ = oura_client(server, env)
    with pytest.raises(fetch.AuthError) as error:
        oura.get("daily_sleep", {})
    assert "own OURA_CLIENT_ID" in str(error.value)
    env.update(OURA_CLIENT_ID="fake-client", OURA_CLIENT_SECRET="fake-secret")
    oura, _ = oura_client(server, env)
    assert oura.get(
        "daily_sleep", {"start_date": "2024-03-29", "end_date": "2024-03-30"}
    )["data"]
    stored = json.loads(token_file.read_text())
    assert stored["access_token"] == "fake-new-access"
    assert stored["refresh_token"] == "fake-refresh-2"
    assert stored["provider"] == "oura" and stored["expires_at"] > 0
    assert not (tmp_path / "tokens.json.lock").exists()
    form = [r["body"].decode() for r in server.requests if r["path"] == "/oauth/token"]
    assert len(form) == 1 and "grant_type=refresh_token" in form[0]


def test_weightgurus_login_in_memory_and_errors_hide_password(server, creds):
    client = fetch.WeightGurus(http(), creds.wg_email, creds.wg_password, server.url)
    records = fetch.weightgurus_window(client)
    assert [r["kind"] for r in records].count("weightgurus.operation") == 5
    assert (
        records[0]["id"] == "2024-03-29T13:00:00.000Z|create|2024-03-29T13:00:05.000Z"
    )
    get = [r for r in server.requests if r["path"] == "/v3/operation/"][0]
    assert (
        get["query"] == {"start": fetch.WEIGHTGURUS_START}
        and get["auth"] == "Bearer " + creds.wg_access
    )
    bad = fetch.WeightGurus(http(), creds.wg_email, "fake-wrong-password", server.url)
    with pytest.raises(fetch.AuthError) as error:
        bad.operations()
    assert "fake-wrong-password" not in str(error.value) and creds.wg_email not in str(
        error.value
    )


def export_args(out, server_env, *extra):
    return [
        "export",
        "--out",
        str(out),
        "--min-interval",
        "0",
        "--backoff",
        "0",
        *extra,
    ]


def test_export_writes_complete_windows_and_state(
    server, tmp_path, capsys, fake_env, creds
):
    out, state = tmp_path / "export.jsonl", tmp_path / "state.json"
    code = fetch.main(
        export_args(
            out,
            None,
            "--start",
            "2024-03-25",
            "--end",
            "2024-04-05",
            "--window-days",
            "5",
            "--state",
            str(state),
        ),
        fake_env,
    )
    assert code == 0
    rows = read_jsonl(out)
    assert rows[-1]["kind"] == "export.end"
    summary = rows[-1]["payload"]
    assert summary["complete"] is True and summary["oura_next"] == "2024-04-06"
    assert (
        summary["counts"]["oura.daily_sleep"] == 5
        and summary["counts"]["weightgurus.operation"] == 5
    )
    assert (
        sum(r["kind"] == "export.window" for r in rows) == 4
    )  # WG + three Oura windows
    assert json.loads(state.read_text())["oura_next"] == "2024-04-06"
    assert not list(tmp_path.glob("*.partial"))
    text = out.read_text() + capsys.readouterr().err
    for secret in (OURA_TOKEN, creds.wg_password, creds.wg_access, creds.wg_email):
        assert secret not in text
    if os.name == "posix":
        assert (out.stat().st_mode & 0o777) == 0o600


def test_export_stops_at_budget_and_resumes_from_state(
    server, tmp_path, fake_env, creds
):
    state = tmp_path / "state.json"
    first = tmp_path / "a.jsonl"
    code = fetch.main(
        export_args(
            first,
            None,
            "--sources",
            "oura",
            "--start",
            "2024-03-25",
            "--end",
            "2024-04-05",
            "--window-days",
            "4",
            "--collections",
            "daily_sleep,sleep",
            "--max-requests",
            "7",
            "--state",
            str(state),
        ),
        fake_env,
    )
    assert code == 3
    rows = read_jsonl(first)
    end = rows[-1]["payload"]
    assert end["complete"] is False and end["stopped"].startswith("request limit")
    # Only whole windows were written: 2024-03-25..28 and 03-29..04-01.
    windows = [r["payload"] for r in rows if r["kind"] == "export.window"]
    assert [w["end"] for w in windows] == ["2024-03-28", "2024-04-01"]
    assert json.loads(state.read_text())["oura_next"] == "2024-04-02"
    assert json.loads(state.read_text())["requests"]["used"] == 7
    second = tmp_path / "b.jsonl"
    before = len(server.requests)
    code = fetch.main(
        export_args(
            second,
            None,
            "--sources",
            "oura",
            "--end",
            "2024-04-05",
            "--window-days",
            "4",
            "--collections",
            "daily_sleep,sleep",
            "--overlap-days",
            "1",
            "--state",
            str(state),
        ),
        fake_env,
    )
    assert code == 0
    first_query = server.requests[before]["query"]
    assert first_query["start_date"] == "2024-04-01"  # oura_next minus one overlap day


def test_per_day_budget_with_state(server, tmp_path, fake_env, creds):
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"requests": {"day": date.today().isoformat(), "used": 10}})
    )
    code = fetch.main(
        export_args(
            tmp_path / "x.jsonl",
            None,
            "--sources",
            "weightgurus",
            "--max-requests-per-day",
            "10",
            "--state",
            str(state),
        ),
        fake_env,
    )
    assert code == 3 and not server.requests


def test_export_into_directory_and_check(server, tmp_path, capsys, fake_env, creds):
    directory = tmp_path / "exports"
    directory.mkdir()
    code = fetch.main(
        export_args(directory, None, "--sources", "weightgurus"), fake_env
    )
    assert code == 0
    files = list(directory.glob("health-*.jsonl"))
    assert len(files) == 1
    assert fetch.main(["check", "--min-interval", "0"], fake_env) == 0
    err = capsys.readouterr().err
    assert "oura: ok" in err and "weightgurus: ok" in err and "fake-user" not in err


def test_runs_as_a_plain_script_without_site_packages(
    server, tmp_path, fake_env, creds
):
    out = tmp_path / "script.jsonl"
    env = dict(
        fake_env,
        PATH=os.environ.get("PATH", ""),
        SYSTEMROOT=os.environ.get("SYSTEMROOT", ""),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(FETCH),
            "export",
            "--out",
            str(out),
            "--start",
            "2024-03-29",
            "--end",
            "2024-03-31",
            "--min-interval",
            "0",
            "--collections",
            "daily_sleep,heartrate",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    rows = read_jsonl(out)
    assert {r["kind"] for r in rows} >= {
        "oura.daily_sleep",
        "oura.heartrate",
        "weightgurus.operation",
        "export.end",
    }
    for secret in (OURA_TOKEN, creds.wg_password, creds.wg_access):
        assert secret not in result.stdout + result.stderr + out.read_text()


def test_credentials_are_not_accepted_on_the_command_line():
    with pytest.raises(SystemExit):
        fetch.parser().parse_args(["export", "--out", "x", "--token", "fake"])


def test_weightgurus_failure_still_exports_oura(server, tmp_path, capsys, fake_env):
    out, state = tmp_path / "export.jsonl", tmp_path / "state.json"
    env = dict(fake_env, WEIGHTGURUS_PASSWORD="fake-wrong-password")
    code = fetch.main(
        export_args(
            out,
            None,
            "--start",
            "2024-03-29",
            "--end",
            "2024-03-31",
            "--state",
            str(state),
        ),
        env,
    )
    assert code == 1
    rows = read_jsonl(out)
    kinds = {r["kind"] for r in rows}
    assert "oura.daily_sleep" in kinds and "weightgurus.operation" not in kinds
    summary = rows[-1]["payload"]
    assert (
        summary["complete"] is False
        and "Weight Gurus login failed" in (summary["stopped"])
    )
    assert json.loads(state.read_text())["oura_next"] == "2024-04-01"
    assert "fake-wrong-password" not in out.read_text() + capsys.readouterr().err


def test_exports_in_the_same_second_never_replace_each_other(
    server, tmp_path, fake_env
):
    directory = tmp_path / "exports"
    directory.mkdir()
    for _ in range(3):
        code = fetch.main(
            export_args(directory, None, "--sources", "weightgurus"), fake_env
        )
        assert code == 0
    assert len(list(directory.glob("health-*.jsonl"))) == 3


def test_repeated_page_token_fails_instead_of_truncating(server, monkeypatch):
    oura, _ = oura_client(server)
    pages = iter([{"data": [{"id": "a"}], "next_token": "t"}] * 3)
    monkeypatch.setattr(oura, "get", lambda collection, params: next(pages))
    with pytest.raises(fetch.FetchError, match="repeated a page token"):
        list(oura.pages("daily_sleep", {}))
