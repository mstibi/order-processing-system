import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import BaseModel, Field

from . import orders, payments, reconciler

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:5432/shop")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://localhost:9000")
PAYMENTS_TIMEOUT_S = float(os.environ.get("PAYMENTS_TIMEOUT_S", 3))
RECONCILE_INTERVAL_S = float(os.environ.get("RECONCILE_INTERVAL_S", 5))
PENDING_STALE_S = float(os.environ.get("PENDING_STALE_S", 60))

# shortcut: one uvicorn worker -> one pool of max 20 connections (Postgres allows 100);
# more workers = workers * max_size connections, keep that under ~90.
pool = AsyncConnectionPool(DATABASE_URL, min_size=10, max_size=20, timeout=10, open=False,
                           kwargs={"autocommit": True})
http = httpx.AsyncClient(base_url=PAYMENTS_URL, timeout=PAYMENTS_TIMEOUT_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open(wait=True)
    task = asyncio.create_task(reconciler.run_forever(pool, http, RECONCILE_INTERVAL_S, PENDING_STALE_S))
    yield
    task.cancel()
    await pool.close()


app = FastAPI(title="order-processing-system", lifespan=lifespan)


@app.exception_handler(PoolTimeout)
async def overloaded(request, exc):
    # every DB connection busy for 10 s: tell the client to come back instead of hanging
    return JSONResponse({"error": "overloaded"}, status_code=503, headers={"Retry-After": "1"})


class Item(BaseModel):
    product_id: int
    qty: int = Field(gt=0, le=1000)


class OrderIn(BaseModel):
    customer_id: int
    items: list[Item] = Field(min_length=1, max_length=50)


@app.get("/health")
async def health():
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    async with pool.connection() as conn:
        order = await orders.get(conn, order_id)
    if order is None:
        raise HTTPException(404, "order not found")
    return order


@app.post("/orders")
async def create_order(body: OrderIn, idempotency_key: str = Header(min_length=1, max_length=200)):
    items: dict[int, int] = {}
    for it in body.items:  # same product twice -> one line
        items[it.product_id] = items.get(it.product_id, 0) + it.qty
    # fingerprint of what is bought, independent of item order in the JSON
    request_hash = hashlib.sha256(json.dumps([body.customer_id, sorted(items.items())]).encode()).hexdigest()

    # 1. claim the key. Only one request can insert it; everyone else reads what is there.
    async with pool.connection() as conn:
        claimed = await (await conn.execute(
            "INSERT INTO idempotency_keys (key, request_hash, status) VALUES (%s, %s, 'PROCESSING')"
            " ON CONFLICT (key) DO NOTHING RETURNING key", [idempotency_key, request_hash])).fetchone()
        if not claimed:
            hash_, status, saved = await (await conn.execute(
                "SELECT request_hash, status, response_json FROM idempotency_keys WHERE key = %s",
                [idempotency_key])).fetchone()
    if not claimed:
        if hash_ != request_hash:
            return JSONResponse({"error": "idempotency_key_reused_with_different_body"}, status_code=422)
        if status == "PROCESSING":
            return JSONResponse({"error": "request_in_progress"}, status_code=409)
        return JSONResponse(saved["body"], status_code=saved["status_code"])  # replay the first answer

    status_code, response = await place_order(body.customer_id, items, idempotency_key)

    # 6. save the answer, so a retry with this key gets exactly the same response
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE idempotency_keys SET status = 'COMPLETED', response_json = %s WHERE key = %s",
            [json.dumps({"status_code": status_code, "body": response}), idempotency_key])
    return JSONResponse(response, status_code=status_code)


async def place_order(customer_id: int, items: dict[int, int], idempotency_key: str) -> tuple[int, dict]:
    # 2. stock + PENDING order, one short transaction
    try:
        async with pool.connection() as conn:
            order_id, total = await orders.reserve(conn, customer_id, items)
    except orders.OutOfStock as e:
        return 409, {"error": "out_of_stock", "product_id": e.args[0]}
    except Exception:
        # nothing was committed, so free the key: the client may simply retry
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM idempotency_keys WHERE key = %s", [idempotency_key])
        raise

    # 3. charge OUTSIDE the transaction and without holding a DB connection (it can take seconds)
    outcome = await payments.charge(http, order_id, total)

    # 4. record the outcome. If we crash before this line, the reconciler finds the PENDING order.
    async with pool.connection() as conn:
        await orders.count_attempt(conn, order_id)
        if outcome == "succeeded":
            await orders.mark_paid(conn, order_id)
        elif outcome == "declined":
            await orders.cancel(conn, order_id, "FAILED", "declined")
        else:
            await orders.mark_unknown(conn, order_id)
        order = await orders.get(conn, order_id)
    return {"PAID": 201, "FAILED": 402}.get(order["status"], 202), order
