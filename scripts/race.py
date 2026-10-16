"""M3: many buyers race for the same product, straight against Postgres, using the API's own reserve().

    python scripts/race.py --mode naive --buyers 1000 --stock 100
    python scripts/race.py --mode atomic      (what the API uses)
    python scripts/race.py --mode for_update

Each buyer wants 1 item. Correct result: sold=100 stock=0 (oversold=0, lost_updates=0).
lost_updates = stock the DB still shows minus what should be left; >0 means sales were lost from the count. The run uses its own new product and
deletes its orders at the end, so it does not touch other data.
"""
import argparse
import asyncio
import os
import pathlib
import sys
import time

from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
from app.orders import TAKE, OutOfStock, reserve  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:5432/shop")


async def main(mode: str, buyers: int, stock: int, conns: int) -> None:
    # timeout=600: all buyers queue for `conns` connections at once; the 30 s default is too short
    # when every round trip goes through Docker Desktop's port forwarding
    async with AsyncConnectionPool(DATABASE_URL, min_size=conns, max_size=conns, timeout=600,
                                   kwargs={"autocommit": True}) as pool:
        async with pool.connection() as conn:
            product_id = (await (await conn.execute(
                "INSERT INTO products (name, price_cents, stock) VALUES (%s, 100, %s) RETURNING id",
                [f"race-{mode}", stock])).fetchone())[0]

        async def buyer(customer_id: int) -> bool:
            async with pool.connection() as conn:
                try:
                    await reserve(conn, customer_id, {product_id: 1}, mode)
                    return True
                except OutOfStock:
                    return False

        start = time.perf_counter()
        results = await asyncio.gather(*(buyer(i) for i in range(buyers)))  # all buyers at once
        seconds = time.perf_counter() - start

        async with pool.connection() as conn:
            stock_left = (await (await conn.execute("SELECT stock FROM products WHERE id = %s", [product_id])).fetchone())[0]
            sold = (await (await conn.execute(
                "SELECT coalesce(sum(qty), 0) FROM order_items WHERE product_id = %s", [product_id])).fetchone())[0]
            async with conn.transaction():  # clean up, so the reconciler never sees these PENDING orders
                rows = await (await conn.execute(
                    "DELETE FROM order_items WHERE product_id = %s RETURNING order_id", [product_id])).fetchall()
                ids = [r[0] for r in rows]
                await conn.execute("DELETE FROM payments WHERE order_id = ANY(%s)", [ids])
                await conn.execute("DELETE FROM orders WHERE id = ANY(%s)", [ids])
                await conn.execute("DELETE FROM products WHERE id = %s", [product_id])

    print(f"mode={mode} buyers={buyers} stock_before={stock} sold={sold} stock={stock_left} "
          f"oversold={max(0, sold - stock)} lost_updates={stock_left - (stock - sold)} ok_answers={sum(results)} time={seconds:.2f}s conns={conns}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=list(TAKE), required=True)
    p.add_argument("--buyers", type=int, default=1000)
    p.add_argument("--stock", type=int, default=100)
    p.add_argument("--conns", type=int, default=50, help="DB connections = how many buyers are really in Postgres at once")
    a = p.parse_args()
    if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(a.mode, a.buyers, a.stock, a.conns))
