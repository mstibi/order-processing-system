"""M4: the same order sent many times with the same Idempotency-Key (impatient client / network retries).

    python scripts/replay.py --repeats 50

Half of the copies are sent at the same moment (the "double click"), the rest one by one afterwards.
Expected: orders=1 charges=1, and the same key with another body -> 422.
"""
import argparse
import asyncio
import collections
import json
import os
import random
import uuid

import httpx
import psycopg

API_URL = os.environ.get("API_URL", "http://localhost:8000")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://localhost:9000")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:5432/shop")


async def run(repeats: int, product_name: str = "replay") -> dict:
    """One replay; returns the numbers (also used by the demo dashboard, demo/main.py)."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as db:
        product_id = db.execute("INSERT INTO products (name, price_cents, stock) VALUES (%s, 999, 10) RETURNING id",
                                [product_name]).fetchone()[0]
    customer_id = random.randint(10**9, 2 * 10**9)  # unique, so we can count this run's orders
    key = str(uuid.uuid4())
    body = {"customer_id": customer_id, "items": [{"product_id": product_id, "qty": 1}]}

    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as http:
        send = lambda b: http.post("/orders", json=b, headers={"Idempotency-Key": key})  # noqa: E731
        burst = await asyncio.gather(*(send(body) for _ in range(repeats // 2)))
        one_by_one = [await send(body) for _ in range(repeats - repeats // 2)]
        other_body = await send({**body, "items": [{"product_id": product_id, "qty": 2}]})

    with psycopg.connect(DATABASE_URL, autocommit=True) as db:
        order_ids = [r[0] for r in db.execute("SELECT id FROM orders WHERE customer_id = %s", [customer_id])]
        stock = db.execute("SELECT stock FROM products WHERE id = %s", [product_id]).fetchone()[0]
    paid = [c for c in httpx.get(f"{PAYMENTS_URL}/charges").json() if c["order_id"] in order_ids and c["status"] == "succeeded"]
    codes = collections.Counter(r.status_code for r in burst + one_by_one)

    return {"sent": repeats, "answers": dict(sorted(codes.items())),
            "same_body_on_every_201": len({json.dumps(r.json(), sort_keys=True) for r in burst + one_by_one if r.status_code == 201}) == 1,
            "orders": len(order_ids), "charges": len(paid), "stock": stock, "stock_before": 10,
            "other_body_status": other_body.status_code}


async def main(repeats: int) -> None:
    r = await run(repeats)
    print(f"sent={r['sent']} answers={r['answers']} same_body_on_every_201={r['same_body_on_every_201']}")
    print(f"orders={r['orders']} charges={r['charges']} stock={r['stock']} (started at 10)")
    print(f"same key, other body -> {r['other_body_status']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repeats", type=int, default=50)
    asyncio.run(main(p.parse_args().repeats))
