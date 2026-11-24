import base64
import copy
import io
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint, Record
from throughline_sdk.contract import check_plugin

from throughline_simplefin import SimpleFIN, split_access_url
from throughline_simplefin import claim as claiming
from throughline_simplefin.recorded import bridge

RECORDED = Path(__file__).parent / "recorded" / "bridge.json"
CANARY = "https://invented-canary-user:invented-canary-pass@bridge.example.com/simplefin"
NOW = datetime(2025, 6, 2, tzinfo=timezone.utc)


@pytest.fixture
def env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SIMPLEFIN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL", CANARY)
    monkeypatch.setenv("SIMPLEFIN_START_DATE", "2025-01-01")
    monkeypatch.setenv("SIMPLEFIN_WINDOW_DAYS", "30")
    return monkeypatch


def data():
    return json.loads(RECORDED.read_text())


def plugin(transport):
    p = SimpleFIN(transport=transport)
    p.now = NOW
    p.sleep = lambda s: None
    return p


def messages(items):
    return [i for i in items if isinstance(i, dict)]


def test_split_access_url_keeps_credentials_out_of_the_base():
    base, auth = split_access_url("https://us%40er:p%3Ass@bridge.example.com/simplefin/")
    assert base == "https://bridge.example.com/simplefin"
    assert auth == ("us@er", "p:ss")
    with pytest.raises(ValueError):
        split_access_url("https://bridge.example.com/simplefin")


def test_requests_use_basic_auth_windows_and_pending(env):
    seen = []
    inner = bridge(data())

    def spy(request):
        seen.append(request)
        return inner.handle_request(request)

    items = list(plugin(httpx.MockTransport(spy)).records({}, "backfill"))
    first = seen[0]
    assert first.url.host == "bridge.example.com" and "invented-canary" not in str(first.url)
    expected = "Basic " + base64.b64encode(b"invented-canary-user:invented-canary-pass").decode()
    assert first.headers["authorization"] == expected
    assert first.url.params["pending"] == "1"
    spans = [int(r.url.params["end-date"]) - int(r.url.params["start-date"]) for r in seen]
    assert max(spans) <= 30 * 86400
    checkpoints = [i for i in items if isinstance(i, Checkpoint)]
    assert len(checkpoints) == len(seen) + 1


def test_transactions_and_accounts_are_labelled_and_mapped(env):
    items = messages(plugin(bridge(data())).records({}, "backfill"))
    by_id = {m["external_id"]: m for m in items}
    assert {m["access_label"] for m in items} == {"financial"}
    florist = by_id["ACT-INVENTED-CHK:TRN-CHK-0004"]
    assert florist["channel"] == "simplefin.transactions"
    assert florist["happened_at"] == "2025-02-14T20:10:00+00:00"
    assert florist["fields"]["amount"] == -35.75
    assert florist["fields"]["direction"] == "debit"
    assert florist["fields"]["state"] == "posted"
    assert florist["fields"]["payee"] == "Fictional Florist"
    assert "Invented memo" in florist["content"]
    assert florist["thread"]["external_id"] == "account:ACT-INVENTED-CHK"
    assert "balance" not in json.dumps(florist["payload"])
    pending = by_id["ACT-INVENTED-CHK:TRN-CHK-PEND"]
    assert pending["fields"]["state"] == "pending" and pending["fields"]["posted_at"] is None
    card = by_id["ACT-INVENTED-CARD"]
    assert card["channel"] == "simplefin.accounts"
    assert card["fields"]["balance"] == -412.73
    assert card["fields"]["available_balance"] == 4587.27
    broker = by_id["ACT-INVENTED-BRK"]
    assert broker["fields"]["holdings"][0]["symbol"] == "FAKEX"
    assert broker["fields"]["available_balance"] is None
    # Each transaction once, though windows overlap nothing.
    assert len([m for m in items if m["channel"] == "simplefin.transactions"]) == 17


def test_access_label_and_timezone_settings(env):
    env.setenv("SIMPLEFIN_ACCESS_LABEL", "")
    env.setenv("SIMPLEFIN_TIMEZONE", "America/Winnipeg")
    items = messages(plugin(bridge(data())).records({}, "backfill"))
    assert {m["access_label"] for m in items} == {None}
    florist = next(m for m in items if m["external_id"].endswith("TRN-CHK-0004"))
    assert florist["happened_at"] == "2025-02-14T14:10:00-06:00"


