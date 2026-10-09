"""Provider error handling, retries, rate limiting, freshness and honest feed labelling."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from mwbot.data.base import DataUnavailableError
from mwbot.data.http import AuthError, ProviderHTTPError, RateLimiter, backoff_delays, request_json
from mwbot.data.massive import MassiveClient, MassiveProvider, parse_aggregates
from mwbot.domain import FeedKind

from .conftest import TEST_DAY

SECRET = "sk-test-SECRET-123"


def gainers_payload(now_ns):
    return {"status": "OK", "tickers": [
        {"ticker": "ZNTR", "todaysChangePerc": 21.5, "updated": now_ns,
         "prevDay": {"c": 5.0, "v": 900000}, "lastQuote": {"p": 6.0, "P": 6.02}},
        {"ticker": "QBLT", "prevDay": {"c": 12.0}, "updated": now_ns},
    ]}


def agg_payload(start_ms, n=40, px=6.0):
    return {"status": "OK", "results": [
        {"t": start_ms + i * 60_000, "o": px, "h": px + 0.05, "l": px - 0.05, "c": px + 0.01 * (i % 3), "v": 20_000 + i}
        for i in range(n)]}


class Recorder:
    def __init__(self, handler):
        self.handler, self.requests = handler, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return self.handler(req)


def _backoffs(sleeps):
    return [s for s in sleeps if s >= 0.1]  # ignore the rate limiter's micro-waits


def provider_with(handler, feed="delayed", **kw):
    rec = Recorder(handler)
    sleeps: list[float] = []
    p = MassiveProvider(SECRET, feed=feed, delay_seconds=900, calls_per_second=1000,
                        transport=httpx.MockTransport(rec), sleep=sleeps.append, **kw)
    return p, rec, sleeps


def test_no_api_key_is_a_clear_error():
    with pytest.raises(DataUnavailableError, match="API_KEY"):
        MassiveProvider("")


def test_api_key_sent_in_header_never_in_url():
    p, rec, _ = provider_with(lambda r: httpx.Response(200, json={"tickers": []}))
    p.client.gainers()
    req = rec.requests[0]
    assert SECRET not in str(req.url) and req.headers["authorization"] == f"Bearer {SECRET}"


def test_feed_labels_are_honest():
    delayed, _, _ = provider_with(lambda r: httpx.Response(200, json={}), feed="delayed")
    realtime, _, _ = provider_with(lambda r: httpx.Response(200, json={}), feed="realtime")
    d, r = delayed.feed_info(), realtime.feed_info()
    assert d.kind == FeedKind.DELAYED and "DELAYED" in d.label and d.delay_seconds == 900 and not d.to_dict()["is_realtime"]
    assert r.kind == FeedKind.REALTIME and r.to_dict()["is_realtime"]


def test_parse_aggregates_handles_empty_and_bad_rows():
    assert parse_aggregates({"status": "OK"}).empty
    df = parse_aggregates({"results": [
        {"t": 1_700_000_000_000, "o": 1, "h": 1.1, "l": 0.9, "c": 1.0, "v": 10},
        {"t": 1_700_000_060_000, "o": 1, "h": 0.5, "l": 0.9, "c": 1.0, "v": 10},   # high < close -> malformed
    ]})
    assert len(df) == 1 and str(df.index.tz) == "UTC"


def test_retry_on_429_with_bounded_backoff_then_success():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "2"}) if calls["n"] < 3 else httpx.Response(200, json={"tickers": []})

    p, _, sleeps = provider_with(handler)
    assert p.client.gainers() == []
    assert calls["n"] == 3 and _backoffs(sleeps) == [2.0, 2.0]  # honours Retry-After, bounded


def test_gives_up_after_max_retries():
    p, rec, sleeps = provider_with(lambda r: httpx.Response(503))
    with pytest.raises(ProviderHTTPError) as e:
        p.client.gainers()
    assert e.value.retriable and len(rec.requests) == 4 and len(_backoffs(sleeps)) == 3  # 1 try + 3 retries


def test_auth_error_is_not_retried_and_degrades_gracefully():
    from mwbot.calendar import session_for
    from mwbot.data.massive import MassiveLiveSource

    p, rec, _ = provider_with(lambda r: httpx.Response(403, json={"status": "NOT_AUTHORIZED"}))
    src = MassiveLiveSource(p.client, session_for(TEST_DAY), p.feed_info())
    with pytest.raises(DataUnavailableError, match="plan"):
        src.contexts_at(datetime.now(UTC))
    assert len(rec.requests) == 1


def test_network_errors_are_retried():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"tickers": []})

    p, _, sleeps = provider_with(handler)
    assert p.client.gainers() == [] and calls["n"] == 2 and len(_backoffs(sleeps)) == 1


def test_live_source_builds_contexts_and_labels_missing_data(monkeypatch):
    """Snapshot -> contexts. Unknown fields stay None (never invented); delayed bars exclude the last 15 min."""
    from mwbot.calendar import session_for
    from mwbot.data import massive as m

    now = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
    sess = session_for(TEST_DAY)
    start_ms = int((now - timedelta(minutes=60)).timestamp() * 1000)
    now_ns = int((now - timedelta(minutes=15)).timestamp() * 1e9)

    def handler(req):
        path = req.url.path
        if "gainers" in path:
            return httpx.Response(200, json=gainers_payload(now_ns))
        if "/range/1/minute/" in path:
            return httpx.Response(200, json=agg_payload(start_ms))
        if "/range/1/day/" in path:
            return httpx.Response(403, json={"status": "NOT_AUTHORIZED"})
        if "/v3/reference/tickers/" in path:
            return httpx.Response(404, json={})
        return httpx.Response(404)

    p, _, _ = provider_with(handler, feed="delayed")
    src = m.MassiveLiveSource(p.client, sess, p.feed_info())
    ctxs = {c.symbol: c for c in src.contexts_at(now)}
    z = ctxs["ZNTR"]
    assert z.meta.prev_close == 5.0
    assert z.meta.avg_daily_volume is None and z.meta.market_cap is None      # plan lacks daily aggs / reference -> unavailable
    assert z.quote is not None and z.quote.bid == 6.0
    assert ctxs["QBLT"].quote is None                                          # no quote in payload -> None
    horizon = now - timedelta(minutes=15) - timedelta(minutes=1)
    assert (z.bars.index <= horizon).all()                                     # delayed feed: nothing newer than now-15m
    assert src.feed_age_seconds(now) == 0.0                                    # snapshot age == configured delay
    assert src.feed_age_seconds(now + timedelta(minutes=10)) == pytest.approx(600)  # stale beyond expected delay


def test_http_helpers():
    assert backoff_delays(5, base=1, cap=8, jitter=False) == [1, 2, 4, 8, 8]
    clock = {"t": 0.0}
    slept = []
    rl = RateLimiter(2.0, clock=lambda: clock["t"], sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)))
    rl.wait(); rl.wait(); rl.wait()
    assert slept == [0.5, 0.5]


def test_invalid_json_and_4xx_are_distinct_errors():
    c = httpx.Client(base_url="http://x", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>")))
    with pytest.raises(ProviderHTTPError, match="invalid JSON"):
        request_json(c, "GET", "/a", sleep=lambda s: None)
    c2 = httpx.Client(base_url="http://x", transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(ProviderHTTPError) as e:
        request_json(c2, "GET", "/a", sleep=lambda s: None)
    assert e.value.status == 404 and not e.value.retriable
    c3 = httpx.Client(base_url="http://x", transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(AuthError):
        request_json(c3, "GET", "/a", sleep=lambda s: None)


def test_client_close_and_historical_download(tmp_path):
    from datetime import date

    from mwbot.data.massive import download_dataset

    start_ms = int(datetime(2026, 9, 14, 13, 30, tzinfo=UTC).timestamp() * 1000)
    p, rec, _ = provider_with(lambda r: httpx.Response(200, json=agg_payload(start_ms)))
    out = download_dataset(p.client, ["ZNTR"], date(2026, 9, 14), date(2026, 9, 14), tmp_path / "ds")
    import json
    man = json.loads((out / "manifest.json").read_text())
    assert man["synthetic"] is False and man["adjusted"] is True and man["timezone"] == "UTC"
    assert "adjusted=true" in str(rec.requests[0].url)
    p.client.close()
