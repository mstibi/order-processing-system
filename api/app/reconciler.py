"""Fixes orders whose payment result we do not know, by asking the provider.

Picks up:
- PAYMENT_UNKNOWN: our charge call timed out / broke.
- PENDING older than PENDING_STALE_S: the API died between COMMIT and the charge call
  (or before it could save the result). A healthy request finishes in a few seconds.
"""
import asyncio
import logging

import httpx
from psycopg_pool import AsyncConnectionPool

from . import orders, payments

log = logging.getLogger("reconciler")


async def reconcile_once(pool: AsyncConnectionPool, http: httpx.AsyncClient, stale_s: float) -> int:
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT id, status FROM orders WHERE status = 'PAYMENT_UNKNOWN'"
            " OR (status = 'PENDING' AND created_at < now() - make_interval(secs => %s))"
            " ORDER BY id LIMIT 500", [stale_s])).fetchall()
    fixed = 0
    for order_id, status in rows:
        result = await payments.lookup(http, order_id)  # no DB connection held while we wait on the network
        if result is None:
            continue  # provider unreachable, next round
        async with pool.connection() as conn:
            if result == "succeeded":
                await orders.mark_paid(conn, order_id)
            elif result == "declined":
                await orders.cancel(conn, order_id, "FAILED", "declined")
            else:  # provider never saw a charge: nobody paid, give the stock back
                await orders.cancel(conn, order_id, "CANCELLED" if status == "PENDING" else "FAILED", "not_found")
        fixed += 1
    return fixed


async def run_forever(pool: AsyncConnectionPool, http: httpx.AsyncClient, interval_s: float, stale_s: float) -> None:
    while True:
        try:
            n = await reconcile_once(pool, http, stale_s)
            if n:
                log.info("reconciled %d orders", n)
        except Exception:
            log.exception("reconcile round failed")  # keep the loop alive, try again next round
        await asyncio.sleep(interval_s)
