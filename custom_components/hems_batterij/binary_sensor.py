"""Binaire sensoren: meekijkmodus en vangnet."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HemsConfigEntry
from .const import SHADOW_MODE
from .coordinator import HemsCoordinator
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: HemsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([HemsShadowMode(coordinator), HemsSafety(coordinator)])


class _Base(CoordinatorEntity[HemsCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HemsCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = device_info(coordinator)


class HemsShadowMode(_Base):
    """Staat aan zolang de integratie alleen meekijkt (in deze versie altijd)."""

    def __init__(self, coordinator: HemsCoordinator) -> None:
        super().__init__(coordinator, "meekijkmodus")

    @property
    def is_on(self) -> bool:
        return SHADOW_MODE


class HemsSafety(_Base):
    """Aan als een vangnet ingrijpt of als er iets ontbreekt."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: HemsCoordinator) -> None:
        super().__init__(coordinator, "vangnet")

    @property
    def is_on(self) -> bool | None:
        result = self.coordinator.data.result
        return bool(result.safety_reasons) if result else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self.coordinator.data.result
        return {"redenen": result.safety_reasons if result else []}
