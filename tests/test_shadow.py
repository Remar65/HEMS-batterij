"""Schaduwbatterij: laadstand op papier volgens HEMS' eigen voorstellen."""

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hems_batterij.const import DOMAIN
from custom_components.hems_batterij.controller import BatteryState
from custom_components.hems_batterij.shadow import ShadowBattery

from .test_init import _set_states

NOW = datetime(2026, 10, 4, 19, 0, tzinfo=UTC)


def _battery(name: str = "m1", soc: float = 50.0, eff: float = 0.81) -> BatteryState:
    return BatteryState(
        name=name,
        soc_pct=soc,
        capacity_kwh=5.12,
        ac_power_w=0.0,
        max_charge_w=2500.0,
        max_discharge_w=2500.0,
        soc_min_pct=12.0,
        soc_max_pct=100.0,
        efficiency=eff,
    )


def test_starts_at_real_soc_and_then_ignores_it() -> None:
    shadow = ShadowBattery()
    shadow.sync([_battery(soc=50.0)], NOW)
    assert abs(shadow.soc_kwh["m1"] - 2.56) < 1e-9
    assert shadow.since == NOW
    # HBC laadt de echte batterij vol; de schaduw blijft waar HEMS hem liet.
    shadow.sync([_battery(soc=100.0)], NOW + timedelta(minutes=5))
    assert abs(shadow.soc_kwh["m1"] - 2.56) < 1e-9
    applied = shadow.apply([_battery(soc=100.0)])[0]
    assert abs(applied.soc_pct - 50.0) < 1e-9
    assert applied.max_charge_w == 2500.0


def test_integrate_applies_efficiency_per_leg() -> None:
    b = _battery(eff=0.81)  # wortel = 0.9 per richting
    shadow = ShadowBattery()
    shadow.sync([b], NOW)
    shadow.integrate({"m1": -1000.0}, 3600, [b])  # 1 kWh AC laden -> 0.9 kWh erin
    assert abs(shadow.soc_kwh["m1"] - (2.56 + 0.9)) < 1e-9
    shadow.integrate({"m1": 900.0}, 3600, [b])  # 0.9 kWh AC leveren -> 1.0 kWh eruit
    assert abs(shadow.soc_kwh["m1"] - 2.46) < 1e-9


def test_integrate_respects_limits() -> None:
    b = _battery(soc=20.0)
    shadow = ShadowBattery()
    shadow.sync([b], NOW)
    shadow.integrate({"m1": 2500.0}, 3600, [b])
    assert abs(shadow.soc_kwh["m1"] - 5.12 * 0.12) < 1e-9
    shadow.integrate({"m1": -2500.0}, 4 * 3600, [b])
    assert abs(shadow.soc_kwh["m1"] - 5.12) < 1e-9


def test_roundtrip_dict_and_removed_battery() -> None:
    shadow = ShadowBattery()
    shadow.sync([_battery("m1"), _battery("m2", soc=80.0)], NOW)
    copy = ShadowBattery.from_dict(shadow.to_dict())
    assert copy.soc_kwh == shadow.soc_kwh and copy.since == NOW
    copy.sync([_battery("m1")], NOW)
    assert list(copy.soc_kwh) == ["m1"]
    assert ShadowBattery.from_dict(None).total_kwh is None


async def test_coordinator_plans_from_shadow_and_counts_shadow_cost(hass: HomeAssistant) -> None:
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"batteries": "marstek_m1, marstek_m2"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    start = coordinator.shadow.total_kwh
    assert abs(start - (0.50 + 0.45) * 5.12) < 1e-6

    # HBC laadt de echte batterijen vol; HEMS blijft plannen vanaf zijn eigen stand.
    for prefix in ("marstek_m1", "marstek_m2"):
        hass.states.async_set(f"sensor.{prefix}_battery_state_of_charge", "100")
    await coordinator.async_replan("test")
    assert abs(coordinator.shadow.total_kwh - start) < 0.05
    assert abs(coordinator._plan.steps[0].soc_start_kwh - start) < 0.05

    # Netkosten met HEMS: huis 600 W, HBC levert 500 W, HEMS zou 200 W leveren -> 400 W afname.
    now = dt_util.now()
    coordinator._last_tick = now
    before = coordinator._shadow_cost_today
    coordinator._learn(now + timedelta(seconds=36), 600, 100, 500, 200)
    step = coordinator._plan.step_at(now + timedelta(seconds=36))
    assert abs((coordinator._shadow_cost_today - before) - 400 * 36 / 3_600_000 * step.buy) < 1e-9
    assert await hass.config_entries.async_unload(entry.entry_id)