def test_pending_posts_as_new_version_and_vanished_pending_is_dropped(env):
    source = data()
    first = list(plugin(bridge(source)).records({}, "backfill"))
    cursor = [i for i in first if isinstance(i, Checkpoint)][-1].cursor
    assert set(cursor["pending"]) == {
        "ACT-INVENTED-CHK:TRN-CHK-PEND",
        "ACT-INVENTED-CARD:TRN-CARD-PEND",
    }
    later = copy.deepcopy(source)
    chk = later["accounts"][0]
    coffee = next(t for t in chk["transactions"] if t["id"] == "TRN-CHK-PEND")
    coffee.update(posted=coffee["transacted_at"] + 86400, pending=False)
    card = later["accounts"][1]
    card["transactions"] = [t for t in card["transactions"] if t["id"] != "TRN-CARD-PEND"]
    chk["balance"] = "1827.94"
    second = list(plugin(bridge(later)).records(cursor, "sync"))
    out = {m["external_id"]: m for m in messages(second)}
    assert out["ACT-INVENTED-CHK:TRN-CHK-PEND"]["fields"]["state"] == "posted"
    dropped = out["ACT-INVENTED-CARD:TRN-CARD-PEND"]
    assert dropped["fields"]["state"] == "dropped" and dropped["payload"]["dropped"] is True
    assert out["ACT-INVENTED-CHK"]["fields"]["balance"] == 1827.94
    assert [i for i in second if isinstance(i, Checkpoint)][-1].cursor["pending"] == {}
    # The sync re-read only the overlap, not the whole history.
    assert "ACT-INVENTED-CHK:TRN-CHK-0001" not in out


def test_bridge_errors_become_partial_coverage(env):
    source = data()
    source["errors"] = ["Connection to Invented Credit Union may need attention"]
    items = list(plugin(bridge(source)).records({}, "backfill"))
    reports = [i.data for i in items if isinstance(i, Record) and i.kind == "coverage"]
    assert {r["channel"] for r in reports} == {"simplefin.accounts", "simplefin.transactions"}
    assert all(r["status"] == "partial" and r["reasons"] == ["source_error"] for r in reports)


@pytest.mark.parametrize("status", [403, 402, 500])
def test_unreachable_bridge_raises_without_leaking(env, status):
    transport = httpx.MockTransport(lambda r: httpx.Response(status))
    with pytest.raises(RuntimeError) as error:
        list(plugin(transport).records({}, "backfill"))
    assert "invented-canary" not in str(error.value)


def test_access_url_file(env, tmp_path):
    env.delenv("SIMPLEFIN_ACCESS_URL")
    path = tmp_path / "access"
    path.write_text(CANARY + "\n")
    env.setenv("SIMPLEFIN_ACCESS_URL_FILE", str(path))
    p = SimpleFIN(transport=bridge(data()))
    assert p.auth == ("invented-canary-user", "invented-canary-pass")
    assert p.secrets["access_url"] == CANARY


def test_missing_access_url_is_an_error(env):
    env.delenv("SIMPLEFIN_ACCESS_URL")
    with pytest.raises(ValueError):
        SimpleFIN()


def test_claim_exchanges_setup_token_and_writes_private_file(tmp_path, capsys):
    claim_url = "https://bridge.example.com/simplefin/claim/INVENTEDTOKEN"
    token = base64.b64encode(claim_url.encode()).decode()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=CANARY + "\n")

    target = tmp_path / "dir" / "access-url"
    code = claiming.main(
        ["--output", str(target)],
        transport=httpx.MockTransport(handler),
        stdin=io.StringIO(token + "\n"),
    )
    assert code == 0
    assert calls[0].method == "POST" and str(calls[0].url) == claim_url
    assert target.read_text().strip() == CANARY
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    out = capsys.readouterr()
    assert "invented-canary" not in out.out + out.err


def test_claim_refused_and_bad_tokens():
    with pytest.raises(ValueError, match="already claimed"):
        claiming.claim(
            base64.b64encode(b"https://bridge.example.com/claim/x").decode(),
            transport=httpx.MockTransport(lambda r: httpx.Response(403)),
        )
    with pytest.raises(ValueError):
        claiming.claim_url(base64.b64encode(b"not a url").decode())


def test_contract_kit():
    result = check_plugin(
        "throughline_simplefin:SimpleFIN",
        {
            "SIMPLEFIN_FIXTURE": str(RECORDED),
            "SIMPLEFIN_ACCESS_URL": CANARY,
            "SIMPLEFIN_START_DATE": "2025-01-01",
            "SIMPLEFIN_WINDOW_DAYS": "30",
        },
        secrets=[CANARY, "invented-canary-pass"],
    )
    assert result["messages"] == 20
