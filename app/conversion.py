"""Pure date and arithmetic rules for the conversion endpoint."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from app import config, validation
from app.errors import RateTooStale


def resolve_asked_date(raw: str | None) -> date:
    """Return the explicit date, or today's date in the ECB timezone."""

    if raw is None:
        return config.today_in_ecb_tz()
    return validation.validate_date(raw)


def upstream_path_for(asked_date: date, was_explicit: bool) -> str:
    """Select Frankfurter's latest or dated endpoint."""

    return asked_date.isoformat() if was_explicit else "latest"


def check_staleness(asked_date: date, rate_date: date) -> None:
    """Reject a published rate that is older than the documented safety window."""

    if asked_date - rate_date > timedelta(days=config.MAX_STALENESS_DAYS):
        raise RateTooStale(
            f"The newest rate available is from {rate_date}, which is too old "
            f"for the requested date {asked_date}."
        )


def compute_result(amount: Decimal, rate: Decimal) -> Decimal:
    """Multiply without floats and round the result only at the end."""

    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
