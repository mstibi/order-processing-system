"""Client for the payment provider (fake-payments, a pretend Stripe)."""
import httpx


async def charge(http: httpx.AsyncClient, order_id: int, amount_cents: int) -> str:
    """Returns "succeeded", "declined" or "unknown".
    The Idempotency-Key is the order id, so asking again (API retry, reconciler) never charges twice."""
    try:
        r = await http.post("/charges", json={"order_id": order_id, "amount_cents": amount_cents},
                            headers={"Idempotency-Key": str(order_id)})
    except httpx.TransportError:
        # timeout or broken connection: the charge may or may NOT have happened -> do not guess
        return "unknown"
    if r.status_code == 201:
        return "succeeded"
    if r.status_code == 402:
        return "declined"
    return "unknown"  # 5xx etc.: same as a timeout, the reconciler will ask later


async def lookup(http: httpx.AsyncClient, order_id: int) -> str | None:
    """What the provider knows about this order: "succeeded", "declined", "not_found",
    or None if we could not ask right now (try again next round)."""
    try:
        r = await http.get(f"/charges/{order_id}")
    except httpx.TransportError:
        return None
    if r.status_code == 404:
        return "not_found"
    if r.status_code == 200:
        return r.json()["status"]
    return None
