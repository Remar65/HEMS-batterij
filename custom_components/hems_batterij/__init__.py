"""HEMS Batterij: planner en regellus voor de Marstek-thuisbatterijen.

Standaard kijkt de integratie alleen mee naast HomeBatteryControl. Batterijen die
in de opties als "door HEMS gestuurd" staan, worden pas aangestuurd als de
schakelaar "HEMS stuurt" aan staat.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import meetlogger
from .const import DOMAIN
from .coordinator import HemsCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.NUMBER]

type HemsConfigEntry = ConfigEntry[HemsCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: HemsConfigEntry) -> bool:
    coordinator = HemsCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    async def _log(now: datetime) -> None:
        await meetlogger.async_log_measurement(hass, now)

    entry.async_on_unload(async_track_time_change(hass, _log, minute=[0, 15, 30, 45], second=20))
    _register_services(hass)
    return True


SERVICE_EXPORT = "meetdata_exporteren"
EXPORT_SCHEMA = vol.Schema(
    {
        vol.Optional("start"): cv.datetime,
        vol.Optional("einde"): cv.datetime,
        vol.Optional("kolommen"): vol.All(cv.ensure_list, [vol.In(meetlogger.HEADER)]),
    }
)


def _local(value: datetime) -> datetime:
    return dt_util.as_local(value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone()))


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_EXPORT):
        return

    async def _export(call: ServiceCall) -> ServiceResponse:
        """Meetdata verwarming (kwartierregels) teruggeven; standaard de laatste 7 dagen."""
        end = _local(call.data["einde"]) if "einde" in call.data else dt_util.now()
        start = _local(call.data["start"]) if "start" in call.data else end - timedelta(days=7)
        return await hass.async_add_executor_job(
            meetlogger.read_rows, meetlogger.folder(hass), start, end, call.data.get("kolommen")
        )

    hass.services.async_register(
        DOMAIN, SERVICE_EXPORT, _export, schema=EXPORT_SCHEMA, supports_response=SupportsResponse.ONLY
    )


async def async_unload_entry(hass: HomeAssistant, entry: HemsConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: HemsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
