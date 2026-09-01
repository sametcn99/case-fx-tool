"""Unit tests for the upstream boundary; no real network is used."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app import config
from app.errors import (
    RateUnavailable,
    UpstreamError,
    UpstreamInvalidResponse,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from app.upstream import (
    CURRENT_TTL_SECONDS,
    FrankfurterClient,
)


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.responses: dict[str, httpx.Response] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        response = self.responses.get(request.url.path)
        if response is None:
            return httpx.Response(
                200,
                json={"amount": 1, "base": "EUR", "date": "2026-08-28", "rates": {"TRY": 55.5}},
                request=request,
            )
        return response


@pytest.fixture
def fake():
    return FakeUpstream()


@pytest.fixture
def client(fake):
    return FrankfurterClient(transport=httpx.MockTransport(fake))


@pytest.mark.asyncio
async def test_rate_keeps_decimal_and_upstream_date(client, fake, monkeypatch):
    # ``./test.sh`` is intentionally run with a closed FX_UPSTREAM_BASE by the
    # evaluator.  This assertion is about URL construction, so isolate it from
    # that environment-level setting.
    monkeypatch.delenv("FX_UPSTREAM_BASE", raising=False)
    fake.responses["/v1/2026-08-28"] = httpx.Response(
        200,
        content=b'{"date":"2026-08-28","rates":{"TRY":1.0847}}',
    )

    rate, rate_date = await client.get_rate("eur", "try", date(2026, 8, 28))

    assert rate == Decimal("1.0847")
    assert isinstance(rate, Decimal)
    assert rate_date == date(2026, 8, 28)
    assert fake.calls[0].url == httpx.URL(
        "https://api.frankfurter.dev/v1/2026-08-28?base=EUR&symbols=TRY"
    )
    await client.close()


@pytest.mark.asyncio
async def test_same_question_is_cached(client, fake):
    first = await client.get_rate("EUR", "TRY", "2026-08-28")
    second = await client.get_rate("EUR", "TRY", "2026-08-28")

    assert first == second
    assert len(fake.calls) == 1
    await client.close()


@pytest.mark.asyncio
async def test_date_is_part_of_cache_key(client, fake):
    fake.responses["/v1/2026-08-29"] = httpx.Response(
        200,
        json={"date": "2026-08-29", "rates": {"TRY": 56.5}},
    )

    await client.get_rate("EUR", "TRY", "2026-08-28")
    other = await client.get_rate("EUR", "TRY", "2026-08-29")

    assert other[0] == Decimal("56.5")
    assert len(fake.calls) == 2
    await client.close()


@pytest.mark.asyncio
async def test_errors_are_not_cached(client, fake):
    fake.responses["/v1/2026-08-28"] = httpx.Response(500, content=b"oops")

    with pytest.raises(UpstreamError):
        await client.get_rate("EUR", "TRY", "2026-08-28")
    with pytest.raises(UpstreamError):
        await client.get_rate("EUR", "TRY", "2026-08-28")

    assert len(fake.calls) == 2
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "content", "error"),
    [
        (404, b'{"message":"not found"}', RateUnavailable),
        (200, b"<html>nope</html>", UpstreamInvalidResponse),
        (200, b'{"rates":{"TRY":1.2}}', UpstreamInvalidResponse),
    ],
)
async def test_upstream_failures_are_mapped(client, fake, status, content, error):
    fake.responses["/v1/2026-08-28"] = httpx.Response(status, content=content)

    with pytest.raises(error):
        await client.get_rate("EUR", "TRY", "2026-08-28")
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_rate", ["0", "-1"])
async def test_non_positive_rate_is_invalid(client, fake, raw_rate):
    fake.responses["/v1/2026-08-28"] = httpx.Response(
        200,
        content=(
            f'{{"date":"2026-08-28","rates":{{"TRY":{raw_rate}}}}}'
        ).encode(),
    )

    with pytest.raises(UpstreamInvalidResponse):
        await client.get_rate("EUR", "TRY", "2026-08-28")
    await client.close()


@pytest.mark.asyncio
async def test_future_publication_date_is_invalid_and_not_cached(client, fake):
    fake.responses["/v1/2026-08-28"] = httpx.Response(
        200,
        content=b'{"date":"2026-08-29","rates":{"TRY":1.2}}',
    )

    with pytest.raises(UpstreamInvalidResponse):
        await client.get_rate("EUR", "TRY", "2026-08-28")

    fake.responses["/v1/2026-08-28"] = httpx.Response(
        200,
        content=b'{"date":"2026-08-28","rates":{"TRY":1.2}}',
    )
    assert await client.get_rate("EUR", "TRY", "2026-08-28") == (
        Decimal("1.2"),
        date(2026, 8, 28),
    )
    assert len(fake.calls) == 2
    await client.close()


@pytest.mark.asyncio
async def test_connect_failure_is_unavailable():
    async def fail(request):
        raise httpx.ConnectError("offline", request=request)

    client = FrankfurterClient(transport=httpx.MockTransport(fail))
    with pytest.raises(UpstreamUnavailable):
        await client.get_rate("EUR", "TRY", "2026-08-28")
    await client.close()


@pytest.mark.asyncio
async def test_closed_local_port_is_unavailable(monkeypatch):
    # This is the same local-only failure mode used by the evaluator.  It does
    # not contact the public internet.
    monkeypatch.setenv("FX_UPSTREAM_BASE", "http://127.0.0.1:1")
    client = FrankfurterClient()
    try:
        with pytest.raises(UpstreamUnavailable):
            await client.get_rate("EUR", "TRY", "2026-08-28")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_timeout_is_timeout():
    async def fail(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = FrankfurterClient(transport=httpx.MockTransport(fail))
    with pytest.raises(UpstreamTimeout):
        await client.get_rate("EUR", "TRY", "2026-08-28")
    await client.close()


@pytest.mark.asyncio
async def test_connect_timeout_is_timeout():
    async def fail(request):
        raise httpx.ConnectTimeout("connect timed out", request=request)

    client = FrankfurterClient(transport=httpx.MockTransport(fail))
    with pytest.raises(UpstreamTimeout):
        await client.get_rate("EUR", "TRY", "2026-08-28")
    await client.close()


@pytest.mark.asyncio
async def test_currency_list_is_cached(client, fake):
    fake.responses["/v1/currencies"] = httpx.Response(
        200,
        json={"eur": "Euro", "TRY": "Turkish lira"},
    )

    assert await client.get_currencies() == frozenset({"EUR", "TRY"})
    assert await client.get_currencies() == frozenset({"EUR", "TRY"})
    assert len(fake.calls) == 1
    await client.close()


@pytest.mark.asyncio
async def test_current_rate_uses_short_ttl(monkeypatch, fake):
    now = [100.0]
    today = config.today_in_ecb_tz().isoformat()
    client = FrankfurterClient(
        transport=httpx.MockTransport(fake),
        clock=lambda: now[0],
    )

    await client.get_rate("EUR", "TRY", today)
    now[0] += CURRENT_TTL_SECONDS + 1
    await client.get_rate("EUR", "TRY", today)

    assert len(fake.calls) == 2
    await client.close()
