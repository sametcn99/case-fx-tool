# fx-tool

Small FastAPI service for converting currencies with European Central Bank
reference rates. It never invents a rate and reports the date the returned rate
actually belongs to.

## Run

```bash
./run.sh
```

The service listens on port `8080` by default. Set `PORT` to change it and
`FX_UPSTREAM_BASE` to point at another upstream (the default is
`https://api.frankfurter.dev`). For example:

```bash
PORT=9000 FX_UPSTREAM_BASE=http://127.0.0.1:9001 ./run.sh
```

Try a conversion:

```bash
curl "http://127.0.0.1:8080/tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28"
```

## Test

```bash
./test.sh
```

The suite fakes the upstream and needs no internet connection. The same suite
also passes when `FX_UPSTREAM_BASE` points at a closed local port:

```bash
FX_UPSTREAM_BASE=http://127.0.0.1:1 ./test.sh
```

## Endpoint

`GET /tools/convert`

| Parameter | Required | Behaviour                                                    |
| --------- | -------- | ------------------------------------------------------------ |
| `amount`  | yes      | Positive, finite decimal; maximum `1e12`.                    |
| `from`    | yes      | Three-letter currency code, case-insensitive.                |
| `to`      | yes      | Three-letter currency code, case-insensitive.                |
| `date`    | no       | Strict `YYYY-MM-DD`; omitted means today in `Europe/Berlin`. |

Successful responses contain `amount`, `from`, `to`, `rate`, `result`,
`rate_date`, `asked_date`, and `source`. Amounts and rates stay decimal-safe;
only `result` is rounded to two decimal places with `ROUND_HALF_UP`.

`asked_date` is the date the caller requested. `rate_date` is the publication
date returned by the ECB upstream for the rate we actually used. On a weekend
or holiday, Frankfurter may return the previous published rate; the two fields
then differ. The calling model must make that difference clear to the customer
instead of presenting the rate as belonging to `asked_date`.

## Error codes

Every error is returned as `{ "error": "<code>", "message": "<readable sentence>" }`.

| Code                        | HTTP | When                                                          |
| --------------------------- | ---: | ------------------------------------------------------------- |
| `invalid_request`           |  400 | The request shape is not understood.                          |
| `invalid_amount`            |  400 | Missing, non-numeric, non-finite, non-positive, or too large. |
| `invalid_currency_code`     |  400 | `from` or `to` is not three alphabetic letters.               |
| `unknown_currency`          |  400 | The code is shaped correctly but is not in the ECB catalogue. |
| `same_currency`             |  400 | `from` and `to` are equal.                                    |
| `invalid_date`              |  400 | The date is not strict `YYYY-MM-DD`.                          |
| `date_in_future`            |  400 | The requested date is after today in `Europe/Berlin`.         |
| `date_before_series_start`  |  400 | The date is before `1999-01-04`.                              |
| `rate_unavailable`          |  404 | The upstream has no rate for the requested pair/date.         |
| `rate_too_stale`            |  404 | The available rate is more than 7 days older than requested.  |
| `upstream_timeout`          |  504 | The upstream did not respond within the timeout.              |
| `upstream_unavailable`      |  502 | The upstream could not be reached.                            |
| `upstream_error`            |  502 | The upstream returned an unexpected or 5xx status.            |
| `upstream_invalid_response` |  502 | The body is not valid JSON or lacks usable rate fields.       |
| `internal_error`            |  500 | An unexpected error occurred inside this service.             |

## Case decisions

| Situation                              | Behaviour                                                                                   |
| -------------------------------------- | ------------------------------------------------------------------------------------------- |
| Weekend or holiday                     | Use the previous published rate when it is at most 7 days old; return its real `rate_date`. |
| Rate more than 7 days old              | Return `rate_too_stale`; do not present a stale number.                                     |
| Future date                            | Reject locally with `date_in_future`.                                                       |
| Before `1999-01-04`                    | Reject locally with `date_before_series_start`.                                             |
| Unknown currency                       | Reject with `unknown_currency` when the ECB catalogue identifies it.                        |
| Same currency                          | Reject with `same_currency`; no artificial `1.0` rate is created.                           |
| Slow, unavailable, or failing upstream | Return `upstream_timeout`, `upstream_unavailable`, or `upstream_error`.                     |
| Non-JSON or malformed upstream body    | Return `upstream_invalid_response`.                                                         |
| Ten decimal places in `amount`         | Accept and calculate with `Decimal`; round only the final result.                           |

## Cache and upstream URL

Successful rates are cached by `(from, to, requested date)`; `latest` is a
separate key from every explicit date. Historical entries live for 24 hours,
current/latest entries for 5 minutes, and the cache is a bounded LRU. Errors
are never cached.

Requests are sent to `{FX_UPSTREAM_BASE}/v1/...`. The real Frankfurter API
requires the `/v1` prefix even though the documented base URL does not include
it; a fake upstream used for review should expose the same paths.
