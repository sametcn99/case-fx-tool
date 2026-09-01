# Review of tool.py

One page. Findings **ranked** — most harmful to a customer first.

For each finding: what is wrong, what it does to a customer (not to a linter),
and how you would verify it.

## 1. The cache is keyed only by currency pair, so it answers the wrong date — and never expires

`fetch_rate` builds `key = f"{base}-{target}"` (line 28) and drops `on` from the key.
The first EUR→TRY request stores that rate for the lifetime of the process; every later
EUR→TRY request returns it (line 30) regardless of the date asked for, regardless of age.
Worse, the cached value is re-stamped with `str(on or date.today())` (line 30), so the
response asserts the rate belongs to a date it was never fetched for.

**Customer impact.** Two customers ask the same pair for different dates and get the same
number, one of them silently wrong. A support agent looking up 2015 pricing poisons the
cache for everyone asking for today. Nothing is logged, no status code changes, and the
`rate_date` field actively confirms the wrong answer — so this is undetectable from the
outside until a customer disputes an invoice. Because entries never expire, a long-running
process serves last week's rate as today's forever.

**How I would verify it.** Point the service at a fake upstream that returns a distinct
sentinel rate per date. Call `?amount=1&to=TRY&on=2015-01-05`, then call the same pair with
no `on`, and assert the second response's `rate` differs from the first. Reverse the order
and assert the same. Second check: run the pair twice against the fake and assert the
transport saw two requests, not one — proving staleness has no time bound.

## 2. Every failure is swallowed and returned as `HTTP 200` with `rate: 0.0`

The `except Exception` in `convert` (lines 71–81) catches network errors, upstream 5xx,
unknown currency codes, malformed bodies — everything — `print`s to stdout, and returns a
success-shaped body with `rate: 0.0`, `result: 0.0`, and `"source": "ECB via frankfurter.dev"`.
Nothing upstream is checked either: `response.json()` (line 34) is parsed with no status
check, so an error body flows into `payload["rates"][target]` and lands in the same handler.

**Customer impact.** The agent has no way to know the call failed, so it tells the customer
"250 EUR is 0.00 TRY", attributed to the ECB. That is worse than an error: an error gets
retried or surfaced, a confident zero gets acted on. Operationally it is invisible too —
`print` goes nowhere structured, and the 200s keep every dashboard green during a full
upstream outage.

**How I would verify it.** Set the upstream to a closed port (or pull the network) and call
`/tools/convert?amount=250&to=TRY`; assert the status is 200 and the body says `0.0`. Same
with a nonsense code (`to=ZZZ`) and with a fake upstream returning 500 or `not json`. Any
one of them returning 200 confirms it.

## 3. `round(rate, 2)` destroys the rate before using it

Line 60 rounds the *rate itself* to two decimals, then multiplies. That is tolerable for a rate in the
tens, like EUR→TRY, and catastrophic for any pair whose rate is small: JPY→USD ≈ 0.0064 becomes 0.01, a
~56% overcharge; a rate below 0.005 becomes `0.0`, which is finding #2's zero without the
failure. `float` money and `round`'s banker's rounding add a cent of drift on top.

**Customer impact.** Wrong invoices for a whole class of currency pairs — plausible enough
to pass review, large enough to matter. It is silent and deterministic, so it ships.

**How I would verify it.** Table test against a fake upstream: feed rates `0.0064`, `0.004`,
`1.23456`, assert `result` equals `amount × unrounded_rate` to two places. The first
overshoots by more than half, the second returns zero.

## Other findings, lower rank

- **`from_` is the actual query parameter name** (line 48). `?from=USD` is ignored and the
  default `EUR` is used silently — a wrong-currency answer with no error. Mitigated for an
  agent that generates calls from the OpenAPI schema (it will send `from_`), which is why it
  is here and not in the top three; still wrong for the documented `?from=` curl.
- **The weekend fallback goes to `/latest`, not to the last publication before the requested
  date** (lines 39–40). Ask for a historical Sunday and you get *today's* rate labelled with
  that Sunday. It also fires for unknown currencies and error bodies, since the trigger is
  merely "target missing from `rates`". No staleness bound at all.
- **No input validation**: negative or `inf` amounts, `from == to`, non-ISO codes, and future
  dates all pass through. `on=2030-01-01` returns today's rate presented as a 2030 rate.
- **`_cache` is unbounded** — attacker-chosen pairs grow it without limit.

## The one I would fix before shipping tonight

Finding #2 — stop returning `0.0` with a 200. Not because it is the largest error (#1 is),
but because it is the one that makes every other error invisible. While failures are laundered
into successful-looking zeros, you cannot see the cache bug, the rounding bug, or an upstream
outage in any metric you have; the first signal is a customer complaint. It is also the
smallest change of the three: delete the `except` block's fabricated body, add
`response.raise_for_status()`, and let the failure become a 502/504 with a real message. That
turns tonight's unknown-unknowns into visible alerts by morning, and it does not require
rethinking the cache design under time pressure.

## Things that look suspicious but are fine

- **The module-level `httpx.AsyncClient()` with no timeout** (line 23) is not the unbounded
  hang it resembles: unlike `requests`, httpx defaults to a 5s timeout on all operations.
  Untuned, not missing. (Verify: assert `client.timeout` in a REPL.) Constructing it at
  import time before an event loop exists is also fine — httpx binds no loop at construction.
- **The unlocked global `_cache`** is not a data race. The event loop is single-threaded and
  the dict operations are atomic; the only consequence of the `await` between check and store
  is a duplicate upstream call on a concurrent miss. Wasteful, not corrupting.
- **`/health` never touches the upstream.** That is correct for a liveness probe — tying it
  to a third party makes your orchestrator restart healthy pods during an ECB outage.
- **`response.json()` is not a missing `await`.** On an already-read httpx response it is
  synchronous by design.

Being right about a non-issue is worth as much as finding a real defect.
