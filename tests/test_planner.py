from datetime import datetime, timedelta

import pytest

from custom_components.hems_batterij.planner import (
    MODE_GRID_CHARGE,
    MODE_HOLD,
    MODE_SELF_CONSUMPTION,
    MODE_SELL,
    BatteryModel,
    PlanSlot,
    break_even_spread,
    make_plan,
)

T0 = datetime.fromisoformat("2026-10-03T00:00:00+02:00")
Q = timedelta(minutes=15)

# Twee Marsteks samen, zoals bij Marco.
BATTERY = BatteryModel(
    capacity_kwh=10.24,
    soc_min_kwh=1.23,
    soc_max_kwh=10.24,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    roundtrip_efficiency=0.806,
    wear_eur_per_kwh=0.047,
)


def slots(prices, pv=None, load=0.1, sell=None):
    pv = pv or [0.0] * len(prices)
    sell = sell or prices
    return [
        PlanSlot(T0 + i * Q, T0 + (i + 1) * Q, p, s, v, load)
        for i, (p, s, v) in enumerate(zip(prices, sell, pv, strict=True))
    ]


def test_break_even_matches_rekenmodel():
    # 25 ct inkoop, 80,6% rendement, 4,7 ct slijtage -> ca. 10,7 ct
    assert break_even_spread(0.25, 0.806, 0.047) == pytest.approx(0.1072, abs=1e-3)


def test_charges_cheap_and_covers_expensive_load():
    prices = [0.10] * 8 + [0.50] * 8
    plan = make_plan(slots(prices, load=0.5), BATTERY, soc_now_kwh=1.23, created=T0)
    modes = [s.mode for s in plan.steps]
    assert MODE_GRID_CHARGE in modes[:8]
    assert all(s.battery_ac_kwh >= -1e-9 for s in plan.steps[8:])
    assert sum(s.battery_ac_kwh for s in plan.steps[8:]) > 5.0
    assert plan.expected_saving_eur(1.23) > 0.5


def test_pv_exported_during_netting_when_spread_too_small():
    # Met saldering is terugleveren evenveel waard als afnemen. Opslaan kost
    # rendement en slijtage, dus bij een klein prijsverschil levert de planner
    # de zon liever terug dan dat hij de batterij vult.
    prices = [0.25] * 8 + [0.30] * 8
    pv = [1.0] * 8 + [0.0] * 8
    plan = make_plan(slots(prices, pv=pv, load=0.1), BATTERY, soc_now_kwh=1.23, created=T0)
    assert plan.steps[7].soc_end_kwh < 1.5


def test_small_spread_does_not_trade():
    # 5 ct verschil is minder dan de ~10 ct die nodig is: niet laden uit het net.
    prices = [0.25] * 8 + [0.30] * 8
    plan = make_plan(slots(prices, load=0.3), BATTERY, soc_now_kwh=1.23, created=T0)
    assert all(s.mode != MODE_GRID_CHARGE for s in plan.steps)
    assert all(s.battery_ac_kwh >= -1e-9 for s in plan.steps)


def test_pv_surplus_is_stored_for_evening_after_netting():
    # Na saldering levert terugleveren weinig op: zon opslaan, maar niet uit het net laden
    # (5 ct verschil is minder dan de ~10 ct die laden uit het net kost).
    prices = [0.25] * 8 + [0.30] * 8
    sell = [0.02] * 16
    pv = [1.0] * 8 + [0.0] * 8  # 4 kW zon, huis 0,4 kW
    plan = make_plan(slots(prices, pv=pv, load=0.1, sell=sell), BATTERY, soc_now_kwh=1.23, created=T0)
    assert plan.steps[7].soc_end_kwh > 5.0
    assert all(s.mode == MODE_SELF_CONSUMPTION for s in plan.steps[:8])
    assert all(s.mode != MODE_GRID_CHARGE for s in plan.steps)


def test_hold_before_peak():
    # Batterij vol, nu middelmatig, straks duur: niet nu al ontladen.
    prices = [0.25] * 4 + [0.33] * 20
    plan = make_plan(slots(prices, load=0.1), BATTERY, soc_now_kwh=7.0, created=T0)
    assert plan.steps[0].mode == MODE_HOLD
    assert plan.steps[0].max_discharge_w == 0
    assert "bewaren" in plan.steps[0].reason


def test_sell_on_extreme_peak_after_netting():
    # Na saldering: piek met hoge terugleverprijs, daarna goedkoop.
    prices = [0.80] * 2 + [0.10] * 10
    sell = [0.70] * 2 + [0.02] * 10
    plan = make_plan(slots(prices, load=0.05, sell=sell), BATTERY, soc_now_kwh=9.0, created=T0)
    assert plan.steps[0].mode == MODE_SELL
    assert plan.steps[0].fixed_w > 0


def test_limits_are_respected():
    prices = [0.05] * 4 + [0.60] * 4
    plan = make_plan(slots(prices, load=0.2), BATTERY, soc_now_kwh=1.23, created=T0)
    for s in plan.steps:
        assert BATTERY.soc_min_kwh - 1e-6 <= s.soc_end_kwh <= BATTERY.soc_max_kwh + 1e-6
        assert abs(s.battery_ac_kwh) / 0.25 <= 5.0 + 1e-6


def test_flat_prices_nothing_special():
    plan = make_plan(slots([0.30] * 16, load=0.2), BATTERY, soc_now_kwh=5.0, created=T0)
    assert all(s.mode in (MODE_SELF_CONSUMPTION, MODE_HOLD) for s in plan.steps)
    assert all(s.mode != MODE_GRID_CHARGE for s in plan.steps)


def test_empty_input():
    plan = make_plan([], BATTERY, soc_now_kwh=5.0, created=T0)
    assert plan.steps == []
    assert plan.expected_saving_eur(5.0) == 0.0


def test_full_day_is_fast_enough():
    import time

    prices = [0.2 + 0.2 * ((i % 96) / 96) for i in range(140)]
    start = time.perf_counter()
    make_plan(slots(prices, load=0.15), BATTERY, soc_now_kwh=5.0, created=T0, step_kwh=0.1)
    assert time.perf_counter() - start < 5.0
