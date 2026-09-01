"""Frankfurter HTTP client and the small cache around it.

This module deliberately stops at the upstream boundary.  It returns the rate
and the date Frankfurter actually published it; request validation and
conversion arithmetic belong to the layers above it.  The stale-rate guard is
applied before caching so a rejected upstream value cannot poison the cache.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
import json
import re
import time

import httpx

from app import config, conversion
from app.errors import (
    FxError,
    RateUnavailable,
    UpstreamError,
    UpstreamInvalidResponse,
    UpstreamTimeout,
    UpstreamUnavailable,
)

RateResult = tuple[Decimal, date]
CacheKey = tuple[str, str, str]
Clock = Callable[[], float]

UPSTREAM_TIMEOUT = httpx.Timeout(
    connect=2.0,
    read=3.0,
    write=3.0,
    pool=3.0,
)

# httpx's read timeout applies to each socket read, not to the exchange as a
# whole, so an upstream trickling one byte every two seconds satisfies it
# forever while holding a request, a task and a pool slot open. This is the
# deadline that actually bounds the call.
UPSTREAM_TOTAL_TIMEOUT_SECONDS = 8.0

# A rates or currencies document is a few kilobytes. httpx imposes no limit of
# its own, so without this a hostile or misconfigured upstream can hand us a
# body of any size and we will faithfully buffer all of it.
MAX_RESPONSE_BYTES = 1024 * 1024

# A ceiling on upstream calls in flight at once, so that a burst of distinct
# cache keys cannot fan out into an unbounded number of connections.
MAX_CONCURRENT_UPSTREAM_REQUESTS = 8

# The endpoint fails open when the currency catalogue is unavailable. Without a
# negative cache it would retry the dead catalogue on every single conversion
# and pay a full connect timeout each time, so failing open would cost more
# than failing closed. Fail open *fast* instead.
CURRENCY_FAILURE_TTL_SECONDS = 60

RATE_CACHE_MAXSIZE = 512
HISTORICAL_TTL_SECONDS = 24 * 60 * 60
CURRENT_TTL_SECONDS = 5 * 60
_CURRENCY_CODE = re.compile(r"^[A-Za-z]{3}$")

# Sentinel key for the currency catalogue in the in-flight map; the rate keys
# there are 3-tuples, so this cannot collide with one.
_CURRENCIES_KEY = "currencies"


@dataclass(frozen=True)
class _CacheEntry:
    value: RateResult
    expires_at: float


class FrankfurterClient:
    """A single reusable async client for Frankfurter.

    ``transport`` is intentionally injectable so tests can use
    ``httpx.MockTransport`` without opening a socket.  The production client
    is created once by the FastAPI lifespan and closed there.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ValueError("pass either http_client or transport, not both")

        self._http = http_client or httpx.AsyncClient(
            transport=transport,
            timeout=UPSTREAM_TIMEOUT,
        )
        self._owns_http = http_client is None
        self._clock = clock or time.monotonic
        self._rate_cache: OrderedDict[CacheKey, _CacheEntry] = OrderedDict()
        self._currencies: frozenset[str] | None = None
        self._currencies_failed_until: float | None = None
        self._inflight: dict[object, asyncio.Future] = {}
        self._upstream_slots = asyncio.Semaphore(MAX_CONCURRENT_UPSTREAM_REQUESTS)

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The underlying client, exposed for lifespan/tests, not for callers."""

        return self._http

    async def close(self) -> None:
        """Close the underlying client when this instance owns it."""

        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "FrankfurterClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    def clear(self) -> None:
        """Clear rate and currency caches so tests start from a clean slate."""

        self._rate_cache.clear()
        self._currencies = None
        self._currencies_failed_until = None

    clear_cache = clear

    async def _single_flight(
        self,
        key: object,
        fetch: Callable[[], Awaitable[object]],
    ) -> object:
        """Run ``fetch`` once per key and share its outcome with concurrent callers.

        Without this, every concurrent miss on the same key opens its own
        upstream request — worst precisely at a cold start or a TTL expiry,
        which is when traffic is heaviest.
        """

        inflight = self._inflight.get(key)
        if inflight is not None:
            # Shielded so that one caller giving up cannot cancel the fetch the
            # other waiters are still relying on.
            return await asyncio.shield(inflight)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await fetch()
        except asyncio.CancelledError:
            future.cancel()
            raise
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)
            # Retrieve a leader-only failure so asyncio does not log
            # "exception was never retrieved" when nobody was waiting on it.
            if not future.cancelled():
                future.exception()

    async def get_rate(
        self,
        base: str,
        target: str,
        requested_date: date | str | None = None,
        *,
        today: date | None = None,
    ) -> RateResult:
        """Return ``(rate, published_date)`` for one requested observation.

        ``today`` lets the caller pin the ECB day boundary for the whole
        request. Reading the clock again here is what allows a request that
        straddles midnight to reject its own valid answer.
        """

        today = today or config.today_in_ecb_tz()
        base = base.upper()
        target = target.upper()
        asked_date = _requested_date(requested_date, today)
        date_key = _date_key(requested_date, today)
        key = (base, target, date_key)

        cached = self._cache_get(key)
        if cached is not None:
            return cached

        async def fetch() -> RateResult:
            payload = await self._get_json(
                config.upstream_url(date_key),
                params={"base": base, "symbols": target},
                not_found_is_rate=True,
            )
            result = _parse_rate_payload(
                payload,
                base,
                target,
                asked_date=asked_date,
            )

            # Stale rates are not usable endpoint results. Reject them before
            # caching so a transiently old upstream response cannot suppress a
            # recovered rate for the full historical TTL.
            conversion.check_staleness(asked_date, result[1])

            # Only successful, structurally valid responses enter the cache.
            self._cache_put(key, result, _ttl_for(date_key, today))
            return result

        return await self._single_flight(key, fetch)  # type: ignore[return-value]

    async def fetch_rate(
        self,
        base: str,
        target: str,
        requested_date: date | str | None = None,
        *,
        today: date | None = None,
    ) -> RateResult:
        """Compatibility spelling for callers that prefer ``fetch_*``."""

        return await self.get_rate(base, target, requested_date, today=today)

    async def get_currencies(self) -> frozenset[str]:
        """Return the ECB currency codes, caching a successful list forever."""

        if self._currencies is not None:
            return self._currencies

        if (
            self._currencies_failed_until is not None
            and self._clock() < self._currencies_failed_until
        ):
            # The endpoint fails open on this error. Failing open *fast* is the
            # whole point: a dead catalogue must not add a connect timeout to
            # every conversion for as long as it stays dead.
            raise UpstreamUnavailable(
                "The currency catalogue is temporarily unavailable."
            )

        async def fetch() -> frozenset[str]:
            try:
                payload = await self._get_json(
                    config.upstream_url("currencies"),
                    not_found_is_rate=False,
                )
                currencies = _parse_currency_payload(payload)
            except FxError:
                self._currencies_failed_until = (
                    self._clock() + CURRENCY_FAILURE_TTL_SECONDS
                )
                raise
            # A failed call raises before this assignment, so failures are never
            # turned into a permanently cached empty list.
            self._currencies = currencies
            self._currencies_failed_until = None
            return currencies

        return await self._single_flight(_CURRENCIES_KEY, fetch)  # type: ignore[return-value]

    async def fetch_currencies(self) -> frozenset[str]:
        return await self.get_currencies()

    def _cache_get(self, key: CacheKey) -> RateResult | None:
        entry = self._rate_cache.pop(key, None)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            return None
        self._rate_cache[key] = entry
        return entry.value

    def _cache_put(self, key: CacheKey, value: RateResult, ttl: float) -> None:
        self._rate_cache.pop(key, None)
        self._rate_cache[key] = _CacheEntry(
            value=value,
            expires_at=self._clock() + ttl,
        )
        while len(self._rate_cache) > RATE_CACHE_MAXSIZE:
            self._rate_cache.popitem(last=False)

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        not_found_is_rate: bool,
    ) -> object:
        try:
            # The body is streamed rather than read whole so that the size cap
            # can stop a hostile response before it is in memory, and the whole
            # exchange sits under one deadline that httpx's per-read timeout
            # cannot provide.
            async with asyncio.timeout(UPSTREAM_TOTAL_TIMEOUT_SECONDS):
                # The deadline is started before the slot is acquired, not
                # after. Queueing is time the caller spends waiting too, so
                # with these nested the other way a request could sit through
                # several other calls' worth of queueing and still be judged
                # inside its budget: the bound would cover the exchange but
                # not the wait for permission to begin it.
                async with self._upstream_slots:
                    async with self._http.stream("GET", url, params=params) as response:
                        if response.status_code == 404 and not_found_is_rate:
                            raise RateUnavailable(
                                "No exchange rate is available for that request."
                            )
                        if not 200 <= response.status_code < 300:
                            raise UpstreamError(
                                "The exchange-rate service returned HTTP "
                                f"{response.status_code}."
                            )
                        content = await _read_capped(response)
        except TimeoutError as exc:
            # asyncio.timeout fired: the exchange as a whole outlived its budget
            # even though no individual socket operation did.
            raise UpstreamTimeout(
                "The exchange-rate service took too long to respond."
            ) from exc
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(
                "The exchange-rate service took too long to respond."
            ) from exc
        except httpx.NetworkError as exc:
            raise UpstreamUnavailable(
                "The exchange-rate service could not be reached."
            ) from exc
        except (httpx.InvalidURL, httpx.RequestError) as exc:
            raise UpstreamUnavailable(
                "The exchange-rate service could not be reached."
            ) from exc

        try:
            # Parsing JSON directly lets numeric rates become Decimal instead of
            # making a float round trip through response.json().
            return json.loads(content, parse_float=Decimal)
        except (ValueError, TypeError, RecursionError) as exc:
            # ValueError covers JSONDecodeError and UnicodeDecodeError, but also
            # CPython's integer-string digit limit, which a long unquoted number
            # trips; RecursionError covers a deeply nested body. Every one of
            # them is the upstream's fault, and none may surface as an
            # internal_error that tells the caller *we* are broken.
            raise UpstreamInvalidResponse(
                "The exchange-rate service returned invalid JSON."
            ) from exc


# The shorter name is useful in the application layer while the explicit name
# documents which upstream this client speaks to.
UpstreamClient = FrankfurterClient


async def _read_capped(response: httpx.Response) -> bytes:
    """Read a response body, refusing to buffer more than ``MAX_RESPONSE_BYTES``.

    The cap is applied to decoded bytes as they arrive, so a compressed body
    that expands past the limit is abandoned at the limit rather than after.
    """

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise UpstreamInvalidResponse(
                "The exchange-rate service returned a response that is too large."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _date_key(requested_date: date | str | None, today: date | None = None) -> str:
    if requested_date is None or requested_date == "latest":
        return "latest"
    return _requested_date(requested_date, today).isoformat()


def _requested_date(
    requested_date: date | str | None, today: date | None = None
) -> date:
    if requested_date is None or requested_date == "latest":
        return today or config.today_in_ecb_tz()
    if isinstance(requested_date, datetime):
        return requested_date.date()
    if isinstance(requested_date, date):
        return requested_date
    parsed = datetime.strptime(requested_date, "%Y-%m-%d").date()
    if parsed.isoformat() != requested_date:
        raise ValueError("requested_date must be written as YYYY-MM-DD")
    return parsed


def _ttl_for(date_key: str, today: date | None = None) -> int:
    today = today or config.today_in_ecb_tz()
    if date_key == "latest" or date_key == today.isoformat():
        return CURRENT_TTL_SECONDS
    return HISTORICAL_TTL_SECONDS


def _parse_rate_payload(
    payload: object,
    base: str,
    target: str,
    *,
    asked_date: date | None = None,
) -> RateResult:
    if not isinstance(payload, Mapping):
        raise UpstreamInvalidResponse(
            "The exchange-rate response is missing its rates object."
        )

    raw_base = payload.get("base")
    if not isinstance(raw_base, str) or raw_base.upper() != base:
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains an invalid base currency."
        )

    rates = payload.get("rates")
    if not isinstance(rates, Mapping) or target not in rates:
        raise UpstreamInvalidResponse(
            "The exchange-rate response is missing the requested rate."
        )

    raw_rate = rates[target]
    try:
        if isinstance(raw_rate, bool):
            raise InvalidOperation
        rate = Decimal(str(raw_rate))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains an invalid rate."
        ) from exc
    if not rate.is_finite():
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains an invalid rate."
        )
    if rate <= 0:
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains a non-positive rate."
        )

    try:
        # The endpoint accepts amounts up to MAX_AMOUNT and rounds to cents.
        # Reject rates that cannot produce a representable result before they
        # enter the cache.
        largest_result = (config.MAX_AMOUNT * rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except DecimalException as exc:
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains a rate that is too large."
        ) from exc
    if largest_result == 0:
        # The mirror image of the check above, and the same guard `amount`
        # already has on the way in. The response renders `rate` in positional
        # notation, so `1e-100000000` — positive, finite, and thirteen bytes
        # inside a body that passes the 1 MB cap — would render a hundred
        # million characters. Nothing is lost by refusing it: a rate this small
        # converts every amount the endpoint accepts into 0.00 anyway.
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains a rate that is too small."
        )

    raw_date = payload.get("date")
    if not isinstance(raw_date, str):
        raise UpstreamInvalidResponse(
            "The exchange-rate response is missing its publication date."
        )
    try:
        # Frankfurter documents this field as YYYY-MM-DD.  Keep the upstream
        # contract strict rather than accepting Python's newer compact-date
        # extensions (for example, 20260828).
        published_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains an invalid publication date."
        ) from exc
    if published_date.isoformat() != raw_date:
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains an invalid publication date."
        )

    if published_date < config.SERIES_START or (
        asked_date is not None and published_date > asked_date
    ):
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains an invalid publication date."
        )

    return rate, published_date


def _parse_currency_payload(payload: object) -> frozenset[str]:
    if not isinstance(payload, Mapping) or not payload:
        raise UpstreamInvalidResponse(
            "The exchange-rate service returned an invalid currency list."
        )

    currencies: set[str] = set()
    for raw_code in payload:
        if not isinstance(raw_code, str) or not _CURRENCY_CODE.fullmatch(raw_code):
            raise UpstreamInvalidResponse(
                "The exchange-rate service returned an invalid currency list."
            )
        currencies.add(raw_code.upper())
    return frozenset(currencies)
