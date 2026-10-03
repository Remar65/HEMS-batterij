"""Integratietest in een echte (test-)Home Assistant."""

from datetime import timedelta

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hems_batterij.const import DEFAULTS, DOMAIN


def _price_forecast():
    start = dt_util.now().replace(minute=0, second=0, microsecond=0)
    items = []
    for i in range(48):
        s = start + timedelta(minutes=15 * i)
        amount = 1_500_000 if i < 16 else 4_500_000  # eerst 15 ct, daarna 45 ct
        items.append(
            {
                "start_date": s.isoformat(),
                "end_date": (s + timedelta(minutes=15)).isoformat(),
                "price_tax_included": {"amount": amount},
                "price_tax_excluded": {"amount": amount // 2},
            }
        )
    return items


def _set_states(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.p1_meter_power", "350")
    hass.states.async_set("sensor.kwh_meter_power", "-500")
    hass.states.async_set("sensor.kwh_meter_power_2", "-300")
    hass.states.async_set(DEFAULTS["price_sensor"], "0.15", {"forecast": _price_forecast()})
    hass.states.async_set(DEFAULTS["solcast_today"], "10", {"detailedForecast": []})
    hass.states.async_set(DEFAULTS["solcast_tomorrow"], "10", {"detailedForecast": []})
    hass.states.async_set("input_number.batterij_slijtagekosten", "4.7")
    hass.states.async_set("input_select.house_battery_strategy", "Dynamic 2")
    for prefix, soc, eff in (("marstek_m1", "50", "79.5"), ("marstek_m2", "45", "83.1")):
        hass.states.async_set(f"sensor.{prefix}_battery_state_of_charge", soc)
        hass.states.async_set(f"sensor.{prefix}_ac_power", "0")
        hass.states.async_set(f"sensor.{prefix}_battery_total_energy", "5.12")
        hass.states.async_set(f"sensor.{prefix}_lifetime_round_trip_efficiency", eff)
        hass.states.async_set(f"number.{prefix}_max_charge_power", "2500")
        hass.states.async_set(f"number.{prefix}_max_discharge_power", "2500")
        hass.states.async_set(f"number.{prefix}_discharging_cutoff_capacity", "12")
        hass.states.async_set(f"number.{prefix}_charging_cutoff_capacity", "100")
        hass.states.async_set(f"binary_sensor.{prefix}_bms_protect", "off")


async def test_setup_creates_sensors_and_never_calls_services(hass: HomeAssistant) -> None:
    _set_states(hass)
    calls = []
    hass.bus.async_listen(EVENT_CALL_SERVICE, lambda event: calls.append(event.data))

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            k: DEFAULTS[k]
            for k in (
                "p1_power",
                "pv_power",
                "pv_inverted",
                "price_sensor",
                "solcast_today",
                "solcast_tomorrow",
                "batteries",
            )
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)

    def state_of(key: str):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
        assert entity_id, key
        return hass.states.get(entity_id)

    mode = state_of("modus")
    assert mode.state in ("zelfverbruik", "vasthouden", "net_laden")
    assert mode.attributes["meekijkmodus"] is True
    assert mode.attributes["plan"]
    assert mode.attributes["beslislog"]
    # Huis = P1 350 + zon 800 + batterij 0
    assert float(state_of("huisverbruik").state) == 1150
    assert float(state_of("verwachte_besparing").state) > 0
    assert state_of("voorstel_marstek_m1") is not None
    assert state_of("besparing_vandaag") is not None
    assert state_of("terugverdientijd").attributes["investering"] == 2625.0

    shadow = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_meekijkmodus")
    assert hass.states.get(shadow).state == "on"

    # Kern van de meekijkmodus: geen enkele service-aanroep door de integratie.
    assert calls == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_missing_prices_gives_fallback(hass: HomeAssistant) -> None:
    _set_states(hass)
    hass.states.async_set(DEFAULTS["price_sensor"], "unknown", {})
    entry = MockConfigEntry(domain=DOMAIN, data={"batteries": "marstek_m1, marstek_m2"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_modus")
    state = hass.states.get(entity_id)
    assert state.state == "zelfverbruik"
    assert "geen plan" in state.attributes["reden"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_saving_counts_battery_contribution(hass: HomeAssistant) -> None:
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"batteries": "marstek_m1, marstek_m2"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    start_saving = coordinator._saving_total
    now = dt_util.now()
    # Batterij levert 500 W, net nog 100 W afname: zonder batterij was het 600 W afname.
    coordinator._last_tick = now
    coordinator._learn(now + timedelta(seconds=36), 600, 100, 500)
    step = coordinator._plan.step_at(now + timedelta(seconds=36))
    expected = 500 * 36 / 3_600_000 * step.buy
    assert abs((coordinator._saving_total - start_saving) - expected) < 1e-9
    assert await hass.config_entries.async_unload(entry.entry_id)
