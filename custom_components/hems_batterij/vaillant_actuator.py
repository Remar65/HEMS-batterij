"""De enige plek waar HEMS iets naar de Vaillant stuurt: de ruimte-invloed via ebusd.

Twee dingen, allebei via MQTT naar ebusd:

- Opvragen (elk uur): ebusd leest de stand van de kamerbijschakeling (Hc1RoomTempModulation)
  uit zichzelf maar één keer bij het opstarten. Een leesverzoek op .../get met "?3" zet het
  bericht in de pollijst van ebusd (prioriteit 3), zodat hij de VRC voortaan regelmatig
  uitleest. Dit schrijft niets naar de Vaillant.
- Zetten (alleen met de schakelaar "Ruimte-invloed automatisch" aan, standaard uit):
  Actief of Uitgebreid naar .../set. Dat werkt pas als ebusd schrijven toestaat voor dit
  bericht; staat ebusd op alleen lezen, dan verandert er niets en ziet HEMS dat bij het
  volgende opvragen.

Daarnaast vraagt HEMS elk uur het debiet van het verwarmingswater op (hmu/BuildingCircuitFlow,
ook alleen via .../get, dus alleen lezen); zie ebus_debiet.py.

Andere standen (inactief) en andere berichten kunnen hier niet worden verstuurd.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .ruimte_invloed import ACTIVE, EXPANDED

_LOGGER = logging.getLogger(__name__)

TOPIC = "ebusd/basv2/Hc1RoomTempModulation"
ALLOWED_VALUES = (ACTIVE, EXPANDED)
POLL_PAYLOAD = "?3"  # lezen en voortaan pollen met prioriteit 3
FLOW_TOPIC = "ebusd/hmu/BuildingCircuitFlow"


async def async_request_read(hass: HomeAssistant) -> bool:
    """Ebusd de stand opnieuw laten uitlezen."""
    if not hass.services.has_service("mqtt", "publish"):
        return False
    await hass.services.async_call(
        "mqtt", "publish", {"topic": f"{TOPIC}/get", "payload": POLL_PAYLOAD}, blocking=True
    )
    return True


async def async_request_flow_read(hass: HomeAssistant) -> bool:
    """Ebusd het debiet van het verwarmingswater laten uitlezen (alleen lezen)."""
    if not hass.services.has_service("mqtt", "publish"):
        return False
    await hass.services.async_call(
        "mqtt", "publish", {"topic": f"{FLOW_TOPIC}/get", "payload": POLL_PAYLOAD}, blocking=True
    )
    return True


async def async_set_room_influence(hass: HomeAssistant, value: str) -> bool:
    """Actief of Uitgebreid naar de VRC sturen."""
    if value not in ALLOWED_VALUES:
        raise ValueError(f"ruimte-invloed {value!r} niet toegestaan")
    if not hass.services.has_service("mqtt", "publish"):
        return False
    _LOGGER.info("HEMS zet de ruimte-invloed van de VRC op %s", value)
    await hass.services.async_call(
        "mqtt", "publish", {"topic": f"{TOPIC}/set", "payload": value}, blocking=True
    )
    return True
