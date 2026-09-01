"""Frankfurter HTTP client and the small cache around it.

This module deliberately stops at the upstream boundary.  It returns the rate
and the date Frankfurter actually published it; request validation, stale-rate
policy, and conversion arithmetic belong to the layers above it.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
import time

import httpx

from app import config
from app.errors import (
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
RATE_CACHE_MAXSIZE = 512
HISTORICAL_TTL_SECONDS = 24 * 60 * 60
CURRENT_TTL_SECONDS = 5 * 60
_CURRENCY_CODE = re.compile(r"^[A-Za-z]{3}$")


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

    clear_cache = clear

    async def get_rate(
        self,
        base: str,
        target: str,
        requested_date: date | str | None = None,
    ) -> RateResult:
        """Return ``(rate, published_date)`` for one requested observation."""

        base = base.upper()
        target = target.upper()
        asked_date = _requested_date(requested_date)
        date_key = _date_key(requested_date)
        key = (base, target, date_key)

        cached = self._cache_get(key)
        if cached is not None:
            return cached

        payload = await self._get_json(
            config.upstream_url(date_key),
            params={"base": base, "symbols": target},
            not_found_is_rate=True,
        )
        result = _parse_rate_payload(payload, target, asked_date=asked_date)

        # Only successful, structurally valid responses enter the cache.
        self._cache_put(key, result, _ttl_for(date_key))
        return result

    async def fetch_rate(
        self,
        base: str,
        target: str,
        requested_date: date | str | None = None,
    ) -> RateResult:
        """Compatibility spelling for callers that prefer ``fetch_*``."""

        return await self.get_rate(base, target, requested_date)

    async def get_currencies(self) -> frozenset[str]:
        """Return the ECB currency codes, caching a successful list forever."""

        if self._currencies is not None:
            return self._currencies

        payload = await self._get_json(
            config.upstream_url("currencies"),
            not_found_is_rate=False,
        )
        currencies = _parse_currency_payload(payload)
        # A failed call raises before this assignment, so failures are never
        # turned into a permanently cached empty list.
        self._currencies = currencies
        return currencies

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
            response = await self._http.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(
                "The exchange-rate service took too long to respond."
            ) from exc
        except httpx.NetworkError as exc:
            raise UpstreamUnavailable(
                "The exchange-rate service could not be reached."
            ) from exc

        if response.status_code == 404 and not_found_is_rate:
            raise RateUnavailable("No exchange rate is available for that request.")
        if response.status_code >= 500 or not 200 <= response.status_code < 300:
            raise UpstreamError(
                f"The exchange-rate service returned HTTP {response.status_code}."
            )

        try:
            # Parsing JSON directly lets numeric rates become Decimal instead of
            # making a float round trip through response.json().
            return json.loads(response.content, parse_float=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise UpstreamInvalidResponse(
                "The exchange-rate service returned invalid JSON."
            ) from exc


# The shorter name is useful in the application layer while the explicit name
# documents which upstream this client speaks to.
UpstreamClient = FrankfurterClient


def _date_key(requested_date: date | str | None) -> str:
    if requested_date is None or requested_date == "latest":
        return "latest"
    return _requested_date(requested_date).isoformat()


def _requested_date(requested_date: date | str | None) -> date:
    if requested_date is None or requested_date == "latest":
        return config.today_in_ecb_tz()
    if isinstance(requested_date, datetime):
        return requested_date.date()
    if isinstance(requested_date, date):
        return requested_date
    return datetime.strptime(requested_date, "%Y-%m-%d").date()


def _ttl_for(date_key: str) -> int:
    if date_key == "latest" or date_key == config.today_in_ecb_tz().isoformat():
        return CURRENT_TTL_SECONDS
    return HISTORICAL_TTL_SECONDS


def _parse_rate_payload(
    payload: object,
    target: str,
    *,
    asked_date: date | None = None,
) -> RateResult:
    if not isinstance(payload, Mapping):
        raise UpstreamInvalidResponse(
            "The exchange-rate response is missing its rates object."
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
    except ValueError as exc:
        raise UpstreamInvalidResponse(
            "The exchange-rate response contains an invalid publication date."
        ) from exc

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
