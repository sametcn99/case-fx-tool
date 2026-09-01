"""Everything the service refuses before it would ask the upstream anything.

The HTTP tests here assert rejections, which is what T2 actually delivers. The
accepted cases are checked against the validators directly, so these tests do
not depend on the endpoint's not-yet-written tail.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import config, validation
from app.errors import DateBeforeSeriesStart, DateInFuture, FxError, InvalidDate
from app.main import app

CONVERT = "/tools/convert"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def query(**overrides) -> dict:
    params = {"amount": "250", "from": "EUR", "to": "TRY"}
    params.update(overrides)
    return {key: value for key, value in params.items() if value is not None}


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    body = response.json()
    # The shape is part of the contract: exactly these two fields, both strings.
    assert set(body) == {"error", "message"}
    assert body["error"] == code
    assert isinstance(body["message"], str) and body["message"].strip()


# --- amount ------------------------------------------------------------------


@pytest.mark.parametrize("amount", ["0", "-5", "abc", "", "NaN", "Infinity", "1e308"])
def test_unusable_amounts_are_rejected(client, amount):
    assert_error(client.get(CONVERT, params=query(amount=amount)), 400, "invalid_amount")


def test_missing_amount_is_rejected(client):
    params = query()
    del params["amount"]
    assert_error(client.get(CONVERT, params=params), 400, "invalid_amount")


def test_a_long_fraction_is_accepted_and_kept_exactly():
    # Ten decimal places is a legitimate question, not a malformed one, and the
    # precision has to survive: it is carried as a Decimal, not a float.
    amount = Decimal("250.1234567891")
    assert validation.validate_amount(amount) == amount


# --- currencies ---------------------------------------------------------------


@pytest.mark.parametrize("code", ["EU", "EURO", "E1R", "", "12"])
def test_malformed_currency_codes_are_rejected(client, code):
    assert_error(
        client.get(CONVERT, params=query(**{"from": code})), 400, "invalid_currency_code"
    )
    assert_error(
        client.get(CONVERT, params=query(to=code)), 400, "invalid_currency_code"
    )


def test_currency_codes_are_normalised_to_upper_case():
    assert validation.validate_currency("eur", "from") == "EUR"
    assert validation.validate_currency("try", "to") == "TRY"


def test_converting_a_currency_into_itself_is_rejected(client):
    assert_error(
        client.get(CONVERT, params=query(**{"from": "EUR", "to": "EUR"})),
        400,
        "same_currency",
    )


def test_same_currency_is_caught_after_normalisation(client):
    assert_error(
        client.get(CONVERT, params=query(**{"from": "eur", "to": "EUR"})),
        400,
        "same_currency",
    )


# --- dates --------------------------------------------------------------------


@pytest.mark.parametrize("value", ["2026-13-01", "28-08-2026", "20260828", "yesterday", ""])
def test_unparseable_dates_are_rejected(client, value):
    assert_error(client.get(CONVERT, params=query(date=value)), 400, "invalid_date")


def test_a_future_date_is_rejected_without_asking_the_upstream(client):
    tomorrow = config.today_in_ecb_tz() + timedelta(days=1)
    assert_error(
        client.get(CONVERT, params=query(date=tomorrow.isoformat())),
        400,
        "date_in_future",
    )


def test_a_date_before_the_series_starts_is_rejected(client):
    assert_error(
        client.get(CONVERT, params=query(date="1998-12-31")),
        400,
        "date_before_series_start",
    )


def test_the_first_day_of_the_series_is_allowed():
    assert validation.validate_date("1999-01-04") == config.SERIES_START


def test_today_is_allowed():
    today = config.today_in_ecb_tz()
    assert validation.validate_date(today.isoformat()) == today


def test_date_bounds_are_drawn_against_a_supplied_today():
    # The boundary is a real one, so it is worth pinning rather than relying on
    # whatever day the suite happens to run.
    today = date(2026, 8, 28)
    assert validation.validate_date("2026-08-28", today=today) == today
    with pytest.raises(DateInFuture):
        validation.validate_date("2026-08-29", today=today)
    with pytest.raises(DateBeforeSeriesStart):
        validation.validate_date("1999-01-03", today=today)
    with pytest.raises(InvalidDate):
        validation.validate_date("nonsense", today=today)


# --- the contract itself -------------------------------------------------------


def test_no_raw_fastapi_validation_body_escapes(client):
    # FastAPI would answer 422 with a "detail" list. Nothing may see that.
    for params in (query(amount="abc"), query(**{"from": "EU"}), query(date="nope")):
        response = client.get(CONVERT, params=params)
        assert response.status_code != 422
        assert "detail" not in response.json()


def test_every_error_class_carries_a_code_and_a_status():
    for error_class in FxError.__subclasses__():
        assert error_class.error_code
        assert 400 <= error_class.http_status < 600
