"""The oracle must catch every kind of breakage, else every "OK" later is worthless.  pytest scripts/"""
from check_invariants import check

GOOD = dict(initial={1: 10}, stock={1: 7}, sold={1: 3},
            orders={1: "PAID", 2: "FAILED", 3: "CANCELLED"},
            charges=[{"order_id": 1, "status": "succeeded"}, {"order_id": 2, "status": "declined"}])


def run(**changes):
    return check(**{**GOOD, **changes})


def test_good_state_is_all_zero():
    assert not any(run().values())


def test_oversell_and_lost_restock_are_caught():
    assert run(stock={1: 6})["oversell"] == 1  # one item vanished
    assert run(stock={1: 8})["oversell"] == 1  # restocked twice
    assert run(stock={1: -1}, sold={1: 11})["oversell"] == 1


def test_duplicate_charge_is_caught():
    assert run(charges=GOOD["charges"] + [{"order_id": 1, "status": "succeeded"}])["duplicate_charges"] == 1


def test_paid_without_charge_and_charged_not_paid_are_caught():
    assert run(charges=[])["paid_without_charge"] == 1
    assert run(charges=GOOD["charges"] + [{"order_id": 3, "status": "succeeded"}])["charged_not_paid"] == 1


def test_unknown_and_pending_are_caught():
    assert run(orders={**GOOD["orders"], 4: "PAYMENT_UNKNOWN", 5: "PENDING"})["unknown"] == 2
