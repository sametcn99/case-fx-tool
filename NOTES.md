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
