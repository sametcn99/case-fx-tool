"""Coverage for the edge-case and abuse-resistance hardening.

Each test here corresponds to a finding from the security review. They are
grouped together rather than scattered so that the reason each guard exists
stays legible: every one of them fails if its guard is removed.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, upstream as upstream_module
from app.errors import UpstreamInvalidResponse, UpstreamTimeout, UpstreamUnavailable
from app.main import app
from app.ratelimit import RateLimiter
from app.upstream import CURRENT_TTL_SECONDS, FrankfurterClient

CONVERT = "/tools/convert"


def query(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {"amount": "250", "from": "EUR", "to": "TRY"}
    params.update(overrides)
    return {key: value for key, value in params.items() if value is not None}


# --- 1. the amount echo is not an amplifier ----------------------------------


@pytest.mark.parametrize(
    "amount",
    ["1E-1000", "1E-100000", "0.00000000001", "1.00000000000"],
)
def test_amount_with_too_many_decimals_is_rejected(client, amount):
    response = client.get(CONVERT, params=query(amount=amount, date="2026-08-28"))

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_amount"
    # The point of the guard: a tiny query must not render a giant body.
    assert len(response.content) < 500


def test_documented_ten_decimal_places_still_works(client, fake_upstream):
    fake_upstream.set_rate("/v1/2026-08-28", rate_date="2026-08-28", rate="1.0847")

    response = client.get(
        CONVERT, params=query(amount="1000000.1234567891", date="2026-08-28")
    )

    assert response.status_code == 200
    assert "1000000.1234567891" in response.text


# --- 2. the upstream body is bounded, and so is the whole exchange -----------


@pytest.mark.asyncio
async def test_oversized_upstream_body_is_refused():
    async def huge(request: httpx.Request) -> httpx.Response:
        padding = "x" * (upstream_module.MAX_RESPONSE_BYTES + 1)
        return httpx.Response(
            200,
            content=(
                '{"base":"EUR","date":"2026-08-28","padding":"'
                + padding
                + '","rates":{"TRY":1.0847}}'
            ).encode(),
            request=request,
        )

    client = FrankfurterClient(transport=httpx.MockTransport(huge))
    try:
        with pytest.raises(UpstreamInvalidResponse):
            await client.get_rate("EUR", "TRY", "2026-08-28")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_slow_upstream_hits_the_total_deadline(monkeypatch):
    """A body that never stops arriving must not hold the request forever.

    httpx's read timeout is per socket read, so a drip-feeding upstream never
    trips it. Only the whole-exchange deadline can end this.
    """

    monkeypatch.setattr(upstream_module, "UPSTREAM_TOTAL_TIMEOUT_SECONDS", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={}, request=request)

    client = FrankfurterClient(transport=httpx.MockTransport(slow))
    try:
        with pytest.raises(UpstreamTimeout):
            await client.get_rate("EUR", "TRY", "2026-08-28")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_outbound_calls_are_bounded_by_a_semaphore():
    live = 0
    peak = 0

    async def counted(request: httpx.Request) -> httpx.Response:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        day = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={"base": "EUR", "date": day, "rates": {"TRY": 1.0847}},
            request=request,
        )

    client = FrankfurterClient(transport=httpx.MockTransport(counted))
    try:
        await asyncio.gather(
            *(
                client.get_rate("EUR", "TRY", f"2026-08-{day:02d}")
                for day in range(1, 29)
            )
        )
    finally:
        await client.close()

    assert peak <= upstream_module.MAX_CONCURRENT_UPSTREAM_REQUESTS


# --- 3. a broken upstream is never reported as our own bug -------------------


# The bodies are built inside the test rather than parametrised directly: a
# 200 KB parameter becomes a 200 KB test id.
_UNPARSEABLE_BODIES = {
    "deeply-nested": b"[" * 100_000 + b"]" * 100_000,
    "integer-literal-past-cpython-digit-limit": (
        b'{"base":"EUR","date":"2026-08-28","rates":{"TRY":' + b"9" * 10_000 + b"}}"
    ),
}


@pytest.mark.parametrize("kind", sorted(_UNPARSEABLE_BODIES))
def test_unparseable_upstream_body_is_a_502_not_an_internal_error(
    client, fake_upstream, kind
):
    fake_upstream.set_content("/v1/2026-08-28", _UNPARSEABLE_BODIES[kind])

    response = client.get(CONVERT, params=query(date="2026-08-28"))

    assert response.status_code == 502
    assert response.json()["error"] == "upstream_invalid_response"


# --- 4. one reading of the clock per request ---------------------------------


def test_today_is_read_exactly_once_per_request(client, fake_upstream, monkeypatch):
    """Two readings can straddle Berlin midnight and disagree with each other.

    When they do, the publication-date bound in the upstream parser and the
    staleness check in the conversion layer are working from different days,
    and a response that satisfies one can be rejected by the other.
    """

    readings: list[date] = []

    def counting_today() -> date:
        readings.append(date(2026, 8, 29))
        return readings[-1]

    monkeypatch.setattr(config, "today_in_ecb_tz", counting_today)
    fake_upstream.set_rate("/v1/latest", rate_date="2026-08-28")

    response = client.get(CONVERT, params=query())

    assert response.status_code == 200
    assert len(readings) == 1


def test_todays_explicit_date_keeps_the_short_ttl_across_a_rollover(monkeypatch):
    """The TTL must be chosen against the same day the request was resolved on.

    Sampling the clock again inside ``_ttl_for`` after midnight makes today's
    provisional rate look historical and freezes it in the cache for 24 hours.
    """

    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"base": "EUR", "date": "2026-08-28", "rates": {"TRY": 1.0847}},
            request=request,
        )

    days = iter([date(2026, 8, 28), date(2026, 8, 29), date(2026, 8, 29)])
    monkeypatch.setattr(config, "today_in_ecb_tz", lambda: next(days))

    now = [100.0]
    client = FrankfurterClient(
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    try:
        asyncio.run(client.get_rate("EUR", "TRY", "2026-08-28"))
        now[0] += CURRENT_TTL_SECONDS + 1
        asyncio.run(client.get_rate("EUR", "TRY", "2026-08-28"))
    finally:
        asyncio.run(client.close())

    # A 24-hour historical TTL would have served the second call from cache.
    assert len(calls) == 2


# --- 5. a dead currency catalogue fails open fast ----------------------------


def test_failed_currency_catalogue_is_not_retried_on_every_request(
    client, fake_upstream
):
    fake_upstream.set_exception(
        "/v1/currencies",
        httpx.ConnectError("offline", request=httpx.Request("GET", "http://fake.test")),
    )
    fake_upstream.set_rate("/v1/2026-08-28", rate_date="2026-08-28", rate="1.0847")

    for _ in range(4):
        assert (
            client.get(CONVERT, params=query(date="2026-08-28")).status_code == 200
        )

    catalogue_attempts = fake_upstream.paths.count("/v1/currencies")
    assert catalogue_attempts == 1


@pytest.mark.asyncio
async def test_currency_catalogue_recovers_after_the_negative_ttl():
    calls: list[httpx.Request] = []
    fail = True

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if fail:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"EUR": "Euro", "TRY": "Lira"}, request=request)

    now = [100.0]
    client = FrankfurterClient(
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    try:
        with pytest.raises(UpstreamUnavailable):
            await client.get_currencies()
        with pytest.raises(UpstreamUnavailable):
            await client.get_currencies()
        assert len(calls) == 1  # the second was served by the negative cache

        fail = False
        now[0] += upstream_module.CURRENCY_FAILURE_TTL_SECONDS + 1
        assert await client.get_currencies() == frozenset({"EUR", "TRY"})
        assert len(calls) == 2
    finally:
        await client.close()


# --- 6. concurrent misses collapse into one upstream call --------------------


class _SuspendingTransport(httpx.AsyncBaseTransport):
    """A transport that actually yields, as a real socket would.

    ``httpx.MockTransport`` with a synchronous handler can complete without ever
    suspending, which hides exactly the interleaving this test is about.
    """

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        await asyncio.sleep(0.02)
        if request.url.path.endswith("/currencies"):
            return httpx.Response(
                200, json={"EUR": "Euro", "TRY": "Lira"}, request=request
            )
        return httpx.Response(
            200,
            json={"base": "EUR", "date": "2026-08-28", "rates": {"TRY": 1.0847}},
            request=request,
        )


@pytest.mark.asyncio
async def test_concurrent_identical_rate_requests_make_one_upstream_call():
    transport = _SuspendingTransport()
    client = FrankfurterClient(transport=transport)
    try:
        results = await asyncio.gather(
            *(client.get_rate("EUR", "TRY", "2026-08-28") for _ in range(20))
        )
    finally:
        await client.close()

    assert len(transport.calls) == 1
    assert len(set(results)) == 1


@pytest.mark.asyncio
async def test_concurrent_currency_lookups_make_one_upstream_call():
    transport = _SuspendingTransport()
    client = FrankfurterClient(transport=transport)
    try:
        results = await asyncio.gather(*(client.get_currencies() for _ in range(20)))
    finally:
        await client.close()

    assert len(transport.calls) == 1
    assert all(result == frozenset({"EUR", "TRY"}) for result in results)


@pytest.mark.asyncio
async def test_a_shared_failure_is_not_cached_for_later_callers():
    transport = _SuspendingTransport()

    async def failing(request: httpx.Request) -> httpx.Response:
        transport.calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(500, content=b"oops", request=request)

    client = FrankfurterClient(transport=httpx.MockTransport(failing))
    try:
        results = await asyncio.gather(
            *(client.get_rate("EUR", "TRY", "2026-08-28") for _ in range(5)),
            return_exceptions=True,
        )
        assert all(isinstance(result, Exception) for result in results)
        assert len(transport.calls) == 1

        # The next caller must still get a fresh attempt; errors are not cached.
        with pytest.raises(Exception):
            await client.get_rate("EUR", "TRY", "2026-08-28")
        assert len(transport.calls) == 2
    finally:
        await client.close()


# --- 7. one caller cannot walk the cache forever -----------------------------


def test_rate_limiter_refills_over_time():
    now = [0.0]
    limiter = RateLimiter(limit=2, window_seconds=60.0, clock=lambda: now[0])

    assert limiter.allow("a") is True
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True  # a different client has its own bucket

    now[0] += 31.0  # half a window refills one token
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False


def test_rate_limiter_bucket_table_is_bounded():
    limiter = RateLimiter(limit=1)
    for index in range(5000):
        limiter.allow(f"client-{index}")

    assert len(limiter._buckets) <= 4096


def test_disabled_rate_limiter_allows_everything():
    limiter = RateLimiter(limit=0)

    assert limiter.enabled is False
    assert all(limiter.allow("a") for _ in range(1000))


def test_endpoint_returns_429_in_the_documented_error_shape(client, fake_upstream):
    fake_upstream.set_rate("/v1/2026-08-28", rate_date="2026-08-28", rate="1.0847")
    app.state.rate_limiter = RateLimiter(limit=2)

    assert client.get(CONVERT, params=query(date="2026-08-28")).status_code == 200
    assert client.get(CONVERT, params=query(date="2026-08-28")).status_code == 200

    throttled = client.get(CONVERT, params=query(date="2026-08-28"))

    assert throttled.status_code == 429
    body = throttled.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == "rate_limited"
    assert body["message"].strip()


def test_health_is_exempt_from_the_rate_limit(client):
    app.state.rate_limiter = RateLimiter(limit=1)

    for _ in range(10):
        assert client.get("/health").status_code == 200


def test_rate_limit_is_configurable(monkeypatch):
    monkeypatch.delenv("FX_RATE_LIMIT_PER_MINUTE", raising=False)
    assert config.rate_limit_per_minute() == config.DEFAULT_RATE_LIMIT_PER_MINUTE

    monkeypatch.setenv("FX_RATE_LIMIT_PER_MINUTE", "0")
    assert config.rate_limit_per_minute() == 0

    monkeypatch.setenv("FX_RATE_LIMIT_PER_MINUTE", "-5")
    assert config.rate_limit_per_minute() == 0

    monkeypatch.setenv("FX_RATE_LIMIT_PER_MINUTE", "nonsense")
    assert config.rate_limit_per_minute() == config.DEFAULT_RATE_LIMIT_PER_MINUTE


def test_conversion_still_works_under_the_default_limit(client, fake_upstream):
    """The default must not throttle ordinary agent traffic."""

    fake_upstream.set_rate("/v1/2026-08-28", rate_date="2026-08-28", rate="1.0847")
    app.state.rate_limiter = RateLimiter(config.DEFAULT_RATE_LIMIT_PER_MINUTE)

    for _ in range(config.DEFAULT_RATE_LIMIT_PER_MINUTE):
        assert client.get(CONVERT, params=query(date="2026-08-28")).status_code == 200


def test_openapi_still_describes_exactly_one_tool_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) == {"/tools/convert", "/health"}


def test_lifespan_installs_a_limiter():
    with TestClient(app):
        assert isinstance(app.state.rate_limiter, RateLimiter)
