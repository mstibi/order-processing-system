# order-processing-system

An order API that, under heavy concurrency and when things crash, **never charges twice and never sells more than it has in stock**, with the numbers to prove it.

FastAPI + PostgreSQL 16 + psycopg 3 (hand-written SQL, no ORM) + a fake Stripe (`fake-payments`) that fails, times out and remembers idempotency keys.

## Demo dashboard

`docker compose up -d`, then open **http://localhost:8080**. A single page (plain HTML/JS/CSS, no build, works offline) where anyone can click through what this project proves; every button runs the real scripts against the real stack.

| Demo | What you click | What you see (real runs, 2026-10-16) |
|---|---|---|
| 1. Overselling | 1000 buyers, 100 in stock: Naive / Atomic UPDATE / SELECT FOR UPDATE | naive sold **1000 of 100** (DB still showed 74-85 left); atomic and lock sold exactly 100 in 0.65-0.92 s |
| 2. Same click, many times | the same order 50x with one Idempotency-Key | 26x 201 + 24x 409 "still processing", **orders=1 charges=1**, same key with another body: 422 |
| 3. When payments fail | sliders for decline / timeout rate, 100-500 orders | 500 orders at 20% declined + 40% timeouts: 203 first got PAYMENT_UNKNOWN, after the reconciler 253 PAID / 247 FAILED, `duplicate_charges=0 oversell=0 unknown=0` |
| Live | recent orders table, refreshed every 3 s | status of each order as it changes |

<p>
<img src="docs/img/demo-oversell-desktop.jpg" alt="Demo 1 at desktop width: naive sold 1000 of 100, atomic and SELECT FOR UPDATE sold exactly 100" width="560">
<img src="docs/img/demo-payments-phone-dark.jpg" alt="Demo 3 at phone width in dark mode: order statuses and all invariant checks green" width="220">
</p>

The `demo` service (`demo/main.py`) reuses `scripts/race.py`, `replay.py`, `chaos.one_order` and `check_invariants.check`; it does not touch the order logic. Each demo creates and later deletes only its own `demo-*` products, one demo runs at a time, and all ports are bound to 127.0.0.1. If a service is down, its status pill turns red and the demo shows which service is not answering. Tests: `docker compose exec demo pytest -q demo`.

## Results

All numbers below are from real runs with `docker compose` (Docker Desktop 29.8.2, Postgres 16, one PC: Ryzen 5 7600X, 32 GB, Windows 11). Raw output is in `results/`. The earlier no-Docker numbers (`scripts/local_stack.py`) are kept in `results/local/` as a fallback.

### M3: 1000 buyers, 1 product (raw: `results/race.txt`, 5 runs per row, 50 DB connections)

Run inside the compose network (`docker compose run api python /w/scripts/race.py`, see Run it), so Docker Desktop's port forwarding does not dominate the timings:

| mode | stock | sold | stock left | oversold | lost updates | time (median, min-max) |
|---|---|---|---|---|---|---|
| `naive` (read in Python, write back) | 100 | **1000** | 71-80 | **900** | 971-980 | 3.25 s (2.92-3.68) |
| `atomic` (`UPDATE ... WHERE stock >= q`) | 100 | 100 | 0 | 0 | 0 | 0.81 s (0.68-0.87) |
| `for_update` (`SELECT ... FOR UPDATE`) | 100 | 100 | 0 | 0 | 0 | 0.84 s (0.72-0.98) |
| `naive` | 1000 | 1000 | 973-982 | 0 | 973-982 | 3.17 s (2.98-3.51) |
| `atomic` | 1000 | 1000 | 0 | 0 | 0 | 3.30 s (2.81-3.34) |
| `for_update` | 1000 | 1000 | 0 | 0 | 0 | 3.08 s (2.90-3.21) |

The naive bug sells **10x the stock** and the DB still says items are left. Both fixes are always exact. `atomic` vs `for_update`: the gap is inside the run-to-run noise here (about 4%); the extra round trip of `for_update` only matters when round trips are expensive.

