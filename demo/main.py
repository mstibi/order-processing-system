"""Demo dashboard: serves index.html plus demo-only endpoints that reuse scripts/race.py, replay.py and chaos.py.

Bound to 127.0.0.1:8080 in docker-compose. Every demo creates its own products named 'demo-...' and, before
running again, deletes only those products and their orders, so it never touches other data.
One demo runs at a time (409 otherwise).
"""
import asyncio
import collections
import contextlib
import os
import pathlib
import sys
import time

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chaos  # noqa: E402
import check_invariants  # noqa: E402
import race  # noqa: E402
import replay  # noqa: E402

API_URL = os.environ.get("API_URL", "http://localhost:8000")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://localhost:9000")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:5432/shop")
SERVICE = {httpx.URL(API_URL).host: "api", httpx.URL(PAYMENTS_URL).host: "payments"}

app = FastAPI(title="order-processing demo", docs_url=None, redoc_url=None)
lock = asyncio.Lock()
last_payments_run: dict = {}  # {"initial": {product_id: stock}} of the latest "When payments fail" run


@app.exception_handler(httpx.TransportError)
async def service_down(request, exc: httpx.TransportError):
    name = SERVICE.get(exc.request.url.host, exc.request.url.host)
    return JSONResponse({"detail": f"The {name} service is not answering ({type(exc).__name__}). "
                                   "Start the stack with: docker compose up -d"}, status_code=503)


@app.exception_handler(psycopg.OperationalError)
async def db_down(request, exc):
    return JSONResponse({"detail": "The database is not answering. Start the stack with: docker compose up -d"},
                        status_code=503)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(pathlib.Path(__file__).with_name("index.html"))


