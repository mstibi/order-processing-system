"""Fake Stripe. State is in memory: a restart forgets all charges (the chaos test only kills the API).

POST /charges  header Idempotency-Key, body {"order_id": int, "amount_cents": int}
    201 {"status": "succeeded"} | 402 {"status": "declined"} | hangs LATENCY+TIMEOUT_SLEEP_S then answers (timeout case)
    Same key again -> the stored result, never a second charge. Same key with other params -> 400 (like Stripe).
GET /charges/{key}  -> stored result or 404 (never seen). Used by the reconciler.
GET /charges        -> all charges, for scripts/check_invariants.py.
POST /reset         -> forget everything (scripts/tests).
Env (read per request, so tests can change them): FAIL_RATE, TIMEOUT_RATE, LATENCY_MS, TIMEOUT_SLEEP_S.
A "timeout" charge is decided like any other (50% it really succeeded) and recorded BEFORE the delay:
the client sees no answer, but the money may have moved. That is the whole point of PAYMENT_UNKNOWN.
"""
import asyncio
import os
import random

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="fake-payments")
charges: dict[str, dict] = {}


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


class ChargeIn(BaseModel):
    order_id: int
    amount_cents: int


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/charges")
async def create_charge(body: ChargeIn, idempotency_key: str = Header(...)):
    existing = charges.get(idempotency_key)
    if existing:
        if (existing["order_id"], existing["amount_cents"]) != (body.order_id, body.amount_cents):
            raise HTTPException(400, "idempotency key reused with different parameters")
        return JSONResponse(_public(existing), status_code=201 if existing["status"] == "succeeded" else 402)

    roll = random.random()
    timed_out = roll < _f("TIMEOUT_RATE", 0)
    declined = (not timed_out and roll < _f("TIMEOUT_RATE", 0) + _f("FAIL_RATE", 0)) or (timed_out and random.random() < 0.5)
    rec = {
        "key": idempotency_key, "order_id": body.order_id, "amount_cents": body.amount_cents,
        "status": "declined" if declined else "succeeded",
    }
    charges[idempotency_key] = rec  # recorded before any await: concurrent retries of this key see it

    await asyncio.sleep(_f("LATENCY_MS", 0) / 1000)
    if timed_out:
        await asyncio.sleep(_f("TIMEOUT_SLEEP_S", 30))
    return JSONResponse(_public(rec), status_code=201 if rec["status"] == "succeeded" else 402)


@app.get("/charges/{key}")
def get_charge(key: str):
    rec = charges.get(key)
    if not rec:
        raise HTTPException(404, "unknown key")
    return _public(rec)


@app.get("/charges")
def list_charges():
    return [_public(c) for c in charges.values()]


@app.post("/reset")
def reset():
    charges.clear()
    return {"status": "ok"}


def _public(rec: dict) -> dict:
    return dict(rec)
