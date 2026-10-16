"""Demo endpoints against the real stack: docker compose exec demo pytest -q demo
Unlike the API tests these do NOT wipe the DB: each demo only replaces its own 'demo-...' products."""
import sys
import time
import pathlib

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import main  # noqa: E402


@pytest.fixture(scope="module")
def c():
    with TestClient(main.app) as client:
        yield client


def demo_products(prefix: str) -> int:
    with psycopg.connect(main.DATABASE_URL) as db:
        return db.execute("SELECT count(*) FROM products WHERE name LIKE %s", [prefix + "%"]).fetchone()[0]


def test_status_all_up(c):
    assert c.get("/demo/status").json() == {"api": True, "payments": True, "database": True}


def test_index_served(c):
    r = c.get("/")
    assert r.status_code == 200 and "Overselling" in r.text


@pytest.mark.parametrize("mode", ["atomic", "for_update"])
def test_oversell_fixed_modes_are_exact(c, mode):
    r = c.post("/demo/oversell", params={"mode": mode, "buyers": 200, "stock": 20}).json()
    assert (r["sold"], r["stock"], r["ok"]) == (20, 0, True)


def test_oversell_naive_shows_the_bug(c):
    r = c.post("/demo/oversell", params={"mode": "naive", "buyers": 200, "stock": 20}).json()
    assert r["sold"] > 20 and r["ok"] is False


def test_oversell_rejects_unknown_mode(c):
    assert c.post("/demo/oversell", params={"mode": "yolo"}).status_code == 422


def test_replay_one_order_one_charge_and_repeatable(c):
    for _ in range(2):  # second run must clean up the first one's product
        r = c.post("/demo/replay", params={"repeats": 10}).json()
        assert (r["orders"], r["charges"], r["other_body_status"], r["ok"]) == (1, 1, 422, True)
    assert demo_products("demo-replay") == 1


def test_payments_declines_only(c):
    r = c.post("/demo/payments", params={"orders": 30, "fail_rate": 0.5, "timeout_rate": 0}).json()
    assert r["settled"] and r["ok"]
    assert set(r["statuses"]) <= {"PAID", "FAILED"}
    assert demo_products("demo-payments") == 5


def test_payments_timeouts_settle_via_reconciler_and_rates_restored(c):
    before = httpx.post(f"{main.PAYMENTS_URL}/config", json={}).json()  # empty body = read only
    r = c.post("/demo/payments", params={"orders": 5, "fail_rate": 0, "timeout_rate": 1}).json()
    assert r["statuses"].get("PAYMENT_UNKNOWN", 0) > 0 or r["statuses"].get("FAILED", 0) > 0
    for _ in range(30):  # reconciler runs every 5 s
        state = c.get("/demo/payments").json()
        if state["settled"]:
            break
        time.sleep(1)
    assert state["settled"] and state["ok"], state
    assert httpx.post(f"{main.PAYMENTS_URL}/config", json={}).json() == before


def test_payments_rates_over_one_rejected(c):
    assert c.post("/demo/payments", params={"fail_rate": 0.7, "timeout_rate": 0.6}).status_code == 422


def test_recent_orders(c):
    rows = c.get("/demo/orders", params={"limit": 3}).json()
    assert 0 < len(rows) <= 3 and {"id", "status", "items"} <= set(rows[0])
