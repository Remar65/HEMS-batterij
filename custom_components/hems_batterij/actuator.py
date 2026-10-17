"""Aansturing van een Marstek via Modbus (ESPHome LilyGo).

Dit is de enige plek in de integratie die services aanroept. Er wordt alleen
gestuurd naar batterijen die in de opties als "door HEMS gestuurd" staan, en
alleen zolang de schakelaar "HEMS stuurt" aan staat (dat bewaakt de coördinator).

Een opdracht is een richting (stop / charge / discharge) plus een vermogen. Er
wordt alleen geschreven als de batterij nog iets anders doet dan gevraagd, zodat
de Modbus-bus niet elke vijf seconden volloopt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONTROL_CHARGE,
    CONTROL_DISCHARGE,
    CONTROL_ENTITY_PATTERNS,
    CONTROL_MIN_W,
    CONTROL_RESEND_SECONDS,
    CONTROL_STOP,
    CONTROL_WRITE_DELTA_W,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Command:
    mode: str
    power_w: int  # altijd >= 0; richting zit in mode


def command_for(watt: float, max_w: float, min_w: float = CONTROL_MIN_W) -> Command:
    """Van gewenst AC-vermogen (+ = ontladen, - = laden) naar een Marstek-opdracht."""
    if abs(watt) < min_w:
        return Command(CONTROL_STOP, 0)
    power = int(round(min(abs(watt), max_w)))
    return Command(CONTROL_DISCHARGE if watt > 0 else CONTROL_CHARGE, power)


def writes_needed(
    wanted: Command,
    current_mode: str | None,
    current_charge_w: float | None,
    current_discharge_w: float | None,
    delta_w: float = CONTROL_WRITE_DELTA_W,
) -> list[tuple[str, str | int]]:
    """Welke entiteiten moeten worden bijgewerkt, in volgorde: eerst vermogen, dan richting.

    Geeft paren (sleutel uit CONTROL_ENTITY_PATTERNS, waarde).
    """
    writes: list[tuple[str, str | int]] = []
    if wanted.mode == CONTROL_CHARGE:
        if current_charge_w is None or abs(current_charge_w - wanted.power_w) >= delta_w:
            writes.append(("charge_w", wanted.power_w))
    elif wanted.mode == CONTROL_DISCHARGE:
        if current_discharge_w is None or abs(current_discharge_w - wanted.power_w) >= delta_w:
            writes.append(("discharge_w", wanted.power_w))
    if current_mode != wanted.mode:
        writes.append(("mode", wanted.mode))
    return writes


def _float(value: str | None) -> float | None:
    if value in (None, "", STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MarstekActuator:
    """Stuurt één Marstek; onthoudt wat het het laatst stuurde."""

    def __init__(self, hass: HomeAssistant, prefix: str) -> None:
        self.hass = hass
        self.prefix = prefix
        self.entities = {k: v.format(p=prefix) for k, v in CONTROL_ENTITY_PATTERNS.items()}
        self._last_sent: dict[str, tuple[str | int, datetime]] = {}
        self.last_command: Command | None = None
        self.last_error: str | None = None

    def _current(self, key: str) -> str | None:
        state = self.hass.states.get(self.entities[key])
        return state.state if state else None

    async def async_apply(self, watt: float, max_w: float, now: datetime) -> Command:
        wanted = command_for(watt, max_w)
        mode = self._current("mode")
        writes = writes_needed(
            wanted,
            None if mode in (None, STATE_UNAVAILABLE, STATE_UNKNOWN) else mode,
            _float(self._current("charge_w")),
            _float(self._current("discharge_w")),
        )
        for key, value in writes:
            sent = self._last_sent.get(key)
            if sent and sent[0] == value and (now - sent[1]).total_seconds() < CONTROL_RESEND_SECONDS:
                # Net al gestuurd; ESPHome heeft de nieuwe waarde nog niet teruggemeld.
                continue
            await self._async_write(key, value)
            self._last_sent[key] = (value, now)
        self.last_command = wanted
        return wanted

    async def async_stop(self, now: datetime) -> None:
        """Batterij stilzetten (bij uitzetten van HEMS-sturing, vangnet of afsluiten)."""
        self._last_sent.clear()
        await self.async_apply(0.0, 0.0, now)

    async def _async_write(self, key: str, value: str | int) -> None:
        entity_id = self.entities[key]
        if key == "mode":
            domain, service, data = "select", "select_option", {"option": value}
        else:
            domain, service, data = "number", "set_value", {"value": value}
        try:
            await self.hass.services.async_call(
                domain, service, {"entity_id": entity_id, **data}, blocking=True
            )
            self.last_error = None
            _LOGGER.debug("HEMS Batterij stuurt %s = %s", entity_id, value)
        except (HomeAssistantError, ValueError) as err:
            self.last_error = f"{entity_id}: {err}"
            _LOGGER.warning("HEMS Batterij kon %s niet op %s zetten: %s", entity_id, value, err)
