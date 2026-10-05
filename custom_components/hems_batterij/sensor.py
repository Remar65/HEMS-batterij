"""Sensoren van HEMS Batterij (alleen uitlezen, niets aansturen)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import HemsConfigEntry
from .const import DOMAIN, NAME, SHADOW_MODE
from .coordinator import HemsCoordinator, HemsData
from .planner import ALL_MODES

# Hoeveel plankwartieren in de attributen (beperkt i.v.m. database-grootte).
PLAN_ATTR_STEPS = 48


@dataclass(frozen=True, kw_only=True)
class HemsSensorDescription(SensorEntityDescription):
    value_fn: Callable[[HemsData], Any]
    attrs_fn: Callable[[HemsData], dict[str, Any]] | None = None


def _mode(d: HemsData) -> str | None:
    return d.result.mode if d.result else None


def _mode_attrs(d: HemsData) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "meekijkmodus": SHADOW_MODE,
        "reden": d.result.reason if d.result else None,
        "vangnet_redenen": d.result.safety_reasons if d.result else [],
        "hbc_strategie": d.hbc_strategy,
        "beslislog": list(reversed(d.decision_log[-20:])),
    }
    if d.plan:
        attrs["plan_gemaakt"] = d.plan.created.isoformat(timespec="seconds")
        attrs["plan"] = [s.as_dict() for s in d.plan.steps[:PLAN_ATTR_STEPS]]
    return attrs


def _deviation(d: HemsData) -> float | None:
    if d.result is None or d.battery_actual_w is None:
        return None
    return round(d.battery_actual_w - d.result.total_w, 0)


def _round(value: float | None, digits: int = 0) -> float | None:
    return None if value is None else round(value, digits)


def _payback(d: HemsData) -> tuple[float | None, float | None]:
    """Besparing per jaar en terugverdientijd, geëxtrapoleerd uit de meting tot nu toe."""
    if d.saving_since is None:
        return None, None
    days = (dt_util.now() - d.saving_since).total_seconds() / 86400.0
    if days < 1.0:
        return None, None
    per_year = d.saving_total_eur / days * 365.0
    if per_year <= 0:
        return round(per_year, 0), None
    return round(per_year, 0), round(d.investment_eur / per_year, 1)


SENSORS: tuple[HemsSensorDescription, ...] = (
    HemsSensorDescription(
        key="modus",
        translation_key="modus",
        device_class=SensorDeviceClass.ENUM,
        options=ALL_MODES,
        value_fn=_mode,
        attrs_fn=_mode_attrs,
    ),
    HemsSensorDescription(
        key="voorgesteld_vermogen",
        translation_key="voorgesteld_vermogen",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: d.result.total_w if d.result else None,
        attrs_fn=lambda d: {"per_batterij": d.result.per_battery_w if d.result else {}},
    ),
    HemsSensorDescription(
        key="werkelijk_vermogen",
        translation_key="werkelijk_vermogen",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: _round(d.battery_actual_w),
        attrs_fn=lambda d: {"per_batterij": d.battery_actual_per},
    ),
    HemsSensorDescription(
        key="afwijking_hbc",
        translation_key="afwijking_hbc",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=_deviation,
    ),
    HemsSensorDescription(
        key="huisverbruik",
        translation_key="huisverbruik",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: _round(d.house_w),
        attrs_fn=lambda d: {"profiel_kwartieren_gevuld": d.profile_filled_bins, "van": 672},
    ),
    HemsSensorDescription(
        key="energie_in_batterijen",
        translation_key="energie_in_batterijen",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: _round(d.soc_total_kwh, 2),
        attrs_fn=lambda d: {"verwacht_einde_plan_kwh": round(d.plan.soc_end_kwh, 2) if d.plan else None},
    ),
    HemsSensorDescription(
        key="verwachte_besparing",
        translation_key="verwachte_besparing",
        native_unit_of_measurement="€",
        value_fn=lambda d: _round(d.expected_saving_eur, 2),
        attrs_fn=lambda d: {
            "kosten_met_plan": round(d.plan.cost_eur, 2) if d.plan else None,
            "kosten_zonder_batterij": round(d.plan.baseline_cost_eur, 2) if d.plan else None,
            "horizon_tot": d.plan.steps[-1].end.isoformat() if d.plan and d.plan.steps else None,
        },
    ),
    HemsSensorDescription(
        key="netkosten_vandaag",
        translation_key="netkosten_vandaag",
        native_unit_of_measurement="€",
        value_fn=lambda d: round(d.actual_grid_cost_today_eur, 2),
    ),
    HemsSensorDescription(
        key="besparing_vandaag",
        translation_key="besparing_vandaag",
        native_unit_of_measurement="€",
        value_fn=lambda d: round(d.baseline_cost_today_eur - d.actual_grid_cost_today_eur, 2),
        attrs_fn=lambda d: {
            "netkosten_met_batterij": round(d.actual_grid_cost_today_eur, 2),
            "netkosten_zonder_batterij": round(d.baseline_cost_today_eur, 2),
        },
    ),
    HemsSensorDescription(
        key="energie_schaduwbatterij",
        translation_key="energie_schaduwbatterij",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: _round(d.shadow_soc_kwh, 2),
        attrs_fn=lambda d: {
            "uitleg": "laadstand als HEMS de batterijen zelf had aangestuurd",
            "echte_stand_kwh": _round(d.soc_total_kwh, 2),
            "sinds": d.shadow_since.isoformat(timespec="minutes") if d.shadow_since else None,
        },
    ),
    HemsSensorDescription(
        key="besparing_hems_vandaag",
        translation_key="besparing_hems_vandaag",
        native_unit_of_measurement="€",
        value_fn=lambda d: (
            round(d.baseline_cost_today_eur - d.shadow_cost_today_eur, 2)
            if d.shadow_soc_kwh is not None
            else None
        ),
        attrs_fn=lambda d: {
            "netkosten_met_hems": round(d.shadow_cost_today_eur, 2),
            "netkosten_met_hbc": round(d.actual_grid_cost_today_eur, 2),
            "netkosten_zonder_batterij": round(d.baseline_cost_today_eur, 2),
            "hems_beter_dan_hbc": round(d.actual_grid_cost_today_eur - d.shadow_cost_today_eur, 2),
            "besparing_hems_totaal": round(d.shadow_saving_total_eur, 2),
            "sinds": d.shadow_since.isoformat(timespec="minutes") if d.shadow_since else None,
        },
    ),
    HemsSensorDescription(
        key="terugverdientijd",
        translation_key="terugverdientijd",
        native_unit_of_measurement="jaar",
        value_fn=lambda d: _payback(d)[1],
        attrs_fn=lambda d: {
            "besparing_totaal": round(d.saving_total_eur, 2),
            "gemeten_sinds": d.saving_since.isoformat(timespec="minutes") if d.saving_since else None,
            "besparing_per_jaar": _payback(d)[0],
            "investering": d.investment_eur,
            "let_op": "bij minder dan 30 dagen meting is dit een grove schatting",
        },
    ),
    HemsSensorDescription(
        key="rendement",
        translation_key="rendement",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _round(d.efficiency * 100 if d.efficiency else None, 1),
        attrs_fn=lambda d: {
            "slijtage_ct_per_kwh": _round(d.wear_eur_per_kwh * 100 if d.wear_eur_per_kwh else None, 2)
        },
    ),
    HemsSensorDescription(
        key="minimale_spread",
        translation_key="minimale_spread",
        native_unit_of_measurement="ct",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _round(d.break_even_ct, 1),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: HemsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [HemsSensor(coordinator, d) for d in SENSORS]
    entities.extend(HemsBatterySetpoint(coordinator, p) for p in coordinator.battery_prefixes)
    async_add_entities(entities)


def device_info(coordinator: HemsCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.entry.entry_id)},
        name=NAME,
        manufacturer="Eigen bouw",
        model="Planner + regellus (meekijkmodus)",
        entry_type=DeviceEntryType.SERVICE,
    )


class HemsSensor(CoordinatorEntity[HemsCoordinator], SensorEntity):
    _attr_has_entity_name = True
    # Plan en log veranderen vaak en zijn groot: niet in de database opslaan.
    _unrecorded_attributes = frozenset({"plan", "beslislog"})
    entity_description: HemsSensorDescription

    def __init__(self, coordinator: HemsCoordinator, description: HemsSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = device_info(coordinator)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class HemsBatterySetpoint(CoordinatorEntity[HemsCoordinator], SensorEntity):
    """Voorgesteld vermogen voor één batterij (+ = ontladen, - = laden)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_translation_key = "voorgesteld_vermogen_batterij"

    def __init__(self, coordinator: HemsCoordinator, prefix: str) -> None:
        super().__init__(coordinator)
        self._prefix = prefix
        self._attr_unique_id = f"{coordinator.entry.entry_id}_voorstel_{prefix}"
        self._attr_translation_placeholders = {"batterij": prefix}
        self._attr_device_info = device_info(coordinator)

    @property
    def native_value(self) -> float | None:
        result = self.coordinator.data.result
        return result.per_battery_w.get(self._prefix) if result else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"werkelijk_w": self.coordinator.data.battery_actual_per.get(self._prefix)}
