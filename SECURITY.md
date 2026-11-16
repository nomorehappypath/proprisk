# Security

PropRisk is built for one person, on their own Mac, using their own API
keys. There is no sign-in, no second account, and no outside network
exposure by design. This document says plainly what is and is not
protected, and how to report a problem.

## The trust model

- **Users:** one person, on their own Mac. No other accounts exist, and
  PropRisk never asks anyone else to sign in.
- **Anyone with access to your Mac user account can read your keys.**
  The secret store (`data/proprisk_secrets.sqlite3`) is protected by normal
  macOS file permissions (0600 on the file, 0700 on its folder) — the same
  protection as any other file only you can read. It is not encrypted at
  rest, because the owner's direction was "no need for extra security"
  beyond keeping keys out of code, logs and the repository.
- **The local PostgreSQL cluster trusts local connections.** It listens
  only on `127.0.0.1` and a Unix socket inside `data/run/`, both reachable
  only from this Mac. Its own files hold the data; nothing is exposed to
  the network.

## What is protected, and how

- **Secrets never leave the one store.** Every API key and token lives
  only in `data/proprisk_secrets.sqlite3` (see `backend/app/core/secret_store.py`
  and SPEC.md chapter 1.6). Nothing is read from an environment variable
  or a `.env` file. A saved value is never returned in full by any API
  response or shown again on any screen — only its last 4 characters.
- **Nothing is logged.** Secret-store values, the `Authorization` and
  `Cookie` headers, and request bodies are scrubbed before anything is
  written to the error log (`backend/app/core/error_sink.py`) or any other
  log line.
- **Network exposure is local-only.** The server and the database both
  bind `127.0.0.1`. Because any web page open in the same browser could
  still try to call `127.0.0.1:8765`, the server refuses a request whose
  `Host` header does not name this machine, and refuses a state-changing
  request (create, update, delete) whose `Origin` header is present and
  names anywhere else. State-changing endpoints accept `application/json`
  bodies only.
- **Input is bounded.** Every value PropRisk accepts has a length, range
  or shape check before it is used: addresses (1–512 characters), parcel
  IDs (up to 64), key values (8–512 characters, no whitespace or control
  characters), key names (a fixed allow-list), the spending cap
  (0–1,000,000 cents), and browser error reports (16 KB). SQL is only ever
  built through SQLAlchemy or `sqlite3` parameters — never string
  concatenation. The launch and stop scripts quote every variable and
  never `eval` anything.
- **Costly calls are capped.** AI calls are metered and blocked once the
  monthly spending cap is reached (`backend/app/services/gateway.py`);
  there is no public endpoint to protect with a rate limit, since nothing
  is reachable from outside this Mac.
- **Errors reveal nothing internal.** Every error response is a plain
  sentence plus a short reference code — never a stack trace, a query, or
  a file path.
- **Dependencies are checked before delivery.** `uv run pip-audit` for the
  backend and `npm audit` for the screens are run once before each
  release; any high or critical finding is upgraded past, or named here
  with the reason it is accepted.

## What is not covered

- Someone else gaining access to your Mac user account — that is outside
  what an application running under that account can protect against.
- Hard crashes, out-of-memory conditions, and anything that happens before
  the error sink itself starts (see `docs/MAINTENANCE.md`).
- Multi-user separation — there is only one user.

## Reporting a problem

This is a personal, open-source project with no dedicated support plan.
If you find a security problem, please open an issue in the project's
repository describing the problem and how to reproduce it. Avoid posting
real API keys or other secrets in the report.
