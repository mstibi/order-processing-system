import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient
import app as fp

H = {"Idempotency-Key": "k1"}
B = {"order_id": 1, "amount_cents": 500}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    fp.charges.clear()
    for v in ("FAIL_RATE", "TIMEOUT_RATE", "LATENCY_MS", "TIMEOUT_SLEEP_S"):
        monkeypatch.delenv(v, raising=False)
    yield TestClient(fp.app)


@pytest.fixture
def c():
    return TestClient(fp.app)


def test_same_key_charges_once(c):
    assert c.post("/charges", json=B, headers=H).status_code == 201
    assert c.post("/charges", json=B, headers=H).status_code == 201
    assert len(c.get("/charges").json()) == 1


def test_same_key_other_params_is_400(c):
    c.post("/charges", json=B, headers=H)
    assert c.post("/charges", json={**B, "amount_cents": 1}, headers=H).status_code == 400


def test_fail_rate_one_declines_and_is_remembered(c, monkeypatch):
    monkeypatch.setenv("FAIL_RATE", "1")
    assert c.post("/charges", json=B, headers=H).status_code == 402
    monkeypatch.setenv("FAIL_RATE", "0")
    assert c.post("/charges", json=B, headers=H).status_code == 402  # same key, same answer
    assert c.get("/charges/k1").json()["status"] == "declined"


def test_timeout_is_lost_or_recorded_before_hanging(c, monkeypatch):
    monkeypatch.setenv("TIMEOUT_RATE", "1")
    monkeypatch.setenv("TIMEOUT_SLEEP_S", "0")  # we cannot hang in a test; the record is what matters
    seen = set()
    for i in range(60):
        r = c.post("/charges", json={**B, "order_id": i}, headers={"Idempotency-Key": str(i)})
        got = c.get(f"/charges/{i}")
        seen.add(got.json()["status"] if got.status_code == 200 else "lost")
        assert (r.status_code == 504) == (got.status_code == 404)
    assert seen == {"lost", "succeeded", "declined"}


def test_unknown_key_404(c):
    assert c.get("/charges/nope").status_code == 404
