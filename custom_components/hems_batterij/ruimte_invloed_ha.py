"""Ruimte-invloed in Home Assistant: elk uur leren, advies geven en eventueel zetten.

Leest de kwartierregels van de meetlogger (zie meetlogger.py), laat ruimte_invloed.py
daaruit leren en kijkt met de weersverwachting van de coördinator vooruit. Wijkt het
advies af van de huidige stand, dan komt er één melding in Home Assistant. Staat de
schakelaar "Ruimte-invloed automatisch" aan (standaard uit), dan zet HEMS de stand zelf,
maar alleen met een betrouwbaar advies, hooguit eens per 12 uur en niet tijdens het stoken.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.util import dt as dt_util

from . import meetlogger, ruimte_invloed as ri, vaillant_actuator

_LOGGER = logging.getLogger(__name__)

SETTING_ENTITY = "sensor.heating_ebusd_basv2_hc1roomtempmodulation"
LEARN_DAYS = 45
LOOKAHEAD_HOURS = 12
MIN_SWITCH_INTERVAL = timedelta(hours=12)
NOTIFICATION_ID = "hems_ruimte_invloed"
START_DELAY_SECONDS = 180
COLUMNS = [
    "ruimte_invloed",
    "vrc_setpoint_c",
    "vrc_kamer_c",
    "keuken_c",
    "buiten_gem_c",
    "wind_kmh",
    "pv_aiko_w",
    "pv_jinko_w",
    "kachel_c",
]


class RoomInfluence:
    def __init__(self, hass: HomeAssistant, coordinator: Any) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.auto_enabled = False
        self.data: dict[str, Any] = {}
        self.advice: str | None = None
        self._notified: str | None = None
        self._last_switch: datetime | None = None
        self._unsubs: list[Callable[[], None]] = []

    async def async_start(self) -> None:
        self._unsubs.append(async_track_time_change(self.hass, self._async_refresh, minute=2, second=40))
        self._unsubs.append(async_track_time_change(self.hass, self.async_update, minute=10, second=10))
        await self._async_refresh(dt_util.now())
        # Eerste beoordeling pas als ebusd de actuele stand heeft teruggemeld, anders volgt een
        # melding op een verouderde stand.
        self._unsubs.append(async_call_later(self.hass, START_DELAY_SECONDS, self.async_update))

    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def _async_refresh(self, now: datetime) -> None:
        try:
            await vaillant_actuator.async_request_read(self.hass)
        except Exception as err:  # noqa: BLE001 - opvragen mag nooit de integratie breken
            _LOGGER.debug("Ruimte-invloed opvragen bij ebusd mislukt: %s", err)

    def current_setting(self) -> str | None:
        state = self.hass.states.get(SETTING_ENTITY)
        return state.state if state is not None and state.state in ri.SETTINGS else None

    def _forecast(self, now: datetime) -> dict[str, float] | None:
        temps: dict[datetime, float] = getattr(self.coordinator, "_temps", {}) or {}
        winds: dict[datetime, float] = getattr(self.coordinator, "_wind", {}) or {}
        start = dt_util.as_utc(now).replace(minute=0, second=0, microsecond=0)
        hours = [start + timedelta(hours=h) for h in range(1, LOOKAHEAD_HOURS + 1)]
        t = [temps[h] for h in hours if h in temps]
        w = [winds[h] for h in hours if h in winds]
        if len(t) < LOOKAHEAD_HOURS // 2:
            return None
        return {"buiten_c": round(sum(t) / len(t), 1), "wind_kmh": round(sum(w) / len(w), 1) if w else 0.0}

    async def async_update(self, now: datetime) -> None:
        local = dt_util.as_local(now)
        try:
            raw = await self.hass.async_add_executor_job(
                meetlogger.read_rows,
                meetlogger.folder(self.hass),
                local - timedelta(days=LEARN_DAYS),
                local,
                COLUMNS,
                LEARN_DAYS * 96 + 200,
            )
        except OSError as err:
            _LOGGER.warning("Ruimte-invloed: meetdata niet leesbaar: %s", err)
            return
        quarters = ri.quarters_from_rows(raw["kolommen"], raw["rijen"])
        blocks = ri.make_blocks(quarters, local)
        fitted = ri.fit(blocks)
        current = self.current_setting()
        forecast = self._forecast(now)
        advice: str | None = None
        t_eff: float | None = None
        if forecast is not None:
            advice, t_eff = ri.advise(current, fitted, forecast["buiten_c"], forecast["wind_kmh"])
        self.advice = advice
        stove_now = bool(quarters) and ri.stove_flags(quarters[-25:])[-1]
        self.data = ri.summary(blocks, fitted, current, advice, t_eff, forecast)
        self.data["automatisch"] = self.auto_enabled
        self.data["laatst_gezet"] = self._last_switch.isoformat() if self._last_switch else None
        self.data["kachel_nu"] = stove_now
        self.data["berekend"] = local.isoformat(timespec="minutes")
        await self._async_act(now, current, advice, fitted.reliable, stove_now)

    async def _async_act(
        self, now: datetime, current: str | None, advice: str | None, reliable: bool, stove_now: bool
    ) -> None:
        if advice is None or current is None or advice == current:
            if advice == current and self._notified:
                persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)
                self._notified = None
            return
        if (
            self.auto_enabled
            and reliable
            and not stove_now
            and (self._last_switch is None or now - self._last_switch >= MIN_SWITCH_INTERVAL)
        ):
            try:
                if await vaillant_actuator.async_set_room_influence(self.hass, advice):
                    self._last_switch = now
                    await vaillant_actuator.async_request_read(self.hass)
                    self.data["laatst_gezet"] = now.isoformat()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Ruimte-invloed zetten mislukt: %s", err)
            return
        if self._notified == advice:
            return
        self._notified = advice
        name = ri.NAMES.get(advice, advice)
        reden = self.data.get("basis", "")
        persistent_notification.async_create(
            self.hass,
            (
                f"HEMS adviseert de ruimte-invloed van de VRC op **{name}** te zetten "
                f"(nu {ri.NAMES.get(current, current)}). Gevoelstemperatuur komende "
                f"{LOOKAHEAD_HOURS} uur: {self.data.get('gevoelstemperatuur_komend_c')} °C, "
                f"drempel {self.data.get('drempel_c')} °C. Basis: {reden}."
            ),
            title="Ruimte-invloed VRC",
            notification_id=NOTIFICATION_ID,
        )
