"""Settings and constants.

Environment variables are read at call time, not at import time, so that tests
can point the service somewhere else without reloading the module.

Nothing here may hardcode the real upstream host beyond the documented default:
the review harness points FX_UPSTREAM_BASE at a fake upstream.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

DEFAULT_UPSTREAM_BASE = "https://api.frankfurter.dev"
DEFAULT_PORT = 8080

# The ECB series starts here. Anything earlier can never be answered, and the
# upstream reports it the same way it reports a future date, so we draw the
# line ourselves rather than guessing at a 404.
SERIES_START = date(1999, 1, 4)

# How far a published rate may sit behind the date that was asked for before we
# refuse to use it. The longest ECB closure is a few business days, so this
# never affects an ordinary weekend, but it stops a much older rate from being
# presented as an answer to today's question.
MAX_STALENESS_DAYS = 7

# An upper bound on `amount`, so that absurd inputs fail as a clean 400 rather
# than overflowing to infinity somewhere in the arithmetic.
MAX_AMOUNT = Decimal("1e12")

# A lower bound on the exponent, which is a different question from the upper
# bound on the value. The response echoes `amount` back in positional notation,
# so `1E-100000000` — positive, finite, and far below MAX_AMOUNT — would render
# as a hundred million characters from a seventeen-byte query string. Ten
# decimal places is the precision the tool contract already documents.
MAX_AMOUNT_DECIMALS = 10

# How many requests one client may make per minute against the conversion
# endpoint. The endpoint is unauthenticated and every cache miss becomes an
# upstream call, so without a ceiling one caller can walk more distinct
# (from, to, date) keys than the rate cache holds and turn this service into a
# free amplifier against the ECB feed. Set FX_RATE_LIMIT_PER_MINUTE=0 to
# disable it when something in front of the service already does this job.
DEFAULT_RATE_LIMIT_PER_MINUTE = 60

# The ECB publishes on Frankfurt time, so that is where the day boundary sits.
# Using UTC would put "today" a day behind during the early hours.
ECB_TZ = ZoneInfo("Europe/Berlin")


def today_in_ecb_tz() -> date:
    """Today, as the ECB would date it.

    Lives here next to ECB_TZ because the question it answers is "where is the
    day boundary", which is a setting rather than conversion logic.
    """
    return datetime.now(tz=ECB_TZ).date()


def upstream_base() -> str:
    """The upstream base URL, without a trailing slash."""
    return (os.environ.get("FX_UPSTREAM_BASE") or DEFAULT_UPSTREAM_BASE).rstrip("/")


def upstream_url(path: str) -> str:
    """Build an upstream URL for `path`, e.g. "latest" or "2026-08-28".

    The `/v1` prefix is required by the real API — `/latest` without it is a
    404 — while the documented default base has no prefix, so it belongs here
    rather than in the configured value.
    """
    return f"{upstream_base()}/v1/{path.lstrip('/')}"


def port() -> int:
    """The port to listen on. Falls back to the default if PORT is unusable."""
    try:
        return int(os.environ["PORT"])
    except (KeyError, ValueError):
        return DEFAULT_PORT


def rate_limit_per_minute() -> int:
    """Requests allowed per client per minute. Zero disables the limiter."""
    try:
        value = int(os.environ["FX_RATE_LIMIT_PER_MINUTE"])
    except (KeyError, ValueError):
        return DEFAULT_RATE_LIMIT_PER_MINUTE
    return max(value, 0)