The literal host command (`python scripts/race.py ...` from Windows to `localhost:5432`, raw: `results/race_host.txt`, 3 runs) gives the same correctness numbers but every round trip crosses Docker Desktop's port forwarder: naive 48.7 s, atomic 5.48 s, for_update 5.66 s. That is also why `race.py` now waits up to 600 s for a pool connection (psycopg's 30 s default timed out in naive mode).

### M4: same request 50x with one Idempotency-Key (raw: `results/replay.txt`, 3 runs)

25 sent at the same moment + 25 one by one: `orders=1 charges=1` in every run (26x 201 with an identical body, 24x 409 "request in progress"). Same key with another body: **422**.

### M5 / M6: 1000 orders, payment provider with `FAIL_RATE=0.2 TIMEOUT_RATE=0.1` (raw: `results/chaos_m5.txt`, `results/chaos_m6.txt`)

M6 kills the API container with `docker compose kill api && docker compose start api` (SIGKILL, a real crash) every 200 orders.

| run | API kills | client saw PAYMENT_UNKNOWN | client gave up | final PAID / FAILED / CANCELLED | duplicate_charges | oversell | unknown left |
|---|---|---|---|---|---|---|---|
| M5 #1 | 0 | 114 | 0 | 709 / 285 / 0 | 0 | 0 | 0 |
| M5 #2 | 0 | 97 | 0 | 678 / 277 / 0 | 0 | 0 | 0 |
| M5 #3 | 0 | 113 | 0 | 704 / 260 / 0 | 0 | 0 | 0 |
| M6 #1 | 4 (SIGKILL) | 34 | 67 | 685 / 222 / 28 | 0 | 0 | 0 |
| M6 #2 | 4 | 45 | 48 | 667 / 223 / 18 | 0 | 0 | 0 |
| M6 #3 | 4 | 29 | 61 | 683 / 230 / 31 | 0 | 0 | 0 |

Every PAID order has exactly one succeeded charge, and no other order has one. (Orders that got `out_of_stock` were never created, so the totals are below 1000.) `check_invariants.py` is itself tested against deliberately broken states (`scripts/test_check_invariants.py`).

### M7: load (k6 2.3.0 inside the compose network, raw: `results/load_runs.txt`, `results/load*.json`)

50 virtual users, 30 s, every order for the same product, payments without faults. 8 runs, 100% of requests got 201 PAID in every run:

| | rps | p50 | p99 |
|---|---|---|---|
| median of 8 runs | **129** | **357 ms** | **825 ms** |
| best run (`results/load.json`) | 185 | 252 ms | 557 ms |
| worst run | 103 | 442 ms | 1039 ms |

