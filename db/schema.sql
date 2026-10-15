-- Runs once on an empty Postgres volume (docker-entrypoint-initdb.d). Re-apply: `docker compose down -v`.

CREATE TABLE products (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT   NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    stock       INTEGER NOT NULL CHECK (stock >= 0)  -- last line of defence: a buggy path errors instead of overselling
);

CREATE TABLE orders (
    id          BIGSERIAL PRIMARY KEY,
    customer_id BIGINT  NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'PAID', 'FAILED', 'PAYMENT_UNKNOWN', 'CANCELLED')),
    total_cents INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- the reconciler scans only the few non-final orders
CREATE INDEX orders_open_idx ON orders (created_at) WHERE status IN ('PENDING', 'PAYMENT_UNKNOWN');

CREATE TABLE order_items (
    order_id    BIGINT  NOT NULL REFERENCES orders(id),
    product_id  BIGINT  NOT NULL REFERENCES products(id),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    price_cents INTEGER NOT NULL,  -- price at the time of purchase, products.price can change later
    PRIMARY KEY (order_id, product_id)
);
CREATE INDEX order_items_product_idx ON order_items (product_id);

CREATE TABLE payments (
    id           BIGSERIAL PRIMARY KEY,
    order_id     BIGINT  NOT NULL UNIQUE REFERENCES orders(id),
    provider_ref TEXT    NOT NULL,  -- the Idempotency-Key we send to the provider (= order id)
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending | succeeded | declined | unknown | not_found
    attempts     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE idempotency_keys (
    key           TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('PROCESSING', 'COMPLETED')),
    response_json JSONB,  -- {"status_code": 201, "body": {...}} once COMPLETED
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
