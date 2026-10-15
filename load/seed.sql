-- one product with "infinite" stock for the load test, fixed id so orders.js knows it
INSERT INTO products (id, name, price_cents, stock) VALUES (1000000, 'load-test', 100, 100000000)
ON CONFLICT (id) DO UPDATE SET stock = EXCLUDED.stock;