The limit is the design, not a bug: one uvicorn worker (84% CPU = one core during the run) and one hot row (10 Postgres sessions waiting on the product's row lock). A fresh DB gives the same ~130 rps as one with 15k orders, so the spread between runs is host noise (Docker Desktop VM on Windows), not data growth. More workers or a different product per user would raise it; that is out of v1 scope.

## How it works

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant DB as Postgres
    participant P as fake-payments
    C->>A: POST /orders (Idempotency-Key)
    A->>DB: INSERT key PROCESSING ON CONFLICT DO NOTHING
    alt key already there
        A-->>C: saved response / 409 in progress / 422 other body
    end
    A->>DB: TX: UPDATE stock WHERE stock >= q, INSERT order PENDING, COMMIT
    A->>P: POST /charges (Idempotency-Key = order_id), no DB connection held
    alt 201
        A->>DB: PAID
    else 402
        A->>DB: FAILED + restock
    else timeout / error
        A->>DB: PAYMENT_UNKNOWN
    end
    A->>DB: save response, key COMPLETED
    A-->>C: 201 / 402 / 202
    loop every 5 s
        A->>DB: PAYMENT_UNKNOWN + PENDING older than 60 s
        A->>P: GET /charges/{order_id}
        A->>DB: PAID, or FAILED/CANCELLED + restock
    end
```

## Run it

```bash
docker compose up -d                       # postgres + fake-payments + api on :8000 + demo on :8080
curl localhost:8000/health                 # {"status":"ok"}
docker compose exec api pytest             # API tests (they wipe the DB)
docker compose exec demo pytest -q demo    # demo endpoint tests (only touch demo-* data)

pip install -r scripts/requirements.txt    # scripts run on the host
python scripts/race.py --mode naive --buyers 1000 --stock 100      # also: atomic, for_update
# same inside the compose network (fair timings, used for the table):
docker compose run --rm --no-deps -v ./scripts:/w/scripts:ro -e PYTHONPATH=/app \
  -e DATABASE_URL=postgresql://app:app@postgres:5432/shop api python /w/scripts/race.py --mode atomic
python scripts/replay.py --repeats 50
FAIL_RATE=0.2 TIMEOUT_RATE=0.1 docker compose up -d
python scripts/chaos.py --orders 1000 [--kill-api-every 200]       # wipes the DB first
python scripts/check_invariants.py
docker compose exec -T postgres psql -U app -d shop -f - < load/seed.sql
docker compose --profile load run --rm k6                          # -> results/load.json (FAIL_RATE=0)
```

Fallback without Docker (Windows): `python -m venv .venv`, install `api/requirements.txt`, `fake-payments/requirements.txt` and `pgserver`, run `python scripts/local_stack.py`, and point the scripts at the printed `DATABASE_URL` plus `API_URL=http://127.0.0.1:8000 PAYMENTS_URL=http://127.0.0.1:9000`. For M6 use `--restart-cmd` with a command that kills the PID in `.api.pid` (the stack restarts it).

> Git Bash on Windows rewrites `/results/...`-style arguments: prefix such commands with `MSYS_NO_PATHCONV=1`.
>
> Status: every command above was run with Docker Desktop (results above). CI (`.github/workflows/ci.yml`) has not run yet: the repo is not on GitHub.

## Decisions

- **Atomic conditional UPDATE on READ COMMITTED**, not SERIALIZABLE: Postgres re-checks the `WHERE stock >= q` after waiting for the row lock, so it cannot oversell. SERIALIZABLE would turn 1000 buyers on one row into a storm of serialization errors and retries.
- **Payment call outside the transaction and without a pooled connection**: a 3 s timeout inside a transaction would hold the product's row lock and drain the 20-connection pool.
- **Timeout is PAYMENT_UNKNOWN, never FAILED**: the charge may have happened. The reconciler asks the provider later; the provider key is the order id, so nothing is charged twice.
- **Every status change is guarded** (`WHERE status IN (...)`), and stock is returned only by whoever won that change, so API and reconciler cannot both restock.
- **Locks taken in product-id order** to avoid deadlocks on multi-item orders.
- **Pool 10-20 connections, 503 + Retry-After when it is exhausted for 10 s**: Postgres allows 100 connections; more connections would not help a hot row anyway.
- **422 for a reused key with another body** (Stripe uses a 400-class error too); 409 while the first request is still running, like Stripe.
- **race.py talks to Postgres directly** using the API's own `reserve()` function: it isolates the locking part from HTTP noise.

## Known limits (where M6 stops)

- If the API dies while a key is PROCESSING, retries with that key get 409 until the client gives up (48-67 of 1000 clients per M6 run). Money and stock are still correct, the client just gets no answer. Fix: `locked_at` + takeover of stale keys.
- An order cut off by a crash before the charge is **cancelled** after 60 s instead of completed. An outbox + worker would finish it (bonus phase).
- The reconciler trusts a provider 404 as "never charged". True for fake-payments (it records synchronously); with a real provider, re-send the charge with the same key instead.
- fake-payments keeps charges in memory; the crash test kills only the API. Idempotency keys are never pruned (Stripe prunes after 24 h).
