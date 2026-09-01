"""The failures this service can explain.

Every error leaves the service in one shape:

    {"error": "<machine code>", "message": "<a sentence a person could read>"}

with a non-2xx status. The caller is a language model talking to a customer, so
the two fields have different jobs: `error` is what the model branches on, and
`message` is what it can pass on to a person. A code only exists where it would
lead the caller to do something different — there is no separate code for every
way the upstream can disappoint us.
"""

from __future__ import annotations


class FxError(Exception):
    """Base class for anything we can describe to the caller."""

    http_status = 500
    error_code = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def payload(self) -> dict[str, str]:
        return {"error": self.error_code, "message": self.message}


# --- the request was wrong ---------------------------------------------------


class InvalidRequest(FxError):
    """A malformed request that none of the more specific codes describes."""

    http_status = 400
    error_code = "invalid_request"


class InvalidAmount(FxError):
    http_status = 400
    error_code = "invalid_amount"


class InvalidCurrencyCode(FxError):
    """Not shaped like a currency code at all."""

    http_status = 400
    error_code = "invalid_currency_code"


class UnknownCurrency(FxError):
    """Shaped like a code, but the ECB does not publish it."""

    http_status = 400
    error_code = "unknown_currency"


class SameCurrency(FxError):
    http_status = 400
    error_code = "same_currency"


class InvalidDate(FxError):
    http_status = 400
    error_code = "invalid_date"


class DateInFuture(FxError):
    http_status = 400
    error_code = "date_in_future"


class DateBeforeSeriesStart(FxError):
    http_status = 400
    error_code = "date_before_series_start"


# --- there is no rate we are willing to stand behind -------------------------


class RateUnavailable(FxError):
    http_status = 404
    error_code = "rate_unavailable"


class RateTooStale(FxError):
    """A rate exists, but it sits too far behind the date that was asked for."""

    http_status = 404
    error_code = "rate_too_stale"


# --- the upstream let us down ------------------------------------------------


class UpstreamUnavailable(FxError):
    http_status = 502
    error_code = "upstream_unavailable"


class UpstreamTimeout(FxError):
    http_status = 504
    error_code = "upstream_timeout"


class UpstreamError(FxError):
    http_status = 502
    error_code = "upstream_error"


class UpstreamInvalidResponse(FxError):
    """A response arrived, but it was not something we could read as rates."""

    http_status = 502
    error_code = "upstream_invalid_response"
