from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.hems_batterij.planner import MODE_GRID_CHARGE, BatteryModel, PlanSlot, make_plan
from custom_components.hems_batterij.price_forecast import (
    DEFAULT_TRUST,
    ForecastPoint,
    ForecastSkill,
    Tariff,
    fit_tariff,
    forecast_blocks,
    parse_epexpredictor,
)
from custom_components.hems_batterij.prices import PriceSlot

TZ = ZoneInfo("Europe/Amsterdam")


def _quarters(start: datetime, raws: list[float]) -> list[ForecastPoint]:
    return [
        ForecastPoint(start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)), r)
        for i, r in enumerate(raws)
    ]


def test_parse_epexpredictor():
    payload = {"s": [1792786500, 1792787400, 1792788300], "t": [0.0872, 0.098, "x"]}
    points = parse_epexpredictor(payload)
    assert len(points) == 2
    assert points[0].end - points[0].start == timedelta(minutes=15)
    assert abs(points[1].raw - 0.098) < 1e-9
    assert parse_epexpredictor(None) == []
    assert parse_epexpredictor({"s": 1}) == []


def test_fit_tariff_learns_vat_and_tax():
    start = datetime(2026, 10, 24, 0, 0, tzinfo=TZ)
    raws = [0.05 + 0.01 * (i % 12) for i in range(40)]
    points = _quarters(start, raws)
    known = [PriceSlot(p.start, p.end, 1.21 * p.raw + 0.13, p.raw + 0.02) for p in points]
    tariff = fit_tariff(known, points)
    assert tariff is not None
    assert abs(tariff.a_buy - 1.21) < 1e-6 and abs(tariff.b_buy - 0.13) < 1e-6
    assert abs(tariff.ex(0.1) - 0.12) < 1e-6
    # Te weinig overlap: niets leren.
    assert fit_tariff(known[:5], points) is None


def test_skill_measures_trust_per_lead_day():
    issued = datetime(2026, 10, 20, 7, 0, tzinfo=TZ)
    day = datetime(2026, 10, 21, 0, 0, tzinfo=TZ)
    actual = [0.20 + 0.10 * (h in range(17, 21)) for h in range(24)]
    perfect = {day + timedelta(hours=h): actual[h] for h in range(24)}
    skill = ForecastSkill()
    skill.record(issued, perfect)
    real = [
        PriceSlot(day + timedelta(minutes=15 * q), day + timedelta(minutes=15 * (q + 1)), actual[q // 4], 0)
        for q in range(96)
    ]
    assert skill.score(real, TZ) == 1
    assert skill.trust(1) > DEFAULT_TRUST[1]
    assert skill.score(real, TZ) == 0  # niet dubbel tellen

    # Een voorspelling die niets van het verloop snapt, verlaagt het vertrouwen.
    bad = ForecastSkill()
    flat_wrong = {day + timedelta(hours=h): 0.45 - actual[h] for h in range(24)}
    bad.record(issued, flat_wrong)
    bad.score(real, TZ)
    assert bad.trust(1) < DEFAULT_TRUST[1]
    restored = ForecastSkill.from_dict(bad.to_dict())
    assert restored.trust(1) == bad.trust(1)


def test_forecast_blocks_are_hourly_damped_and_after_known():
    now = datetime(2026, 10, 24, 8, 0, tzinfo=TZ)
    known_until = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
    points = _quarters(now, [0.20] * 4 * 16 + [0.30] * 4 * 24)
    blocks = forecast_blocks(points, Tariff(1.0, 0.0, 1.0, 0.0), ForecastSkill(), known_until, now, 0.20)
    assert blocks[0].start == known_until
    assert all(b.end - b.start == timedelta(hours=1) for b in blocks)
    trust = DEFAULT_TRUST[1]
    assert abs(blocks[0].buy - (0.20 + trust * 0.10)) < 1e-9
    assert blocks[0].buy_raw == 0.30


def _battery() -> BatteryModel:
    return BatteryModel(
        capacity_kwh=10.0,
        soc_min_kwh=1.0,
        soc_max_kwh=10.0,
        max_charge_kw=4.0,
        max_discharge_kw=4.0,
        roundtrip_efficiency=0.81,
        wear_eur_per_kwh=0.047,
    )


def _slots(now: datetime, known: list[float], forecast: list[float]) -> list[PlanSlot]:
    slots = [
        PlanSlot(now + timedelta(hours=h), now + timedelta(hours=h + 1), p, p, 0.0, 0.3)
        for h, p in enumerate(known)
    ]
    base = now + timedelta(hours=len(known))
    slots += [
        PlanSlot(base + timedelta(hours=h), base + timedelta(hours=h + 1), p, p, 0.0, 0.3, forecast=True)
        for h, p in enumerate(forecast)
    ]
    return slots


def test_planner_charges_for_expensive_forecast_day_only_when_trusted():
    now = datetime(2026, 10, 24, 10, 0, tzinfo=TZ)
    known = [0.13] * 14
    reference = 0.13
    raw = [0.25] * 48 + [0.45] * 6 + [0.25] * 18
    trusted = [reference + 0.9 * (p - reference) for p in raw]
    doubtful = [reference + 0.1 * (p - reference) for p in raw]

    plan = make_plan(_slots(now, known, trusted), _battery(), 1.0, now, 0.1)
    charging = [s for s in plan.steps[:14] if s.mode == MODE_GRID_CHARGE]
    assert charging, "goedkoop laden voor de dure voorspelde dag"
    assert "(voorspeld)" in charging[0].reason
    assert any(s.as_dict().get("voorspeld") for s in plan.steps)

    plan = make_plan(_slots(now, known, doubtful), _battery(), 1.0, now, 0.1)
    assert all(s.mode != MODE_GRID_CHARGE for s in plan.steps[:14])
