"""HEMS Batterij: planner en regellus voor de Marstek-thuisbatterijen.

Standaard kijkt de integratie alleen mee naast HomeBatteryControl. Batterijen die
in de opties als "door HEMS gestuurd" staan, worden pas aangestuurd als de
schakelaar "HEMS stuurt" aan staat.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import HemsCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.NUMBER]

type HemsConfigEntry = ConfigEntry[HemsCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: HemsConfigEntry) -> bool:
    coordinator = HemsCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HemsConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: HemsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
