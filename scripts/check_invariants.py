"""Checks the three invariants (no oversell, no double charge, no unresolved payment) against the DB + fake-payments. Exit code 1 if any is broken.

    python scripts/check_invariants.py [--wait 150]

1. oversell: for every product, initial stock == stock now + qty in orders that are not FAILED/CANCELLED
2. charges: every PAID order has exactly 1 succeeded charge, every other order has none
3. unknown: no order is left PAYMENT_UNKNOWN (or PENDING) once the reconciler is done
Initial stock comes from results/seed.json, written by chaos.py.
It first waits (up to --wait s) for the reconciler to finish; stale PENDING orders need PENDING_STALE_S (60 s).
"""
import argparse
import collections
import json
import os
import pathlib
import sys
import time

import httpx
import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:5432/shop")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://localhost:9000")
SEED = pathlib.Path(__file__).resolve().parents[1] / "results" / "seed.json"


def check(initial: dict[int, int], stock: dict[int, int], sold: dict[int, int],
          orders: dict[int, str], charges: list[dict]) -> dict[str, int]:
    """Pure function (tested in scripts/test_check_invariants.py). sold = qty per product in active orders."""
    succeeded = collections.Counter(c["order_id"] for c in charges if c["status"] == "succeeded")
    return {
        "duplicate_charges": sum(n - 1 for n in succeeded.values()),
        "oversell": sum(1 for p, start in initial.items() if stock[p] < 0 or start != stock[p] + sold.get(p, 0)),
        "unknown": sum(1 for s in orders.values() if s in ("PAYMENT_UNKNOWN", "PENDING")),
        "paid_without_charge": sum(1 for o, s in orders.items() if s == "PAID" and succeeded[o] == 0),
        "charged_not_paid": sum(1 for o in succeeded if orders.get(o) != "PAID"),
    }


def load(db) -> tuple[dict, dict, dict, list]:
    stock = dict(db.execute("SELECT id, stock FROM products").fetchall())
    sold = dict(db.execute(
        "SELECT oi.product_id, sum(oi.qty) FROM order_items oi JOIN orders o ON o.id = oi.order_id"
        " WHERE o.status NOT IN ('FAILED', 'CANCELLED') GROUP BY oi.product_id").fetchall())
    orders = dict(db.execute("SELECT id, status FROM orders").fetchall())
    charges = httpx.get(f"{PAYMENTS_URL}/charges").json()
    return stock, sold, orders, charges


def main(wait_s: float) -> int:
    initial = {int(k): v for k, v in json.loads(SEED.read_text()).items()}
    deadline = time.monotonic() + wait_s
    with psycopg.connect(DATABASE_URL, autocommit=True) as db:
        while True:
            stock, sold, orders, charges = load(db)
            result = check(initial, stock, sold, orders, charges)
            if result["unknown"] == 0 or time.monotonic() > deadline:
                break
            print(f"waiting for the reconciler: {result['unknown']} orders still PENDING/PAYMENT_UNKNOWN", flush=True)
            time.sleep(5)
    statuses = collections.Counter(orders.values())
    print("orders:", dict(sorted(statuses.items())), "charges_succeeded:", sum(c["status"] == "succeeded" for c in charges))
    print(" ".join(f"{k}={v}" for k, v in result.items()))
    ok = not any(result.values())
    print("INVARIANTS OK" if ok else "INVARIANTS BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--wait", type=float, default=150, help="max seconds to wait for the reconciler")
    sys.exit(main(p.parse_args().wait))
