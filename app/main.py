"""HTTP surface for the currency conversion tool."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import Mapping
from decimal import Decimal
import json
import logging

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app import config, conversion, validation
from app.errors import (
    FxError,
    InvalidAmount,
    InvalidCurrencyCode,
    InvalidDate,
    InvalidRequest,
    RateLimited,
    UnknownCurrency,
)
from app.ratelimit import RateLimiter
from app.upstream import FrankfurterClient

logger = logging.getLogger("fx-tool")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own one upstream client and one rate limiter for the process lifetime."""

    upstream = FrankfurterClient()
    app.state.upstream = upstream
    app.state.rate_limiter = RateLimiter(config.rate_limit_per_minute())
    try:
        yield
    finally:
        await upstream.close()

app = FastAPI(
    title="fx-tool",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Converts an amount between two currencies using ECB reference rates. "
        "Never invents a rate, and always reports the date the rate it used "
        "actually belongs to."
    ),
)


# --- keep one caller from becoming everyone's problem ------------------------


@app.middleware("http")
async def limit_request_rate(request: Request, call_next):
    """Throttle the conversion endpoint per client.

    The response is built here rather than raised: middleware sits outside the
    exception handlers below, so a raised ``FxError`` would escape the one
    documented error shape instead of being rendered into it.

    ``/health`` is deliberately exempt — an orchestrator's liveness probe is not
    the traffic this is defending against.
    """

    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None and limiter.enabled and request.url.path == "/tools/convert":
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            error = RateLimited(
                "Too many conversion requests; please retry in a moment."
            )
            return JSONResponse(
                status_code=error.http_status, content=error.payload()
            )
    return await call_next(request)


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


def _json_fragment(value: object) -> str:
    """Encode JSON while retaining Decimal values as numeric decimal tokens."""

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be encoded as JSON")
        return format(value, "f")
    if isinstance(value, Mapping):
        items = ",".join(
            f"{json.dumps(str(key), ensure_ascii=False)}:{_json_fragment(item)}"
            for key, item in value.items()
        )
        return "{" + items + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json_fragment(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class DecimalJSONResponse(Response):
    """JSON response that does not route Decimal values through float."""

    media_type = "application/json"

    def render(self, content: object) -> bytes:
        return _json_fragment(content).encode("utf-8")


class ConversionResponse(BaseModel):
    """The documented success shape; runtime rendering stays Decimal-safe."""

    model_config = ConfigDict(populate_by_name=True)

    amount: Decimal = Field(json_schema_extra={"type": "number"})
    from_currency: str = Field(alias="from")
    to_currency: str = Field(alias="to")
    rate: Decimal = Field(json_schema_extra={"type": "number"})
    result: Decimal = Field(json_schema_extra={"type": "number"})
    rate_date: str
    asked_date: str
    source: str


@app.get("/tools/convert", response_model=ConversionResponse)
async def convert(
    request: Request,
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
) -> Response:
    """Convert an amount between two currencies at ECB reference rates."""
    # One reading of the clock for the whole request. Sampling it again deeper
    # in the call stack lets a request that straddles Berlin midnight ask about
    # one day and validate the answer against another.
    today = config.today_in_ecb_tz()

    validation.validate_amount(amount)
    base = validation.validate_currency(from_currency, "from")
    target = validation.validate_currency(to_currency, "to")
    validation.validate_pair(base, target)
    resolved_date = conversion.resolve_asked_date(asked_date, today)
    was_explicit = asked_date is not None

    upstream: FrankfurterClient = request.app.state.upstream

    # The currency catalogue only improves an otherwise ambiguous 404.  If it
    # is unavailable, fail open and let the rate request provide the answer.
    try:
        currencies = await upstream.get_currencies()
    except FxError:
        currencies = None
    if currencies is not None:
        for code, field in ((base, "from"), (target, "to")):
            if code not in currencies:
                raise UnknownCurrency(
                    f"'{field}' currency {code} is not published by the ECB."
                )

    rate, rate_date = await upstream.get_rate(
        base,
        target,
        resolved_date if was_explicit else None,
        today=today,
    )
    conversion.check_staleness(resolved_date, rate_date)

    return DecimalJSONResponse(
        {
            "amount": amount,
            "from": base,
            "to": target,
            "rate": rate,
            "result": conversion.compute_result(amount, rate),
            "rate_date": rate_date.isoformat(),
            "asked_date": resolved_date.isoformat(),
            "source": "ECB via frankfurter.dev",
        }
    )


@app.get("/scalar", include_in_schema=False)
async def scalar_reference() -> HTMLResponse:
    """A browsable API reference, for trying the endpoint by hand.

    Kept out of the OpenAPI schema on purpose: that document is what a calling
    agent reads to build its tool definition, and it should describe exactly one
    endpoint. This is a convenience for humans, not part of the tool surface.
    """
    return HTMLResponse(
        """<!doctype html>
<html>
  <head>
    <title>fx-tool API reference</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
  </head>
  <body>
    <div id="app"></div>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
    <script>
      Scalar.createApiReference('#app', { url: '/openapi.json' })
    </script>
  </body>
</html>
"""
    )


@app.get("/health")
async def health() -> dict:
    """Liveness only.

    This deliberately does not probe the upstream: a broken upstream does not
    mean this process should be restarted, and an orchestrator should not take
    us down because the ECB feed is having a bad morning.
    """
    return {"ok": True}
