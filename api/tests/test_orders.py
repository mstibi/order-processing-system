from conftest import add_product, charges, stock_of


def order(client, key, product_id, qty=1, customer_id=7):
    return client.post("/orders", headers={"Idempotency-Key": key},
                       json={"customer_id": customer_id, "items": [{"product_id": product_id, "qty": qty}]})


def test_happy_path_is_paid_and_charged_once(client, db):
    pid = add_product(db, stock=5, price_cents=1250)
    r = order(client, "k1", pid, qty=2)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "PAID" and body["total_cents"] == 2500
    assert stock_of(db, pid) == 3
    assert [(c["order_id"], c["amount_cents"], c["status"]) for c in charges()] == [(body["id"], 2500, "succeeded")]


def test_get_returns_same_order(client, db):
    pid = add_product(db, stock=5)
    created = order(client, "k1", pid).json()
    assert client.get(f"/orders/{created['id']}").json() == created


def test_more_than_stock_is_409_and_nothing_changes(client, db):
    pid = add_product(db, stock=2)
    r = order(client, "k1", pid, qty=3)
    assert r.status_code == 409 and r.json()["error"] == "out_of_stock"
    assert stock_of(db, pid) == 2
    assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    assert charges() == []


def test_multi_item_order_rolls_back_whole_order(client, db):
    a, b = add_product(db, stock=5), add_product(db, stock=0)
    r = client.post("/orders", headers={"Idempotency-Key": "k1"},
                    json={"customer_id": 1, "items": [{"product_id": a, "qty": 1}, {"product_id": b, "qty": 1}]})
    assert r.status_code == 409
    assert stock_of(db, a) == 5  # the decrement of A was rolled back with the rest


def test_unknown_order_404(client):
    assert client.get("/orders/999").status_code == 404


def test_missing_key_or_bad_qty_is_422(client, db):
    pid = add_product(db, stock=5)
    assert client.post("/orders", json={"customer_id": 1, "items": [{"product_id": pid, "qty": 1}]}).status_code == 422
    assert order(client, "k1", pid, qty=0).status_code == 422
