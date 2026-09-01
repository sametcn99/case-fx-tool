"""Conversion rules and endpoint integration tests without real network access."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, conversion
from app.errors import RateTooStale
from app.main import app
from app.upstream import FrankfurterClient


def test_missing_date_uses_ecb_today(monkeypatch):
    today = date(2026, 8, 29)
    monkeypatch.setattr(config, "today_in_ecb_tz", lambda: today)
    assert conversion.resolve_asked_date(None) == today


def test_explicit_date_selects_dated_path():
    asked = date(2026, 8, 29)
    assert conversion.resolve_asked_date("2026-08-29") == asked
    assert conversion.upstream_path_for(asked, True) == "2026-08-29"
    assert conversion.upstream_path_for(asked, False) == "latest"


def test_staleness_is_strictly_greater_than_seven_days():
    asked = date(2026, 8, 29)
    conversion.check_staleness(asked, date(2026, 8, 22))
    with pytest.raises(RateTooStale):
        conversion.check_staleness(asked, date(2026, 8, 21))


def test_result_rounds_half_up_only_after_multiplication():
    assert conversion.compute_result(Decimal("1000000"), Decimal("1.0847")) == Decimal(
        "1084700.00"
    )
    assert conversion.compute_result(
        Decimal("250.1234567891"), Decimal("1.0847")
    ) == Decimal("271.31")


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.rate_date = "2026-08-28"
        self.rate = "1.0847"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path.endswith("/currencies"):
            return httpx.Response(
                200,
                json={"EUR": "Euro", "TRY": "Turkish lira"},
                request=request,
            )
        return httpx.Response(
            200,
            content=(
                f'{{"date":"{self.rate_date}","rates":{{"TRY":{self.rate}}}}}'
            ).encode(),
            request=request,
        )


@pytest.fixture
def fake_client():
    fake = FakeUpstream()
    upstream = FrankfurterClient(transport=httpx.MockTransport(fake))
    with TestClient(app) as client:
        # Replace the lifespan-created production client after startup; the
        # endpoint still uses the same application-owned state slot.
        app.state.upstream = upstream
        yield client, fake
    # The injected client is intentionally not owned by the app lifespan.
    import asyncio

    asyncio.run(upstream.close())


def test_exact_date_response_keeps_rate_date_and_decimal_text(fake_client):
    client, fake = fake_client
    fake.rate_date = "2026-08-29"
    response = client.get(
        "/tools/convert",
        params={"amount": "1000000", "from": "eur", "to": "try", "date": "2026-08-29"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "amount": 1000000,
        "from": "EUR",
        "to": "TRY",
        "rate": 1.0847,
        "result": 1084700.0,
        "rate_date": "2026-08-29",
        "asked_date": "2026-08-29",
        "source": "ECB via frankfurter.dev",
    }
    assert "1084700.00" in response.text
    assert [request.url.path for request in fake.calls] == [
        "/v1/currencies",
        "/v1/2026-08-29",
    ]


def test_explicit_weekend_date_reports_published_rate_date(fake_client):
    client, fake = fake_client

    response = client.get(
        "/tools/convert",
        params={
            "amount": "250",
            "from": "EUR",
            "to": "TRY",
            "date": "2026-08-29",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["asked_date"] == "2026-08-29"
    assert body["rate_date"] == "2026-08-28"
    assert body["rate"] == 1.0847
    assert fake.calls[-1].url.path == "/v1/2026-08-29"


def test_missing_date_uses_latest_and_reports_ecb_today(fake_client, monkeypatch):
    client, fake = fake_client
    today = date(2026, 8, 29)
    monkeypatch.setattr(config, "today_in_ecb_tz", lambda: today)

    response = client.get(
        "/tools/convert",
        params={"amount": "250", "from": "EUR", "to": "TRY"},
    )

    assert response.status_code == 200
    assert response.json()["asked_date"] == "2026-08-29"
    assert response.json()["rate_date"] == "2026-08-28"
    assert fake.calls[-1].url.path == "/v1/latest"


def test_rate_older_than_seven_days_is_rejected(fake_client):
    client, fake = fake_client
    fake.rate_date = "2026-08-21"

    response = client.get(
        "/tools/convert",
        params={
            "amount": "250",
            "from": "EUR",
            "to": "TRY",
            "date": "2026-08-29",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"] == "rate_too_stale"
