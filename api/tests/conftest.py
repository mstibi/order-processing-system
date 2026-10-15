"""Tests talk to the real Postgres and the real fake-payments service (docker compose).
WARNING: every test wipes all tables."""
import asyncio
import sys

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

if sys.platform == "win32":  # psycopg async does not work on Windows' default event loop (local runs only)
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.main import DATABASE_URL, PAYMENTS_URL, app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:  # runs the lifespan: opens the pool, starts the reconciler
        yield c


@pytest.fixture(autouse=True)
def db():
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE order_items, payments, orders, idempotency_keys, products RESTART IDENTITY CASCADE")
        httpx.post(f"{PAYMENTS_URL}/reset").raise_for_status()  # order ids restart at 1, so must the provider
        yield conn


def add_product(db, stock: int, price_cents: int = 1000) -> int:
    return db.execute("INSERT INTO products (name, price_cents, stock) VALUES ('test', %s, %s) RETURNING id",
                      [price_cents, stock]).fetchone()[0]


def stock_of(db, product_id: int) -> int:
    return db.execute("SELECT stock FROM products WHERE id = %s", [product_id]).fetchone()[0]


def charges() -> list[dict]:
    return httpx.get(f"{PAYMENTS_URL}/charges").json()