@app.get("/demo/status")
async def status():
    async def up(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                return (await c.get(url)).status_code == 200
        except httpx.HTTPError:
            return False

    async def db_up() -> bool:
        try:
            async with await psycopg.AsyncConnection.connect(DATABASE_URL, connect_timeout=2) as c:
                await c.execute("SELECT 1")
            return True
        except psycopg.Error:
            return False

    api, payments, database = await asyncio.gather(up(f"{API_URL}/health"), up(f"{PAYMENTS_URL}/health"), db_up())
    return {"api": api, "payments": payments, "database": database}


@contextlib.asynccontextmanager
async def alone():
    """One demo at a time: two demos at once would fight over the same DB connections and payment rates."""
    if lock.locked():
        raise HTTPException(409, "Another demo is still running. Wait for it to finish.")
    async with lock:
        yield


def delete_demo_data(prefix: str) -> None:
    """Deletes products named prefix* with their orders, payments and idempotency keys."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as db, db.transaction():
        pids = [r[0] for r in db.execute("SELECT id FROM products WHERE name LIKE %s", [prefix + "%"])]
        oids = [r[0] for r in db.execute(
            "SELECT DISTINCT order_id FROM order_items WHERE product_id = ANY(%s)", [pids])]
        db.execute("DELETE FROM idempotency_keys WHERE (response_json->'body'->>'id')::bigint = ANY(%s)"
                   " OR (response_json->'body'->>'product_id')::bigint = ANY(%s)", [oids, pids])
        db.execute("DELETE FROM order_items WHERE order_id = ANY(%s)", [oids])
        db.execute("DELETE FROM payments WHERE order_id = ANY(%s)", [oids])
        db.execute("DELETE FROM orders WHERE id = ANY(%s)", [oids])
        db.execute("DELETE FROM products WHERE id = ANY(%s)", [pids])


@app.post("/demo/oversell")
async def oversell(mode: str = Query(pattern="^(naive|atomic|for_update)$"),
                   buyers: int = Query(1000, ge=1, le=5000), stock: int = Query(100, ge=1, le=5000)):
    """scripts/race.py: `buyers` buy 1 item each at the same moment; race.py deletes its own product after."""
    async with alone():
        psycopg.connect(DATABASE_URL, connect_timeout=3).close()  # fail fast: the race pool would wait 30 s
        r = await race.run(mode, buyers, stock, 50)
    return {**r, "time": round(r["time"], 2), "ok": r["oversold"] == 0 and r["lost_updates"] == 0}


@app.post("/demo/replay")
async def same_click(repeats: int = Query(50, ge=2, le=500)):
    """scripts/replay.py: the same order `repeats` times with one Idempotency-Key, then once with another body."""
    async with alone():
        delete_demo_data("demo-replay")
        r = await replay.run(repeats, "demo-replay")
    return {**r, "ok": r["orders"] == 1 and r["charges"] == 1 and r["other_body_status"] == 422}


@app.post("/demo/payments")
async def payments_fail(orders: int = Query(200, ge=1, le=2000), fail_rate: float = Query(0.2, ge=0, le=1),
                        timeout_rate: float = Query(0.1, ge=0, le=1)):
    """scripts/chaos.py without the wipe: own products, faulty provider, a client that retries with the same key."""
    if fail_rate + timeout_rate > 1:
        raise HTTPException(422, "fail_rate + timeout_rate must be at most 1")
    async with alone():
        delete_demo_data("demo-payments")
        stock = max(1, orders // 3)  # 5 products x orders/3 < orders x avg qty 2, so a few orders find it sold out
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            pids = [db.execute("INSERT INTO products (name, price_cents, stock) VALUES (%s, %s, %s) RETURNING id",
                               [f"demo-payments-{i}", 500 + 100 * i, stock]).fetchone()[0] for i in range(5)]
        last_payments_run["initial"] = {p: stock for p in pids}

        async with httpx.AsyncClient(base_url=API_URL, timeout=15, limits=httpx.Limits(max_connections=50)) as http:
            old = (await http.post(f"{PAYMENTS_URL}/config", json={"FAIL_RATE": fail_rate, "TIMEOUT_RATE": timeout_rate})).json()
            sem = asyncio.Semaphore(50)

            async def one(i: int) -> str:
                async with sem:
                    return await chaos.one_order(http, i, pids)

            start = time.perf_counter()
            try:
                outcomes = await asyncio.gather(*(one(i) for i in range(orders)))
            finally:
                await http.post(f"{PAYMENTS_URL}/config", json=old)  # leave the provider as we found it
        return {"orders": orders, "seconds": round(time.perf_counter() - start, 1),
                "client_saw": dict(collections.Counter(outcomes).most_common()), **settle_state()}


@app.get("/demo/payments")
def payments_state():
    """Polled by the page: order statuses + invariants of the latest run, until the reconciler has settled."""
    if not last_payments_run:
        raise HTTPException(404, "No payments demo has run yet.")
    return settle_state()


def settle_state() -> dict:
    initial = last_payments_run["initial"]
    pids = list(initial)
    with psycopg.connect(DATABASE_URL, autocommit=True) as db:
        stock = dict(db.execute("SELECT id, stock FROM products WHERE id = ANY(%s)", [pids]).fetchall())
        sold = dict(db.execute(
            "SELECT oi.product_id, sum(oi.qty) FROM order_items oi JOIN orders o ON o.id = oi.order_id"
            " WHERE oi.product_id = ANY(%s) AND o.status NOT IN ('FAILED', 'CANCELLED') GROUP BY oi.product_id",
            [pids]).fetchall())
        orders = dict(db.execute(
            "SELECT DISTINCT o.id, o.status FROM orders o JOIN order_items oi ON oi.order_id = o.id"
            " WHERE oi.product_id = ANY(%s)", [pids]).fetchall())
    charges = [c for c in httpx.get(f"{PAYMENTS_URL}/charges").json() if c["order_id"] in orders]
    if len(stock) < len(initial):  # someone deleted the products meanwhile (another run): nothing to check
        raise HTTPException(409, "This run's data was replaced by a newer run.")
    checks = check_invariants.check(initial, stock, sold, orders, charges)
    return {"statuses": dict(collections.Counter(orders.values())), "checks": checks,
            "settled": checks["unknown"] == 0, "ok": not any(checks.values())}


@app.get("/demo/orders")
def recent_orders(limit: int = Query(12, ge=1, le=100)):
    with psycopg.connect(DATABASE_URL, autocommit=True) as db:
        rows = db.execute(
            "SELECT o.id, o.status, o.total_cents, o.created_at,"
            " string_agg(p.name || ' x' || oi.qty, ', ' ORDER BY p.id)"
            " FROM orders o JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id"
            " GROUP BY o.id ORDER BY o.id DESC LIMIT %s", [limit]).fetchall()
    return [{"id": i, "status": s, "total_cents": t, "created_at": c.isoformat(), "items": it} for i, s, t, c, it in rows]
