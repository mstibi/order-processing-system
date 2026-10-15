"""All SQL about orders and stock. Every function takes an open psycopg AsyncConnection (autocommit=True),
so a statement commits on its own unless it runs inside `async with conn.transaction()`.

scripts/race.py imports reserve() directly to compare the three stock modes.
"""
from psycopg import AsyncConnection

FINAL = ("PAID", "FAILED", "CANCELLED")


class OutOfStock(Exception):
    pass


# --- three ways to take stock (M3). The API always uses "atomic". ---

async def take_naive(conn: AsyncConnection, product_id: int, qty: int) -> int:
    """BUG on purpose: read in Python, check, write back. Two buyers can read the same stock
    and both write stock-1 ("lost update"), so more is sold than exists."""
    row = await (await conn.execute("SELECT stock, price_cents FROM products WHERE id = %s", [product_id])).fetchone()
    if row is None or row[0] < qty:
        raise OutOfStock(product_id)
    await conn.execute("UPDATE products SET stock = %s WHERE id = %s", [row[0] - qty, product_id])
    return row[1]


async def take_atomic(conn: AsyncConnection, product_id: int, qty: int) -> int:
    """Check and write in ONE statement. Postgres row-locks the product; a second buyer waits,
    then re-checks `stock >= qty` on the new value (READ COMMITTED re-evaluates the WHERE)."""
    row = await (await conn.execute(
        "UPDATE products SET stock = stock - %s WHERE id = %s AND stock >= %s RETURNING price_cents",
        [qty, product_id, qty])).fetchone()
    if row is None:
        raise OutOfStock(product_id)
    return row[0]


async def take_for_update(conn: AsyncConnection, product_id: int, qty: int) -> int:
    """Pessimistic lock: SELECT ... FOR UPDATE holds the row lock until COMMIT, so the check in
    Python is safe. Correct, but one extra round trip while holding the lock."""
    row = await (await conn.execute(
        "SELECT stock, price_cents FROM products WHERE id = %s FOR UPDATE", [product_id])).fetchone()
    if row is None or row[0] < qty:
        raise OutOfStock(product_id)
    await conn.execute("UPDATE products SET stock = stock - %s WHERE id = %s", [qty, product_id])
    return row[1]


TAKE = {"naive": take_naive, "atomic": take_atomic, "for_update": take_for_update}


async def reserve(conn: AsyncConnection, customer_id: int, items: dict[int, int], mode: str = "atomic") -> tuple[int, int]:
    """One transaction: take stock for every item, create the PENDING order + its payment row.
    items = {product_id: qty}. Returns (order_id, total_cents). Raises OutOfStock (and everything is rolled back) if any item is short."""
    async with conn.transaction():
        total, lines = 0, []
        # same lock order for everyone, else orders [A,B] and [B,A] can deadlock
        for product_id, qty in sorted(items.items()):
            price = await TAKE[mode](conn, product_id, qty)
            total += price * qty
            lines.append((product_id, qty, price))
        order_id = (await (await conn.execute(
            "INSERT INTO orders (customer_id, total_cents) VALUES (%s, %s) RETURNING id",
            [customer_id, total])).fetchone())[0]
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO order_items (order_id, product_id, qty, price_cents) VALUES (%s, %s, %s, %s)",
                [(order_id, p, q, pr) for p, q, pr in lines])
        await conn.execute("INSERT INTO payments (order_id, provider_ref) VALUES (%s, %s)", [order_id, str(order_id)])
    return order_id, total


# --- status changes. Each one only moves from an allowed status, so the API and the reconciler
# can race on the same order and only one of them wins (rowcount == 1). ---

async def _move(conn: AsyncConnection, order_id: int, to: str, frm: tuple[str, ...]) -> bool:
    cur = await conn.execute("UPDATE orders SET status = %s WHERE id = %s AND status = ANY(%s)", [to, order_id, list(frm)])
    return cur.rowcount == 1


async def mark_paid(conn: AsyncConnection, order_id: int) -> None:
    async with conn.transaction():
        if await _move(conn, order_id, "PAID", ("PENDING", "PAYMENT_UNKNOWN")):
            await conn.execute("UPDATE payments SET status = 'succeeded' WHERE order_id = %s", [order_id])


async def mark_unknown(conn: AsyncConnection, order_id: int) -> None:
    async with conn.transaction():
        if await _move(conn, order_id, "PAYMENT_UNKNOWN", ("PENDING",)):
            await conn.execute("UPDATE payments SET status = 'unknown' WHERE order_id = %s", [order_id])


async def cancel(conn: AsyncConnection, order_id: int, to: str, payment_status: str) -> None:
    """to = FAILED or CANCELLED. Gives the stock back, but only if WE did the status change:
    otherwise the API and the reconciler could both restock the same order."""
    async with conn.transaction():
        if not await _move(conn, order_id, to, ("PENDING", "PAYMENT_UNKNOWN")):
            return
        await conn.execute("UPDATE payments SET status = %s WHERE order_id = %s", [payment_status, order_id])
        items = await (await conn.execute(
            "SELECT product_id, qty FROM order_items WHERE order_id = %s ORDER BY product_id", [order_id])).fetchall()
        for product_id, qty in items:
            await conn.execute("UPDATE products SET stock = stock + %s WHERE id = %s", [qty, product_id])


async def count_attempt(conn: AsyncConnection, order_id: int) -> None:
    await conn.execute("UPDATE payments SET attempts = attempts + 1 WHERE order_id = %s", [order_id])


async def get(conn: AsyncConnection, order_id: int) -> dict | None:
    row = await (await conn.execute(
        "SELECT id, customer_id, status, total_cents, created_at FROM orders WHERE id = %s", [order_id])).fetchone()
    if row is None:
        return None
    items = await (await conn.execute(
        "SELECT product_id, qty, price_cents FROM order_items WHERE order_id = %s ORDER BY product_id", [order_id])).fetchall()
    return {
        "id": row[0], "customer_id": row[1], "status": row[2], "total_cents": row[3],
        "created_at": row[4].isoformat(),
        "items": [{"product_id": p, "qty": q, "price_cents": pr} for p, q, pr in items],
    }
