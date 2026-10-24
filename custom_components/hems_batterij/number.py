"""Getal "Nu laden tot": tot welk percentage de knop "Nu laden" de batterijen laadt."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HemsConfigEntry
from .coordinator import HemsCoordinator
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: HemsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([HemsForceChargeTarget(entry.runtime_data)])


class HemsForceChargeTarget(CoordinatorEntity[HemsCoordinator], RestoreNumber):
    _attr_has_entity_name = True
    _attr_translation_key = "nu_laden_tot"
    _attr_icon = "mdi:battery-arrow-up"
    _attr_native_min_value = 20
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: HemsCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_nu_laden_tot"
        self._attr_device_info = device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self.coordinator.force_charge_target_pct = float(last.native_value)

    @property
    def native_value(self) -> float:
        return self.coordinator.force_charge_target_pct

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.force_charge_target_pct = float(value)
        self.async_write_ha_state()
