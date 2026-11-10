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


async def _coordinator(hass: HomeAssistant, **options):
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"batteries": "marstek_m1, marstek_m2"}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def test_helios_fields_are_summed(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, pv_source="helios")
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
    coordinator = await _coordinator(hass, pv_source="helios")
    aps, enphase = DEFAULTS["helios_entities"]
    _helios(hass, aps, 2000)
    hass.states.async_set(enphase, "unavailable", {})
    _solcast(hass, 5.0)
    now = dt_util.now()
    periods, source = coordinator._pv_periods(now)
    assert source.startswith("solcast")
    assert enphase in source
    start = now.replace(minute=0, second=0, microsecond=0)
    # Solcast-terugval met de standaard correctiefactor 0,7.
    assert abs(pv_kwh_between(periods, start, start + timedelta(hours=1)) - 3.5) < 1e-9


async def test_solcast_factor_is_configurable(hass: HomeAssistant) -> None:
    _set_states(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"batteries": "marstek_m1, marstek_m2"},
        options={"pv_source": "solcast", "solcast_factor": 1.0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    _solcast(hass, 5.0)
    now = dt_util.now()
    periods, _ = entry.runtime_data._pv_periods(now)
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
    _solcast(hass, 5.0)
    _, source = entry.runtime_data._pv_periods(dt_util.now())
    assert source == "solcast"


async def test_solcast_is_the_default_source(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    aps, enphase = DEFAULTS["helios_entities"]
    _helios(hass, aps, 2000)
    _helios(hass, enphase, 1000)
    _solcast(hass, 5.0)
    now = dt_util.now()
    periods, source = coordinator._pv_periods(now)
    assert source == "solcast"
    start = now.replace(minute=0, second=0, microsecond=0)
    # Nog niets geleerd: de vaste Solcast-factor 0,7.
    assert abs(pv_kwh_between(periods, start, start + timedelta(hours=1)) - 3.5) < 1e-9


async def test_falls_back_to_helios_without_solcast(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    aps, enphase = DEFAULTS["helios_entities"]
    _helios(hass, aps, 2000)
    _helios(hass, enphase, 1000)
    hass.states.async_set(DEFAULTS["solcast_today"], "unavailable", {})
    periods, source = coordinator._pv_periods(dt_util.now())
    assert source.startswith("helios")
    assert "terugval" in source


async def test_learned_hour_factor_replaces_fixed_factor(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    _solcast(hass, 5.0)
    now = dt_util.now()
    start = now.replace(minute=0, second=0, microsecond=0)
    for days_ago in range(1, 8):
        coordinator.pv_correction.record("solcast", start - timedelta(days=days_ago), 2.0, 5.0)
    periods, _ = coordinator._pv_periods(now)
    # Geleerd: 2 van de 5 kWh -> 0,4 in plaats van 0,7.
    assert abs(pv_kwh_between(periods, start, start + timedelta(hours=1)) - 2.0) < 1e-9
    # Het uur erna heeft nog niets geleerd en houdt de vaste factor.
    nxt = start + timedelta(hours=1)
    assert abs(pv_kwh_between(periods, nxt, nxt + timedelta(minutes=30)) - 1.75) < 1e-9


async def test_hour_correction_can_be_switched_off(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, pv_hour_correction=False)
    _solcast(hass, 5.0)
    now = dt_util.now()
    start = now.replace(minute=0, second=0, microsecond=0)
    for days_ago in range(1, 8):
        coordinator.pv_correction.record("solcast", start - timedelta(days=days_ago), 2.0, 5.0)
    periods, _ = coordinator._pv_periods(now)
    assert abs(pv_kwh_between(periods, start, start + timedelta(hours=1)) - 3.5) < 1e-9


async def test_learns_actual_against_forecast_per_hour(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    aps, enphase = DEFAULTS["helios_entities"]
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    _solcast(hass, 4.0)
    _helios(hass, aps, 1000)
    _helios(hass, enphase, 1000)
    coordinator._pv_hour_start = None
    coordinator.pv_correction = type(coordinator.pv_correction)()
    # Een heel uur 1 kW zon gemeten (elke minuut een meting).
    for minute in range(60):
        coordinator._learn_pv(hour + timedelta(minutes=minute), 1000.0, 60.0)
    coordinator._learn_pv(hour + timedelta(hours=1), 1000.0, 60.0)
    days = coordinator.pv_correction.to_dict()
    solcast = days["solcast"][hour.date().isoformat()][str(hour.hour)]
    helios = days["helios"][hour.date().isoformat()][str(hour.hour)]
    assert abs(solcast[0] - 1.0) < 1e-6 and abs(solcast[1] - 4.0) < 1e-6
    assert abs(helios[0] - 1.0) < 1e-6 and abs(helios[1] - 2.0) < 1e-6
