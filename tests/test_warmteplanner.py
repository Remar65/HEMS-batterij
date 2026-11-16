"""Warmteplanner: vooruit stoken rond dure uren."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from custom_components.hems_batterij.warmte_leren import HeatLearner
from custom_components.hems_batterij.warmteplanner import (
    HeatHour,
    HouseParams,
    make_heat_plan,
    setpoint_at,
    summary,
)

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
PARAMS = HouseParams()


def _hours(prices: list[float], t_out: float = 5.0, setpoint: float = 20.0, **kw) -> list[HeatHour]:
    return [
        HeatHour(start=T0 + timedelta(hours=i), price=p, t_out=t_out, setpoint=setpoint, **kw)
        for i, p in enumerate(prices)
    ]


def _rooms(plan) -> list[float]:
    return [h.room_end for h in plan.hours]


def test_laadt_vooruit_voor_dure_uren() -> None:
    # Middag goedkoop, avond duur, nacht weer middelmatig: zoals 16 nov.
    prices = [0.36, 0.36, 0.37, 0.39, 0.44, 0.45, 0.45, 0.42, 0.39, 0.38, 0.37, 0.36]
    plan = make_heat_plan(_hours(prices), PARAMS, room_now=20.4)
    runs = [h.run for h in plan.hours]
    assert not runs[5] and not runs[6], "in de duurste uren niet stoken"
    assert any(runs[:4]), "vóór de piek vooruit laden"
    assert min(_rooms(plan)) >= 19.5 - 0.05
    assert plan.cost_eur <= plan.thermostat_cost_eur + 1e-9


def test_kamer_blijft_boven_ondergrens() -> None:
    plan = make_heat_plan(_hours([0.30] * 24, t_out=0.0), PARAMS, room_now=19.6)
    assert min(_rooms(plan)) >= 19.5 - 0.05


def test_niet_stoken_boven_bovengrens() -> None:
    plan = make_heat_plan(_hours([0.10] * 12), PARAMS, room_now=20.6)
    assert not plan.hours[0].run
    for prev, h in zip([20.6, *_rooms(plan)], plan.hours, strict=False):
        if h.run:
            assert prev < 20.5


def test_warm_genoeg_bij_zacht_weer() -> None:
    plan = make_heat_plan(_hours([0.40] * 6, t_out=19.5), PARAMS, room_now=20.2)
    assert plan.run_hours == 0
    assert summary(plan, PARAMS, 20.2)["advies"] == "niet stoken"


def test_startboete_voorkomt_losse_runs() -> None:
    prices = [0.30, 0.31, 0.30, 0.31, 0.30, 0.31, 0.30, 0.31, 0.30, 0.31, 0.30, 0.31]
    plan = make_heat_plan(_hours(prices, t_out=2.0), PARAMS, room_now=19.8)
    runs = [h.run for h in plan.hours]
    starts = sum(1 for i, r in enumerate(runs) if r and (i == 0 or not runs[i - 1]))
    assert starts <= 3


def test_ontdooien_maakt_warmte_duurder() -> None:
    dry = HeatHour(start=T0, price=0.3, t_out=2.0, setpoint=20.0, humidity=70.0)
    wet = HeatHour(start=T0, price=0.3, t_out=2.0, setpoint=20.0, humidity=95.0)
    assert PARAMS.cop(wet) < PARAMS.cop(dry)


def test_wind_maakt_het_kouder() -> None:
    calm = HeatHour(start=T0, price=0.3, t_out=5.0, setpoint=20.0, wind=5.0)
    windy = HeatHour(start=T0, price=0.3, t_out=5.0, setpoint=20.0, wind=40.0)
    assert PARAMS.next_temp(20.0, windy, False) < PARAMS.next_temp(20.0, calm, False)


def test_reden_en_samenvatting() -> None:
    prices = [0.36, 0.36, 0.44, 0.45, 0.45, 0.40]
    plan = make_heat_plan(_hours(prices), PARAMS, room_now=19.9)
    info = summary(plan, PARAMS, 19.9)
    assert info["advies"] in ("stoken", "niet stoken")
    assert info["reden"]
    assert len(info["plan"]) == 6
    assert info["model"]["bron"] == "standaard"


def test_lege_planning() -> None:
    plan = make_heat_plan([], PARAMS, room_now=20.0)
    assert summary(plan, PARAMS, 20.0) == {"advies": "geen plan"}


@dataclass
class _Block:
    start_time: int
    end_time: int
    setpoint: float


def test_setpoint_uit_weekschema() -> None:
    monday = datetime(2026, 11, 16, 7, 0)
    as_dicts = {
        "monday": [
            {"start_time": 0, "end_time": 360, "setpoint": 19.5},
            {"start_time": 360, "end_time": 1350, "setpoint": 20.0},
        ]
    }
    assert setpoint_at(as_dicts, monday, 18.0) == 20.0
    assert setpoint_at(as_dicts, monday.replace(hour=3), 18.0) == 19.5
    as_objects = {"monday": [_Block(0, 360, 19.5), _Block(360, 1350, 21.0)]}
    assert setpoint_at(as_objects, monday, 18.0) == 21.0
    assert setpoint_at(as_objects, monday.replace(hour=23, minute=55), 18.0) == 18.0
    assert setpoint_at(None, monday, 18.0) == 18.0


def test_leerder_zonder_data_geeft_niets() -> None:
    assert HeatLearner().planner_inputs() == {}
