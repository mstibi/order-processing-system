from conftest import add_product, charges, stock_of

BODY = lambda pid, qty=1: {"customer_id": 1, "items": [{"product_id": pid, "qty": qty}]}  # noqa: E731


def test_same_key_twice_gives_same_answer_one_order_one_charge(client, db):
    pid = add_product(db, stock=5)
    first = client.post("/orders", headers={"Idempotency-Key": "abc"}, json=BODY(pid))
    again = client.post("/orders", headers={"Idempotency-Key": "abc"}, json=BODY(pid))
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
    assert len(charges()) == 1 and stock_of(db, pid) == 4


def test_same_key_other_body_is_422(client, db):
    pid = add_product(db, stock=5)
    client.post("/orders", headers={"Idempotency-Key": "abc"}, json=BODY(pid))
    r = client.post("/orders", headers={"Idempotency-Key": "abc"}, json=BODY(pid, qty=2))
    assert r.status_code == 422
    assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 1


def test_key_still_processing_is_409(client, db):
    pid = add_product(db, stock=5)
    client.post("/orders", headers={"Idempotency-Key": "abc"}, json=BODY(pid))
    db.execute("UPDATE idempotency_keys SET status = 'PROCESSING'")  # pretend the first request is still running
    r = client.post("/orders", headers={"Idempotency-Key": "abc"}, json=BODY(pid))
    assert r.status_code == 409 and r.json()["error"] == "request_in_progress"


def test_item_order_in_json_does_not_change_the_hash(client, db):
    a, b = add_product(db, stock=5), add_product(db, stock=5)
    items = [{"product_id": a, "qty": 1}, {"product_id": b, "qty": 1}]
    first = client.post("/orders", headers={"Idempotency-Key": "abc"}, json={"customer_id": 1, "items": items})
    again = client.post("/orders", headers={"Idempotency-Key": "abc"}, json={"customer_id": 1, "items": items[::-1]})
    assert first.json() == again.json()


def test_out_of_stock_answer_is_also_replayed(client, db):
    pid = add_product(db, stock=0)
    first = client.post("/orders", headers={"Idempotency-Key": "abc"}, json=BODY(pid))
    db.execute("UPDATE products SET stock = 10")  # restocked meanwhile: a retry still gets the first answer
    again = client.post("/orders", headers={"Idempotency-Key": "abc"}, json=BODY(pid))
    assert first.status_code == again.status_code == 409
