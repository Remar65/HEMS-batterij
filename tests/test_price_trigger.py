"""Opnieuw plannen bij nieuwe Zonneplan-prijzen, niet bij het wegvallen van verstreken uren."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hems_batterij.const import DEFAULTS, DOMAIN

from .test_init import _price_forecast, _set_states


async def _coordinator(hass: HomeAssistant):
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"batteries": "marstek_m1, marstek_m2"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def test_dropping_past_slots_does_not_replan(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    sensor = DEFAULTS["price_sensor"]
    items = _price_forecast()
    with patch.object(coordinator, "async_replan", AsyncMock()) as replan:
        # Eerste keer: eindtijd nog onbekend, dus plannen.
        hass.states.async_set(sensor, "0.14", {"forecast": items})
        await hass.async_block_till_done()
        assert replan.await_count == 1
        # Zonneplan laat het verstreken uur vallen (zelfde eindtijd): geen nieuw plan.
        hass.states.async_set(sensor, "0.16", {"forecast": items[4:]})
        await hass.async_block_till_done()
        assert replan.await_count == 1
        # De prijzen van morgen komen erbij: wel opnieuw plannen.
        later = _price_forecast()[:4]
        for item in later:
            item["start_date"] = item["start_date"].replace(item["start_date"][:10], "2099-01-01")
            item["end_date"] = item["end_date"].replace(item["end_date"][:10], "2099-01-01")
        hass.states.async_set(sensor, "0.16", {"forecast": items[4:] + later})
        await hass.async_block_till_done()
        assert replan.await_count == 2
        replan.assert_awaited_with("nieuwe prijzen")
