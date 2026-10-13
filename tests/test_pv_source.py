"""Zonbron: Helios als hij compleet is, anders Solcast."""

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hems_batterij.const import DEFAULTS, DOMAIN
from custom_components.hems_batterij.forecast import pv_kwh_between

from .test_init import _set_states


def _helios(hass: HomeAssistant, entity_id: str, watts: float) -> None:
    start = dt_util.now().replace(minute=0, second=0, microsecond=0)
    forecast = [
        {
            "datetime": (start + timedelta(minutes=15 * i)).isoformat(),
            "watts": watts,
            "p10": None,
            "p90": None,
        }
        for i in range(8)
    ]
    hass.states.async_set(entity_id, "0", {"forecast": forecast, "unit_of_measurement": "W"})


def _solcast(hass: HomeAssistant, kw: float) -> None:
    start = dt_util.now().replace(minute=0, second=0, microsecond=0)
    detailed = [
        {"period_start": (start + timedelta(minutes=30 * i)).isoformat(), "pv_estimate": kw} for i in range(4)
    ]
    hass.states.async_set(DEFAULTS["solcast_today"], "10", {"detailedForecast": detailed})


async def _coordinator(hass: HomeAssistant):
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"batteries": "marstek_m1, marstek_m2"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def test_helios_fields_are_summed(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    aps, enphase = DEFAULTS["helios_entities"]
    _helios(hass, aps, 2000)
    _helios(hass, enphase, 1000)
    _solcast(hass, 5.0)
    now = dt_util.now()
    periods, source = coordinator._pv_periods(now)
    assert source == "helios"
    start = now.replace(minute=0, second=0, microsecond=0)
    assert abs(pv_kwh_between(periods, start, start + timedelta(hours=1)) - 3.0) < 1e-9


async def test_falls_back_to_solcast_when_a_field_is_missing(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    aps, enphase = DEFAULTS["helios_entities"]
    _helios(hass, aps, 2000)
    hass.states.async_set(enphase, "unavailable", {})
    _solcast(hass, 5.0)
    now = dt_util.now()
    periods, source = coordinator._pv_periods(now)
    assert source.startswith("solcast")
    assert enphase in source
    start = now.replace(minute=0, second=0, microsecond=0)
    assert abs(pv_kwh_between(periods, start, start + timedelta(hours=1)) - 5.0) < 1e-9


async def test_solcast_when_chosen(hass: HomeAssistant) -> None:
    _set_states(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, data={"batteries": "marstek_m1, marstek_m2"}, options={"pv_source": "solcast"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    aps, enphase = DEFAULTS["helios_entities"]
    _helios(hass, aps, 2000)
    _helios(hass, enphase, 1000)
    _, source = entry.runtime_data._pv_periods(dt_util.now())
    assert source == "solcast"
