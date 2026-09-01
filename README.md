# fx-tool

Small FastAPI service for converting currencies with European Central Bank
reference rates. It never invents a rate and reports the date the returned rate
actually belongs to.

## Run

The scripts use Bash. On every platform, the first run creates `.venv` and
installs the pinned dependencies from `requirements.txt`. The service listens
on port `8080` by default; `PORT` and `FX_UPSTREAM_BASE` are optional.

<details>
<summary>Windows (Git Bash or WSL)</summary>

### Git Bash — recommended

1. Install [Git for Windows](https://git-scm.com/download/win) if Git Bash is
   not already available.
2. Open **Git Bash**, go to the repository, and start the service:

   ```bash
   cd /c/Users/<your-user>/Documents/projects/github/active/sametcn99/case-fx-tool
   ./run.sh
   ```

3. In another Git Bash window, check that it is running:

   ```bash
   curl http://127.0.0.1:8080/health
   ```

   Expected response: `{"ok":true}`.

4. Use a different port or upstream when needed:

   ```bash
   PORT=9000 FX_UPSTREAM_BASE=http://127.0.0.1:9001 ./run.sh
   ```

The first run may take longer while `.venv` is created. Stop the server with
`Ctrl-C`; the next run reuses the environment.

### WSL

Install the virtual-environment package once on Debian/Ubuntu:

```bash
sudo apt update
sudo apt install python3 python3-venv
```

Then run it from the Linux-mounted repository path:

```bash
cd /mnt/c/Users/<your-user>/Documents/projects/github/active/sametcn99/case-fx-tool
./run.sh
```

Windows and WSL Python environments are different. If the existing `.venv` was
created by Windows Python, the bootstrap script detects it and rebuilds it for
WSL automatically. The first install under `/mnt/c` can be slower because the
repository is on the Windows filesystem.

### Command Prompt or PowerShell

`./run.sh` is Bash syntax and will not run directly in `cmd.exe`. Start it via
Git Bash instead. From PowerShell, for example:

```powershell
& "C:\Program Files\Git\bin\bash.exe" -lc "cd '/c/Users/<your-user>/Documents/projects/github/active/sametcn99/case-fx-tool' && ./run.sh"
```

</details>

<details>
<summary>macOS (Terminal)</summary>

1. Open Terminal and verify that Python 3 is available:

   ```bash
   python3 --version
   ```

2. Go to the repository and start the service:

   ```bash
   cd /path/to/case-fx-tool
   ./run.sh
   ```

3. Verify it from another Terminal tab:

   ```bash
   curl http://127.0.0.1:8080/health
   ```

4. Override the port or upstream with inline environment variables:

   ```bash
   PORT=9000 FX_UPSTREAM_BASE=http://127.0.0.1:9001 ./run.sh
   ```

macOS uses Bash/Zsh for Terminal, so the scripts can be run directly. The
bootstrap script creates `.venv` on the first run; stop Uvicorn with `Ctrl-C`.

</details>

<details>
<summary>Linux (Bash)</summary>

On Debian/Ubuntu, install Python and the venv module once:

```bash
sudo apt update
sudo apt install python3 python3-venv
```

From the repository directory, start the service:

```bash
cd /path/to/case-fx-tool
./run.sh
```

Verify it from another terminal:

```bash
curl http://127.0.0.1:8080/health
```

To change configuration for one run:

```bash
PORT=9000 FX_UPSTREAM_BASE=http://127.0.0.1:9001 ./run.sh
```

On Fedora/RHEL, make sure the installed Python package includes the standard
library `venv` module; the exact package name varies by release. Stop the
service with `Ctrl-C`.

</details>

Try a conversion after the health check succeeds:

```bash
curl "http://127.0.0.1:8080/tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28"
```

## Test

The test script uses the same Bash/bootstrap setup as `run.sh`. Normal cases
fake the upstream with `httpx.MockTransport`, so they do not require the public
Frankfurter API. The suite also includes a closed-port check for the
unavailable-upstream error mapping.

<details>
<summary>Windows (Git Bash or WSL)</summary>

### Git Bash

Open Git Bash in the repository and run:

```bash
cd /c/Users/<your-user>/Documents/projects/github/active/sametcn99/case-fx-tool
./test.sh
```

To reproduce the evaluator's no-network setup:

```bash
FX_UPSTREAM_BASE=http://127.0.0.1:1 ./test.sh
```

At the time of this review, the suite ends with `92 passed`. A known
Starlette/httpx deprecation warning may also be printed; it does not fail the
tests. Do not start `run.sh` first: the tests use an in-process fake upstream.

### WSL

From WSL, use the Linux-mounted path and the WSL-created `.venv`:

```bash
cd /mnt/c/Users/<your-user>/Documents/projects/github/active/sametcn99/case-fx-tool
./test.sh
FX_UPSTREAM_BASE=http://127.0.0.1:1 ./test.sh
```

If the Windows `.venv` is present, `bootstrap.sh` detects the platform mismatch
and rebuilds it. Install venv support once if `python3 -m venv` is unavailable:

```bash
sudo apt update
sudo apt install python3 python3-venv
```

### Command Prompt or PowerShell

`./test.sh` is Bash syntax and will not run directly in `cmd.exe`. Run it
through Git Bash from PowerShell:

```powershell
& "C:\Program Files\Git\bin\bash.exe" -lc "cd '/c/Users/<your-user>/Documents/projects/github/active/sametcn99/case-fx-tool' && FX_UPSTREAM_BASE=http://127.0.0.1:1 ./test.sh"
```

</details>

<details>
<summary>macOS (Terminal)</summary>

Check Python 3 and run from the repository:

```bash
cd /path/to/case-fx-tool
python3 --version
./test.sh
```

Run the same fully offline/evaluator-like test:

```bash
FX_UPSTREAM_BASE=http://127.0.0.1:1 ./test.sh
```

The script creates or reuses `.venv`; no service process or internet connection
is needed. The suite should end with `92 passed`; the existing deprecation
warning may appear.

</details>

<details>
<summary>Linux (Bash)</summary>

On Debian/Ubuntu, install venv support once if needed:

```bash
sudo apt update
sudo apt install python3 python3-venv
```

Then run:

```bash
cd /path/to/case-fx-tool
./test.sh
```

For a fully offline/evaluator-like run:

```bash
FX_UPSTREAM_BASE=http://127.0.0.1:1 ./test.sh
```

No public network is used; `127.0.0.1:1` is a closed local port. On Fedora or
RHEL, ensure the installed Python includes the standard-library `venv` module.

</details>

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
