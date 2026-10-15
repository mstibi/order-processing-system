"""M5/M6: many orders against a faulty payment provider, optionally killing the API now and then.

    docker compose up -d   (with FAIL_RATE=0.2 TIMEOUT_RATE=0.1 for M5)
    python scripts/chaos.py --orders 1000
    python scripts/chaos.py --orders 1000 --kill-api-every 200        # M6
    python scripts/check_invariants.py

WARNING: wipes all tables and the fake-payments memory first, then seeds products (results/seed.json).
The client behaves like a sane real client: on a network error, 5xx or "request in progress" it
retries with the SAME Idempotency-Key.
"""
import argparse
import asyncio
import collections
import json
import os
import pathlib
import random
import subprocess
import time
import uuid

import httpx
import psycopg

API_URL = os.environ.get("API_URL", "http://localhost:8000")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://localhost:9000")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:5432/shop")
ROOT = pathlib.Path(__file__).resolve().parents[1]


def seed(products: int, stock: int) -> list[int]:
    with psycopg.connect(DATABASE_URL, autocommit=True) as db:
        db.execute("TRUNCATE order_items, payments, orders, idempotency_keys, products RESTART IDENTITY CASCADE")
        ids = [db.execute("INSERT INTO products (name, price_cents, stock) VALUES (%s, %s, %s) RETURNING id",
                          [f"chaos-{i}", 500 + 100 * i, stock]).fetchone()[0] for i in range(products)]
    httpx.post(f"{PAYMENTS_URL}/reset").raise_for_status()  # order ids restart at 1, so must the provider
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "seed.json").write_text(json.dumps({i: stock for i in ids}))
    return ids


async def one_order(http: httpx.AsyncClient, customer_id: int, product_ids: list[int]) -> str:
    key = str(uuid.uuid4())
    body = {"customer_id": customer_id, "items": [{"product_id": random.choice(product_ids), "qty": random.randint(1, 3)}]}
    network_errors = in_progress = 0
    while network_errors < 30 and in_progress < 10:
        try:
            r = await http.post("/orders", json=body, headers={"Idempotency-Key": key})
        except httpx.TransportError:  # API down / restarting: same key again later
            network_errors += 1
            await asyncio.sleep(0.5)
            continue
        if r.status_code == 409 and r.json().get("error") == "request_in_progress":
            in_progress += 1
            await asyncio.sleep(0.5)
            continue
        if r.status_code >= 500:
            network_errors += 1
            await asyncio.sleep(0.5)
            continue
        return r.json().get("status") or r.json().get("error")
    return "gave_up"


async def main(n: int, concurrency: int, products: int, stock: int, kill_every: int, restart_cmd: str) -> None:
    product_ids = seed(products, stock)
    sem = asyncio.Semaphore(concurrency)
    kills = []

    async def worker(i: int) -> str:
        async with sem:
            if kill_every and i and i % kill_every == 0:  # crash the API while ~concurrency orders are in flight
                kills.append(asyncio.create_task(asyncio.to_thread(subprocess.run, restart_cmd, shell=True, cwd=ROOT)))
            return await one_order(http, i, product_ids)

    start = time.perf_counter()
    async with httpx.AsyncClient(base_url=API_URL, timeout=15, limits=httpx.Limits(max_connections=concurrency)) as http:
        outcomes = await asyncio.gather(*(worker(i) for i in range(n)))
    await asyncio.gather(*kills)
    print(f"orders={n} kills={len(kills)} seconds={time.perf_counter() - start:.1f}")
    print("client saw:", dict(collections.Counter(outcomes).most_common()))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--orders", type=int, default=1000)
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--products", type=int, default=5)
    p.add_argument("--stock", type=int, default=300, help="per product; 5x300 < 1000 orders x avg qty 2, so some sell out")
    p.add_argument("--kill-api-every", type=int, default=0)
    p.add_argument("--restart-cmd", default="docker compose kill api && docker compose start api",
                   help="how to crash + restart the API (SIGKILL, a real crash, not a graceful stop)")
    a = p.parse_args()
    asyncio.run(main(a.orders, a.concurrency, a.products, a.stock, a.kill_api_every, a.restart_cmd))
