"""Schakelaar "HEMS stuurt": de enige knop waarmee HEMS echt gaat aansturen.

Staat standaard uit. Aan betekent: de batterijen die in de opties als "door HEMS
gestuurd" staan, volgen HEMS. Uit zetten zet die batterijen meteen stil, zodat
HBC (of een terugval-automatisering) ze weer kan overnemen. De stand blijft
bewaard over een herstart heen.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HemsConfigEntry
from .coordinator import HemsCoordinator
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: HemsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([HemsControlSwitch(entry.runtime_data)])


class HemsControlSwitch(CoordinatorEntity[HemsCoordinator], SwitchEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "sturing"

    def __init__(self, coordinator: HemsCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_sturing"
        self._attr_device_info = device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            # Alleen de stand terugzetten; de eerste regelronde volgt bij het opstarten.
            self.coordinator.control_enabled = True

    @property
    def is_on(self) -> bool:
        return self.coordinator.control_enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "gestuurde_batterijen": self.coordinator.controlled_prefixes,
            "actief": self.coordinator.control_active,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control(False)
        self.async_write_ha_state()
