from datetime import UTC, datetime, timedelta
import json
from zoneinfo import ZoneInfo

from custom_components.hems_batterij.planner import MODE_GRID_CHARGE, BatteryModel, PlanSlot, make_plan
from custom_components.hems_batterij.price_forecast import (
    DEFAULT_TRUST,
    ForecastPoint,
    ForecastSkill,
    Tariff,
    fit_tariff,
    forecast_blocks,
    hourly_band,
    jeroen_request,
    parse_epexpredictor,
    parse_jeroen,
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


_JEROEN_COLUMNS = [
    "time_local", "known_market_price", "forecast_p10", "forecast_p50", "forecast_p90", "time_utc"
]


def _jeroen_payload(rows: list[list]) -> dict:
    """Zoals de MCP-server van jeroen.nl antwoordt (ingekort)."""
    content = {
        "source": "jeroen.nl",
        "unit": "EUR/kWh",
        "resolution": "quarter",
        "timezone": "Europe/Amsterdam",
        "forecast_made_at_utc": "2026-10-24T03:00:04Z",
        "columns": _JEROEN_COLUMNS,
        "rows": rows,
    }
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": json.dumps(content)}],
            "structuredContent": content,
            "isError": False,
        },
    }


def test_parse_jeroen_known_and_forecast_rows():
    rows = [
        ["2026-10-24 23:45", 0.0081, None, None, None, "2026-10-24T21:45:00Z"],
        ["2026-10-25 00:00", None, 0.02926, 0.03867, 0.10426, "2026-10-24T22:00:00Z"],
        # Einde zomertijd: 02:00 lokaal komt twee keer voor, time_utc houdt ze uit elkaar.
        ["2026-10-25 02:00", None, 0.02, 0.03, 0.05, "2026-10-25T00:00:00Z"],
        ["2026-10-25 02:00", None, 0.021, 0.031, 0.051, "2026-10-25T01:00:00Z"],
        ["2026-10-25 03:00", None, 0.05, 0.03, 0.06, "2026-10-25T02:00:00Z"],  # band zonder de mediaan erin
        ["kapot", None, None, None, None, "geen tijd"],
        ["2026-10-25 04:00", None, None, None, None, "2026-10-25T03:00:00Z"],
    ]
    points = parse_jeroen(_jeroen_payload(rows))
    assert len(points) == 5
    assert points[0].raw == 0.0081 and points[0].low is None
    assert points[0].start == datetime(2026, 10, 24, 21, 45, tzinfo=UTC)
    assert points[0].end - points[0].start == timedelta(minutes=15)
    assert (points[1].low, points[1].raw, points[1].high) == (0.02926, 0.03867, 0.10426)
    assert points[2].start != points[3].start
    assert points[4].low is None and points[4].raw == 0.03

    # Alleen de tekstvorm (oudere MCP-clients) werkt ook.
    payload = _jeroen_payload(rows)
    del payload["result"]["structuredContent"]
    assert len(parse_jeroen(payload)) == 5

    assert parse_jeroen(None) == []
    assert parse_jeroen({"jsonrpc": "2.0", "error": {"code": -32000}}) == []
    assert parse_jeroen({"result": {"isError": True, "content": []}}) == []
    assert parse_jeroen({"result": {"structuredContent": {"columns": ["x"], "rows": []}}}) == []


def test_jeroen_request_asks_quarters_for_seven_days():
    args = jeroen_request()["params"]["arguments"]
    assert args == {"days": 7, "resolution": "quarter"}
    assert jeroen_request(30)["params"]["arguments"]["days"] == 7


def test_hourly_band_needs_band_on_every_quarter():
    start = datetime(2026, 10, 26, 0, 0, tzinfo=TZ)
    points = [
        ForecastPoint(t, t + timedelta(minutes=15), 0.1, 0.05, 0.2)
        for t in (start + timedelta(minutes=15 * i) for i in range(4))
    ]
    points.append(ForecastPoint(start + timedelta(hours=1), start + timedelta(hours=1, minutes=15), 0.1))
    bands = hourly_band(points, Tariff(1.0, 0.1, 1.0, 0.0), TZ)
    assert list(bands) == [start]
    low, high = bands[start]
    assert abs(low - 0.15) < 1e-9 and abs(high - 0.30) < 1e-9


def test_skill_measures_how_often_actual_falls_in_band():
    issued = datetime(2026, 10, 20, 7, 0, tzinfo=TZ)
    day = datetime(2026, 10, 21, 0, 0, tzinfo=TZ)
    hours = [day + timedelta(hours=h) for h in range(24)]
    skill = ForecastSkill()
    # De echte prijs is overal 0,20; de band mist hem in 6 van de 24 uur.
    bands = {t: ((0.15, 0.25) if h >= 6 else (0.21, 0.30)) for h, t in enumerate(hours)}
    skill.record(issued, {t: 0.20 for t in hours}, bands)
    real = [
        PriceSlot(day + timedelta(minutes=15 * q), day + timedelta(minutes=15 * (q + 1)), 0.20, 0)
        for q in range(96)
    ]
    assert skill.score(real, TZ) == 1
    assert skill.band_hit_pct(1) == 75.0
    assert skill.summary()["1"]["binnen_band_pct"] == 75.0
    restored = ForecastSkill.from_dict(json.loads(json.dumps(skill.to_dict())))
    assert restored.band_hit_pct(1) == 75.0
    # Oude opslag met drie getallen per dag vooruit blijft leesbaar.
    old = ForecastSkill.from_dict({"stats": {"1": [1.0, 2.0, 24.0]}})
    assert old.band_hit_pct(1) is None and old.mae_ct(1) is not None


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
