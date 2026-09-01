"""HTTP surface for the currency conversion tool."""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import validation
from app.errors import FxError, InvalidAmount, InvalidCurrencyCode, InvalidDate, InvalidRequest

logger = logging.getLogger("fx-tool")

app = FastAPI(
    title="fx-tool",
    version="0.1.0",
    description=(
        "Converts an amount between two currencies using ECB reference rates. "
        "Never invents a rate, and always reports the date the rate it used "
        "actually belongs to."
    ),
)


class _NotImplementedYet(FxError):
    """Temporary. The lookup and the arithmetic arrive with the upstream client."""

    http_status = 501
    error_code = "not_implemented"


# --- one error shape, whoever raised it --------------------------------------


@app.exception_handler(FxError)
async def handle_fx_error(request: Request, exc: FxError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.payload())


# FastAPI's own 422 body does not match the documented error shape, so it is
# translated here rather than allowed to leak out of the contract.
_FIELD_ERRORS = {
    "amount": (
        InvalidAmount,
        "amount is required and must be a positive number, for example 250.",
    ),
    "from": (
        InvalidCurrencyCode,
        "'from' is required and must be a three-letter currency code such as EUR.",
    ),
    "to": (
        InvalidCurrencyCode,
        "'to' is required and must be a three-letter currency code such as TRY.",
    ),
    "date": (InvalidDate, "date must be written as YYYY-MM-DD, for example 2026-08-28."),
}


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = exc.errors()
    field = errors[0]["loc"][-1] if errors and errors[0].get("loc") else None
    error_class, message = _FIELD_ERRORS.get(
        field, (InvalidRequest, "The request could not be understood.")
    )
    return await handle_fx_error(request, error_class(message))


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """A bug on our side is a failure, not a zero.

    The detail is logged rather than returned, but the caller is still told
    plainly that it did not get an answer.
    """
    logger.exception("unhandled error serving %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "The conversion failed for an unexpected reason.",
        },
    )


# --- routes ------------------------------------------------------------------


@app.get("/tools/convert")
async def convert(
    amount: Decimal = Query(
        ...,
        description="How much to convert. Must be greater than zero.",
        examples=[250],
    ),
    from_currency: str = Query(
        ...,
        alias="from",
        description="Currency to convert from, as a three-letter code.",
        examples=["EUR"],
    ),
    to_currency: str = Query(
        ...,
        alias="to",
        description="Currency to convert to, as a three-letter code.",
        examples=["TRY"],
    ),
    asked_date: str | None = Query(
        None,
        alias="date",
        description=(
            "The date to price the conversion on, as YYYY-MM-DD. Defaults to the "
            "most recent published rates. The response reports rate_date, which "
            "is the date the rate actually belongs to, and may be earlier than "
            "this one when the ECB published nothing that day."
        ),
        examples=["2026-08-28"],
    ),
) -> dict:
    """Convert an amount between two currencies at ECB reference rates."""
    validation.validate_amount(amount)
    base = validation.validate_currency(from_currency, "from")
    target = validation.validate_currency(to_currency, "to")
    validation.validate_pair(base, target)
    if asked_date is not None:
        validation.validate_date(asked_date)

    raise _NotImplementedYet(
        "This endpoint does not fetch rates yet; only its inputs are checked."
    )


@app.get("/health")
async def health() -> dict:
    """Liveness only.

    This deliberately does not probe the upstream: a broken upstream does not
    mean this process should be restarted, and an orchestrator should not take
    us down because the ECB feed is having a bad morning.
    """
    return {"ok": True}
