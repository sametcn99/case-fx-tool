"""Settings and constants.

Environment variables are read at call time, not at import time, so that tests
can point the service somewhere else without reloading the module.

Nothing here may hardcode the real upstream host beyond the documented default:
the review harness points FX_UPSTREAM_BASE at a fake upstream.
"""

from __future__ import annotations

import os
from datetime import date
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

# The ECB publishes on Frankfurt time, so that is where the day boundary sits.
# Using UTC would put "today" a day behind during the early hours.
ECB_TZ = ZoneInfo("Europe/Berlin")


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
