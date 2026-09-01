"""Everything we can reject without asking the upstream.

This matters more than it looks. The upstream answers a future date, a date
before the series starts and a currency that does not exist with the same
undifferentiated 404, so if we forwarded those questions we could only ever
tell the caller "no rate". Checking them here is what lets each one come back
as a distinct code the caller can act on.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from app import config
from app.errors import (
    DateBeforeSeriesStart,
    DateInFuture,
    InvalidAmount,
    InvalidCurrencyCode,
    InvalidDate,
    SameCurrency,
)

_CURRENCY_PATTERN = re.compile(r"^[A-Za-z]{3}$")


def validate_amount(amount: Decimal) -> Decimal:
    """Reject amounts we cannot honestly convert."""
    if not amount.is_finite():
        raise InvalidAmount("amount must be a finite number, for example 250.")
    if amount <= 0:
        raise InvalidAmount(f"amount must be greater than zero; got {amount}.")
    if amount > config.MAX_AMOUNT:
        raise InvalidAmount(
            f"amount must not exceed {config.MAX_AMOUNT:f}; got {amount}."
        )
    # A long fraction is fine: it is carried as a Decimal and only the result is
    # rounded, so there is no reason to refuse the caller's precision.
    return amount


def validate_currency(raw: str, field: str) -> str:
    """Check the shape of a currency code and normalise it to upper case.

    Whether the code actually exists is a separate question, answered against
    the upstream's currency list.
    """
    if not _CURRENCY_PATTERN.match(raw or ""):
        raise InvalidCurrencyCode(
            f"'{field}' must be a three-letter currency code such as EUR; got {raw!r}."
        )
    return raw.upper()


def validate_pair(base: str, target: str) -> None:
    """Refuse a conversion of a currency into itself.

    Answering 1.0 would be arithmetically true but we could not fill in
    rate_date honestly — there is no ECB publication for EUR against EUR — and
    inventing a date is the one thing this service must never do.
    """
    if base == target:
        raise SameCurrency(
            f"'from' and 'to' are both {base}; there is no exchange rate for a "
            "currency against itself."
        )


def validate_date(raw: str, today: date | None = None) -> date:
    """Parse and bounds-check the date the caller asked about."""
    try:
        asked = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise InvalidDate(
            f"date must be written as YYYY-MM-DD; got {raw!r}."
        ) from None

    today = today or config.today_in_ecb_tz()
    if asked > today:
        raise DateInFuture(
            f"The ECB has not published rates for {asked} yet; that date is in the future."
        )
    if asked < config.SERIES_START:
        raise DateBeforeSeriesStart(
            f"The ECB reference rate series starts on {config.SERIES_START}, "
            f"so there is nothing to report for {asked}."
        )
    return asked
