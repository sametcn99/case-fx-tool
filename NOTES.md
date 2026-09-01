# Notes

## Decisions

- **Weekend and holiday dates:** use the previous published ECB rate when it is
  no more than seven days old. The response keeps `asked_date` and the upstream's
  `rate_date` separate, so a customer can see which day the number belongs to.
  No extra `stale` field was added because the existing two dates already carry
  that signal without expanding the tool contract.
- **Staleness:** reject a rate older than seven days with `rate_too_stale`.
  This covers normal closures while preventing an unexpectedly old rate from
  being presented as current.
- **No explicit date:** treat it as today's date in `Europe/Berlin`, then use
  Frankfurter's `latest` endpoint. If today's rate is not published yet,
  `rate_date` makes that visible.
- **Same currency:** return `same_currency` instead of inventing an identity
  rate. Returning `1.0` would leave no honest publication date to report.
- **Currency validation:** cache the ECB currency catalogue and reject unknown
  codes locally. If the catalogue itself is unavailable, fail open and let the
  rate request return the more general upstream/rate error.
- **Decimal amounts:** accept ten decimal places and use `Decimal` throughout;
  only the final result is rounded to two places.
- **Upstream response hardening:** accept only finite, positive rates and
  publication dates from `1999-01-04` through the requested date. Invalid
  upstream data is rejected as `upstream_invalid_response` before it can be
  cached or shown to a customer. The response `base` must also match the
  requested source currency, and the rate must be representable for the
  documented maximum amount when rounded to cents.
- **Strict external formats:** request and upstream publication dates must
  round-trip to `YYYY-MM-DD`; currency codes use a full three-letter match so
  control characters cannot pass validation.
- **Timeout classification:** map every `httpx.TimeoutException`, including
  `ConnectTimeout`, to `upstream_timeout` (504). Actual network failures remain
  `upstream_unavailable` (502), so callers can distinguish retryable timeouts.
- **Invalid upstream configuration:** malformed `FX_UPSTREAM_BASE` request
  errors are mapped to `upstream_unavailable` (502) instead of leaking as an
  `internal_error`.

## Hardening after the security review

- **Bounded `amount` exponent:** `amount` is echoed back in positional
  notation, so an unbounded negative exponent was a response-size amplifier —
  `1E-100000000` is positive, finite and far below `MAX_AMOUNT`, and rendered a
  hundred megabytes from a seventeen-byte query. Bounded at ten decimal places,
  which is the precision the contract already documented.
- **Bounded rate exponent:** the same amplifier existed on the way back and was
  missed the first time. `rate` is echoed in positional notation too, and the
  parser bounded it from above (a rate whose product with `MAX_AMOUNT` overflows
  is rejected) but not from below, so `1e-100000000` — thirteen bytes inside a
  body that passes the 1 MB cap — rendered a hundred million characters. The
  bound is the mirror of the existing one rather than a new constant: a rate
  that cannot move `MAX_AMOUNT` off `0.00` is refused, which costs nothing real
  and needs no new error code.
- **Bounded upstream body:** the response is streamed and abandoned past 1 MB.
  httpx imposes no limit of its own, so previously any body size was buffered
  faithfully. The cap is applied to decoded bytes, so a compressed body that
  expands past the limit stops at the limit.
- **One deadline for the whole upstream call:** httpx's read timeout applies
  per socket read, which an upstream trickling one byte at a time satisfies
  forever. An `asyncio.timeout` of 8 s is what actually bounds the exchange.
- **The deadline starts before the concurrency slot, not after:** it was nested
  inside the 8-call ceiling, so time spent queueing for a slot did not count
  against it. Measured with a 0.5 s deadline and 24 concurrent keys, the
  slowest call took 1.36 s and reported no timeout at all — the bound covered
  the exchange but not the wait for permission to begin it. Swapping the two
  makes the documented ceiling true, and turns a silent overrun into an honest
  `upstream_timeout` under sustained load.
- **A broken upstream is never our bug:** the JSON parse now also catches
  `RecursionError` (deeply nested body) and plain `ValueError` (CPython's
  integer-string digit limit, which a long unquoted number trips). Both used to
  escape as `internal_error` (500), which tells the calling model to stop rather
  than to retry — the opposite of the truth.
- **One reading of the clock per request:** `today_in_ecb_tz()` was sampled
  independently by the endpoint, the upstream client and the TTL calculation. A
  request straddling Berlin midnight had the publication-date bound and the
  staleness check working from different days. The endpoint now reads it once
  and threads it down.
- **Negative cache for the currency catalogue:** failing open on an unavailable
  catalogue is right, but only if it fails open *fast*. The failure is
  remembered for 60 seconds rather than re-attempted, and its connect timeout
  re-paid, on every conversion.
- **Single-flight:** concurrent misses on the same key share one upstream call
  instead of each opening their own — worst exactly at a cold start or a TTL
  expiry, when traffic is heaviest.
- **Ceilings on abuse:** the rate cache holds 512 entries, so a caller walking
  more distinct keys than that made this service a free amplifier pointed at
  the ECB feed, with the resulting throttling landing on our address. A
  per-client token bucket (60/minute, `FX_RATE_LIMIT_PER_MINUTE=0` to disable)
  and a ceiling of 8 concurrent upstream calls bound it. `rate_limited` is a
  new 429 code, and the only 4xx the caller should retry unchanged.

## With another day

- Add limited retries with jitter for transient timeouts and 5xx responses.
- Use a smarter intraday TTL for `latest`, based on the ECB publication window.
- Add structured per-request logs and an `X-Request-Id`.
- Refresh the currency catalogue in the background instead of only on demand.
- Add Hypothesis tests for Decimal arithmetic boundaries.

## AI tools

I planned this case through the **Traycer** harness, using **Opus 5** as the
planning model. I have been testing Traycer recently because its artifacts keep
technical decisions and tickets visible across planning and implementation. It
helped me use AI models more deliberately: I could compare each change with an
approved plan and stay aware of what the repository contains instead of
evaluating generated code only in the moment.

I also checked the real Frankfurter API before accepting the plan. I treated its
observed URL, 404, and weekend behaviour as evidence rather than copying an
unverified suggestion.

## One thing the AI got wrong

The first upstream URL proposal used `{FX_UPSTREAM_BASE}/{path}` and omitted
`/v1`. I noticed this by making a real request: the documented host's prefixless
`/latest` endpoint returned 404, while `/v1/latest` returned the rate. I moved
the prefix into the URL builder, documented the assumption, and made the fake
upstream tests use the same path shape.
