"""End-to-end contract coverage with the upstream fully faked."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

CONVERT = "/tools/convert"


def query(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {"amount": "250", "from": "EUR", "to": "TRY"}
    params.update(overrides)
    return {key: value for key, value in params.items() if value is not None}


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == code
    assert isinstance(body["message"], str) and body["message"].strip()


def test_success_contract_has_all_fields_and_no_float_artifact(client, fake_upstream):
    fake_upstream.set_rate(
        "/v1/2026-08-28",
        rate_date="2026-08-28",
        rate="1.0847",
    )

    response = client.get(CONVERT, params=query(date="2026-08-28"))

    assert response.status_code == 200
    assert set(response.json()) == {
        "amount",
        "from",
        "to",
        "rate",
        "result",
        "rate_date",
        "asked_date",
        "source",
    }
    assert response.json()["rate"] == 1.0847
    assert response.json()["result"] == 271.18
    assert "1.084700000000" not in response.text
    assert fake_upstream.paths == ["/v1/currencies", "/v1/2026-08-28"]


def test_explicit_weekend_keeps_asked_and_published_dates(client, fake_upstream):
    fake_upstream.set_rate(
        "/v1/2026-08-29",
        rate_date="2026-08-28",
        rate="1.0847",
    )

    response = client.get(CONVERT, params=query(date="2026-08-29"))

    assert response.status_code == 200
    assert response.json()["asked_date"] == "2026-08-29"
    assert response.json()["rate_date"] == "2026-08-28"
    assert fake_upstream.paths[-1] == "/v1/2026-08-29"


def test_missing_date_uses_latest_and_ecb_today(client, fake_upstream, monkeypatch):
    today = date(2026, 8, 29)
    monkeypatch.setattr(config, "today_in_ecb_tz", lambda: today)
    fake_upstream.set_rate("/v1/latest", rate_date="2026-08-28")

    response = client.get(CONVERT, params=query())

    assert response.status_code == 200
    assert response.json()["asked_date"] == "2026-08-29"
    assert response.json()["rate_date"] == "2026-08-28"
    assert fake_upstream.paths[-1] == "/v1/latest"


def test_eight_day_old_rate_is_rejected(client, fake_upstream):
    fake_upstream.set_rate("/v1/2026-08-29", rate_date="2026-08-21")

    assert_error(
        client.get(CONVERT, params=query(date="2026-08-29")),
        404,
        "rate_too_stale",
    )


def test_stale_rate_is_not_cached_when_upstream_recovers(client, fake_upstream):
    """A stale response must not block a fresh response for the same key."""

    params = query(date="2026-08-29")
    fake_upstream.set_rate("/v1/2026-08-29", rate_date="2026-08-21")

    first = client.get(CONVERT, params=params)
    assert_error(first, 404, "rate_too_stale")

    fake_upstream.set_rate(
        "/v1/2026-08-29",
        rate_date="2026-08-28",
        rate="1.09",
    )
    second = client.get(CONVERT, params=params)

    assert second.status_code == 200
    assert second.json()["rate_date"] == "2026-08-28"
    assert second.json()["rate"] == 1.09
    assert fake_upstream.paths == [
        "/v1/currencies",
        "/v1/2026-08-29",
        "/v1/2026-08-29",
    ]


@pytest.mark.parametrize("rate", ["0", "-1"])
def test_non_positive_upstream_rate_is_rejected(client, fake_upstream, rate):
    fake_upstream.set_rate(
        "/v1/2026-08-28",
        rate_date="2026-08-28",
        rate=rate,
    )

    assert_error(
        client.get(CONVERT, params=query(date="2026-08-28")),
        502,
        "upstream_invalid_response",
    )


def test_future_upstream_publication_date_is_rejected(client, fake_upstream):
    fake_upstream.set_rate(
        "/v1/2026-08-28",
        rate_date="2026-08-29",
    )

    assert_error(
        client.get(CONVERT, params=query(date="2026-08-28")),
        502,
        "upstream_invalid_response",
    )


def test_wrong_upstream_base_is_rejected(client, fake_upstream):
    fake_upstream.set_content(
        "/v1/2026-08-28",
        b'{"base":"USD","date":"2026-08-28","rates":{"TRY":1.2}}',
    )

    assert_error(
        client.get(CONVERT, params=query(date="2026-08-28")),
        502,
        "upstream_invalid_response",
    )


def test_unrepresentable_upstream_rate_is_rejected_without_cache_poisoning(
    client, fake_upstream
):
    fake_upstream.set_rate(
        "/v1/2026-08-28",
        rate_date="2026-08-28",
        rate="1e100",
    )

    assert_error(
        client.get(CONVERT, params=query(date="2026-08-28")),
        502,
        "upstream_invalid_response",
    )

    fake_upstream.set_rate(
        "/v1/2026-08-28",
        rate_date="2026-08-28",
        rate="1.0847",
    )
    response = client.get(CONVERT, params=query(date="2026-08-28"))

    assert response.status_code == 200
    assert fake_upstream.paths == [
        "/v1/currencies",
        "/v1/2026-08-28",
        "/v1/2026-08-28",
    ]


def test_future_date_is_rejected_before_upstream(client, fake_upstream):
    tomorrow = config.today_in_ecb_tz() + timedelta(days=1)

    assert_error(
        client.get(CONVERT, params=query(date=tomorrow.isoformat())),
        400,
        "date_in_future",
    )
    assert fake_upstream.call_count == 0


def test_series_before_date_is_rejected_before_upstream(client, fake_upstream):
    assert_error(
        client.get(CONVERT, params=query(date="1998-12-31")),
        400,
        "date_before_series_start",
    )
    assert fake_upstream.call_count == 0


def test_unknown_currency_is_rejected_from_cached_catalogue(client, fake_upstream):
    assert_error(
        client.get(CONVERT, params=query(to="XXX")),
        400,
        "unknown_currency",
    )
    assert fake_upstream.paths == ["/v1/currencies"]


def test_same_currency_is_rejected_before_upstream(client, fake_upstream):
    assert_error(
        client.get(CONVERT, params=query(**{"from": "eur", "to": "EUR"})),
        400,
        "same_currency",
    )
    assert fake_upstream.call_count == 0


def test_lowercase_currencies_are_normalised(client, fake_upstream):
    response = client.get(
        CONVERT,
        params=query(**{"from": "eur", "to": "try", "date": "2026-08-28"}),
    )

    assert response.status_code == 200
    assert response.json()["from"] == "EUR"
    assert response.json()["to"] == "TRY"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        (query(amount="0"), "invalid_amount"),
        (query(amount="-1"), "invalid_amount"),
        (query(amount="abc"), "invalid_amount"),
        (query(amount="1e308"), "invalid_amount"),
        (query(**{"from": "EU"}), "invalid_currency_code"),
        (query(**{"from": "E1R"}), "invalid_currency_code"),
        (query(date="not-a-date"), "invalid_date"),
    ],
)
def test_invalid_input_uses_single_error_contract(client, params, code):
    assert_error(client.get(CONVERT, params=params), 400, code)


def test_large_amount_and_decimal_rate_are_calculated_without_float_rounding(
    client, fake_upstream
):
    fake_upstream.set_rate(
        "/v1/2026-08-28",
        rate_date="2026-08-28",
        rate="1.0847",
    )

    response = client.get(
        CONVERT,
        params=query(amount="1000000.1234567891", date="2026-08-28"),
    )

    assert response.status_code == 200
    assert response.json()["amount"] == 1000000.1234567891
    assert "1000000.1234567891" in response.text
    assert '"result":1084700.13' in response.text
    assert "1084700.133" not in response.text


def test_identical_requests_hit_fake_upstream_once(client, fake_upstream):
    params = query(date="2026-08-28")

    first = client.get(CONVERT, params=params)
    second = client.get(CONVERT, params=params)

    assert first.status_code == second.status_code == 200
    assert fake_upstream.paths == ["/v1/currencies", "/v1/2026-08-28"]


def test_different_dates_are_separate_cache_entries(client, fake_upstream):
    fake_upstream.set_rate("/v1/2026-08-27", rate_date="2026-08-27", rate="1.08")
    fake_upstream.set_rate("/v1/2026-08-28", rate_date="2026-08-28", rate="1.09")

    first = client.get(CONVERT, params=query(date="2026-08-27"))
    second = client.get(CONVERT, params=query(date="2026-08-28"))

    assert first.status_code == second.status_code == 200
    assert first.json()["rate"] != second.json()["rate"]
    assert fake_upstream.paths == [
        "/v1/currencies",
        "/v1/2026-08-27",
        "/v1/2026-08-28",
    ]


def test_error_response_is_not_cached(client, fake_upstream):
    fake_upstream.set_content("/v1/2026-08-28", b"upstream failed", status=500)
    params = query(date="2026-08-28")

    first = client.get(CONVERT, params=params)
    second = client.get(CONVERT, params=params)

    assert_error(first, 502, "upstream_error")
    assert_error(second, 502, "upstream_error")
    assert fake_upstream.paths == [
        "/v1/currencies",
        "/v1/2026-08-28",
        "/v1/2026-08-28",
    ]


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"<html>not json</html>", "upstream_invalid_response"),
        (b'{"rates":{"TRY":1.0847}}', "upstream_invalid_response"),
    ],
)
def test_invalid_upstream_bodies_never_become_success(client, fake_upstream, content, code):
    fake_upstream.set_content("/v1/2026-08-28", content)

    assert_error(
        client.get(CONVERT, params=query(date="2026-08-28")),
        502,
        code,
    )


def test_upstream_rate_404_is_rate_unavailable(client, fake_upstream):
    fake_upstream.set_json("/v1/2026-08-28", {"message": "not found"}, status=404)

    assert_error(
        client.get(CONVERT, params=query(date="2026-08-28")),
        404,
        "rate_unavailable",
    )


def test_timeout_is_reported_as_retryable_upstream_error(client, fake_upstream):
    fake_upstream.set_exception(
        "/v1/2026-08-28",
        httpx.ReadTimeout("slow", request=httpx.Request("GET", "http://fake.test")),
    )

    assert_error(
        client.get(CONVERT, params=query(date="2026-08-28")),
        504,
        "upstream_timeout",
    )


def test_closed_port_is_unavailable_without_public_network(monkeypatch):
    monkeypatch.setenv("FX_UPSTREAM_BASE", "http://127.0.0.1:1")

    with TestClient(app) as client:
        response = client.get(CONVERT, params=query(date="2026-08-28"))

    assert_error(response, 502, "upstream_unavailable")


def test_invalid_upstream_url_is_unavailable_without_internal_error(monkeypatch):
    monkeypatch.setenv("FX_UPSTREAM_BASE", "not-a-url")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(CONVERT, params=query(date="2026-08-28"))

    assert_error(response, 502, "upstream_unavailable")


def test_unexpected_upstream_failure_never_returns_zero(client, fake_upstream):
    fake_upstream.set_rate("/v1/2026-08-28", rate_date="2026-08-28", rate="1.0847")

    response = client.get(CONVERT, params=query(date="2026-08-28"))

    assert response.status_code == 200
    assert response.json()["result"] != 0.0
    assert '"result":0.0' not in response.text
