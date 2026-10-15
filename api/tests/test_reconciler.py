import httpx

from app.main import PAYMENTS_URL, http, pool
from app.reconciler import reconcile_once
from conftest import add_product, stock_of


def paid_order(client, db, pid) -> int:
    return client.post("/orders", headers={"Idempotency-Key": f"k{pid}"},
                       json={"customer_id": 1, "items": [{"product_id": pid, "qty": 2}]}).json()["id"]


def status(db, order_id):
    return db.execute("SELECT status FROM orders WHERE id = %s", [order_id]).fetchone()[0]


def reconcile(client):
    return client.portal.call(reconcile_once, pool, http, 60)


def test_unknown_but_provider_charged_becomes_paid(client, db):
    pid = add_product(db, stock=5)
    oid = paid_order(client, db, pid)
    db.execute("UPDATE orders SET status = 'PAYMENT_UNKNOWN' WHERE id = %s", [oid])  # we "timed out"
    assert reconcile(client) == 1
    assert status(db, oid) == "PAID" and stock_of(db, pid) == 3


def test_unknown_and_never_charged_fails_and_restocks(client, db):
    pid = add_product(db, stock=5)
    oid = paid_order(client, db, pid)
    httpx.post(f"{PAYMENTS_URL}/reset")  # provider never saw it
    db.execute("UPDATE orders SET status = 'PAYMENT_UNKNOWN' WHERE id = %s", [oid])
    reconcile(client)
    assert status(db, oid) == "FAILED" and stock_of(db, pid) == 5
    reconcile(client)  # running again must not restock twice
    assert stock_of(db, pid) == 5


def test_stale_pending_is_cancelled_fresh_pending_is_left_alone(client, db):
    pid = add_product(db, stock=10)
    old, fresh = paid_order(client, db, pid), client.post(
        "/orders", headers={"Idempotency-Key": "fresh"},
        json={"customer_id": 1, "items": [{"product_id": pid, "qty": 2}]}).json()["id"]
    httpx.post(f"{PAYMENTS_URL}/reset")
    db.execute("UPDATE orders SET status = 'PENDING'")  # API "crashed" before the charge call
    db.execute("UPDATE orders SET created_at = now() - interval '2 minutes' WHERE id = %s", [old])
    reconcile(client)
    assert status(db, old) == "CANCELLED" and status(db, fresh) == "PENDING"
    assert stock_of(db, pid) == 8
