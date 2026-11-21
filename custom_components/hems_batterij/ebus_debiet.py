"""Debiet van het verwarmingswater (l/h) uit de warmtepomp, via ebusd.

Ebusd kent het bericht hmu/BuildingCircuitFlow, maar leest het niet uit zichzelf en maakt
er geen Home Assistant-entiteit van. HEMS vraagt het elk uur opnieuw aan met prioriteit 3
(zie vaillant_actuator.async_request_flow_read, alleen lezen), luistert op het MQTT-topic
en geeft de waarde door aan de sensor "Debiet verwarming" (en zo aan de meetlogger).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from . import vaillant_actuator

_LOGGER = logging.getLogger(__name__)


def parse_flow(payload: str | bytes) -> float | None:
    """Getal uit een ebusd-bericht halen: kaal getal of JSON zoals {"value": {"value": 890}}."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    text = payload.strip()
    try:
        data: Any = json.loads(text)
    except ValueError:
        return None
    while isinstance(data, dict):
        if "value" in data:
            data = data["value"]
        elif len(data) == 1:
            data = next(iter(data.values()))
        else:
            return None
    if isinstance(data, bool) or not isinstance(data, int | float):
        return None
    return float(data) if 0 <= data <= 5000 else None


class HeatingFlow:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.value: float | None = None
        self.updated: datetime | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []

    def add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    async def async_start(self) -> None:
        try:
            from homeassistant.components import mqtt

            if not await mqtt.async_wait_for_mqtt_client(self.hass):
                return
            self._unsubs.append(
                await mqtt.async_subscribe(self.hass, vaillant_actuator.FLOW_TOPIC, self._message)
            )
        except Exception as err:  # noqa: BLE001 - zonder MQTT gewoon geen debiet
            _LOGGER.debug("Debiet: abonneren op ebusd mislukt: %s", err)
            return
        self._unsubs.append(async_track_time_change(self.hass, self._async_request, minute=3, second=10))
        await self._async_request(dt_util.now())

    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def _async_request(self, now: datetime) -> None:
        try:
            await vaillant_actuator.async_request_flow_read(self.hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Debiet opvragen bij ebusd mislukt: %s", err)

    @callback
    def _message(self, msg: Any) -> None:
        value = parse_flow(msg.payload)
        if value is None:
            return
        self.value = value
        self.updated = dt_util.now()
        for update in list(self._listeners):
            update()
