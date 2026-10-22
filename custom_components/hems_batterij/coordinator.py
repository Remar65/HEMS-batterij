"""Coördinator: leest Home Assistant, maakt het plan en draait de regellus.

Standaard kijkt de integratie alleen mee: dan wordt niets naar de batterijen
gestuurd. Pas als er in de opties batterijen als "door HEMS gestuurd" staan én de
schakelaar "HEMS stuurt" aan staat, stuurt de regellus die batterijen via
actuator.py. De overige batterijen blijven bij HBC; hun vermogen telt dan als
"andere regelaar" mee. De vergelijking draait in die stand om: HEMS echt tegen
een schaduw-HBC op papier (zie shadow_hbc.py).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .actuator import Command, MarstekActuator
from .const import (
    BATTERY_ALARM_SUFFIXES,
    BATTERY_ENTITY_PATTERNS,
    BATTERY_HEARTBEAT_PATTERNS,
    CONF_BATTERIES,
    CONF_CONTROLLED,
    CONF_DEFAULT_EFFICIENCY,
    CONF_DEFAULT_LOAD_W,
    CONF_FEED_IN_COST_CT,
    CONF_HELIOS_ENTITIES,
    CONF_INVESTMENT_EUR,
    CONF_NETTING_ACTIVE,
    CONF_NETTING_END,
    CONF_P1_POWER,
    CONF_PRICE_SENSOR,
    CONF_PV_INVERTED,
    CONF_PV_POWER,
    CONF_PV_SOURCE,
    CONF_SOLAR_FIRST_CT,
    CONF_SOLCAST_FIELD,
    CONF_SOLCAST_TODAY,
    CONF_SOLCAST_TOMORROW,
    CONF_TRADE_MARGIN_CT,
    CONF_WEAR_CT,
    CONF_WEAR_ENTITY,
    DECISION_LOG_SIZE,
    DEFAULTS,
    DOMAIN,
    FAST_LOOP_SECONDS,
    HBC_CHARGE_TARGET_SOC_ENTITY,
    HBC_CUTOFF_PATTERNS,
    HBC_SELL_TARGET_SOC_ENTITY,
    HBC_SUB_STRATEGY_ENTITY,
    PLAN_STEP_KWH,
    PV_SOURCE_HELIOS,
    PV_SOURCE_SOLCAST,
    RS485_DISABLED,
    STALE_BATTERY_SECONDS,
    STALE_P1_SECONDS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .controller import (
    BatteryState,
    ControllerSettings,
    ControlResult,
    ControlTarget,
    control_step,
)
from .forecast import (
    LoadProfile,
    PvPeriod,
    parse_helios_forecast,
    parse_solcast_detailed,
    pv_kwh_between,
)
from .planner import (
    MODE_GRID_CHARGE,
    MODE_SAFETY,
    MODE_SELF_CONSUMPTION,
    BatteryModel,
    Plan,
    PlanSlot,
    PlanStep,
    Priorities,
    break_even_spread,
    make_plan,
)
from .prices import PriceSlot, parse_zonneplan_forecast, upcoming_slots
from .shadow import ShadowBattery
from .shadow_hbc import HbcTargets, hbc_counteracts, hbc_step

_LOGGER = logging.getLogger(__name__)

HBC_STRATEGY_ENTITY = "input_select.house_battery_strategy"

# Een opgeslagen schaduwstand die ouder is dan dit, is na een herstart niet meer
# te vertrouwen: dan opnieuw beginnen bij de echte stand.
SHADOW_MAX_RESTORE_SECONDS = 2 * 3600


@dataclass
class HemsData:
    """Alles wat de sensoren tonen."""

    result: ControlResult | None = None
    plan: Plan | None = None
    house_w: float | None = None
    p1_w: float | None = None
    pv_w: float | None = None
    battery_actual_w: float | None = None
    battery_actual_per: dict[str, float | None] = field(default_factory=dict)
    soc_total_kwh: float | None = None
    efficiency: float | None = None
    wear_eur_per_kwh: float | None = None
    break_even_ct: float | None = None
    expected_saving_eur: float | None = None
    actual_grid_cost_today_eur: float = 0.0
    baseline_cost_today_eur: float = 0.0
    saving_total_eur: float = 0.0
    saving_since: datetime | None = None
    investment_eur: float = 0.0
    profile_filled_bins: int = 0
    hbc_strategy: str | None = None
    decision_log: list[dict[str, Any]] = field(default_factory=list)
    shadow_soc_kwh: float | None = None
    shadow_cost_today_eur: float = 0.0
    shadow_saving_total_eur: float = 0.0
    shadow_since: datetime | None = None
    shadow_result: ControlResult | None = None
    pv_source: str | None = None
    price_source: str | None = None
    control_active: bool = False
    controlled: list[str] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    control_errors: list[str] = field(default_factory=list)
    other_battery_w: float | None = None
    hems_saving_total_eur: float = 0.0


def _target(step: PlanStep | None) -> ControlTarget | None:
    if step is None:
        return None
    return ControlTarget(
        mode=step.mode,
        fixed_w=step.fixed_w,
        max_charge_w=step.max_charge_w,
        max_discharge_w=step.max_discharge_w,
        reason=step.reason,
    )


def _float_state(state: State | None) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, "", None):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


class HemsCoordinator(DataUpdateCoordinator[HemsData]):
    """Plant per kwartier en rekent elke paar seconden het gewenste vermogen uit."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None, config_entry=entry)
        self.entry = entry
        self.settings = ControllerSettings()
        self._last_prices: list[PriceSlot] = []
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self._unsubs: list[CALLBACK_TYPE] = []
        self._log: deque[dict[str, Any]] = deque(maxlen=DECISION_LOG_SIZE)
        self.profile = LoadProfile(default_w=float(self._conf(CONF_DEFAULT_LOAD_W)))
        self._plan: Plan | None = None
        self._previous_total: float | None = None
        self._last_mode: str | None = None
        self._last_tick: datetime | None = None
        self._quarter_start: datetime | None = None
        self._quarter_energy_wh = 0.0
        self._quarter_seconds = 0.0
        self._cost_day: date | None = None
        self._cost_today = 0.0
        self._baseline_today = 0.0
        self._saving_total = 0.0
        self._saving_since: datetime | None = None
        self._last_price_count = -1
        self.shadow = ShadowBattery()
        self._shadow_cost_today = 0.0
        self._shadow_saving_total = 0.0
        self._shadow_plan: Plan | None = None
        self._shadow_result: ControlResult | None = None
        self._shadow_previous_total: float | None = None
        self._hems_saving_total = 0.0
        # Schakelaar "HEMS stuurt" (switch.py zet dit; standaard uit).
        self.control_enabled = False
        self._control_was_active = False
        self._actuators: dict[str, MarstekActuator] = {}
        self._tick_lock = asyncio.Lock()
        self.data = HemsData()

    # ------------------------------------------------------------------ config
    def _conf(self, key: str) -> Any:
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, DEFAULTS.get(key))

    @property
    def battery_prefixes(self) -> list[str]:
        raw = self._conf(CONF_BATTERIES) or ""
        return [p.strip() for p in str(raw).split(",") if p.strip()]

    @property
    def controlled_prefixes(self) -> list[str]:
        """Batterijen die HEMS mag sturen (uit de opties, en alleen bekende batterijen)."""
        raw = self._conf(CONF_CONTROLLED) or []
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.split(",")]
        wanted = {str(p).strip() for p in raw if str(p).strip()}
        return [p for p in self.battery_prefixes if p in wanted]

    @property
    def control_active(self) -> bool:
        return self.control_enabled and bool(self.controlled_prefixes)

    def _actuator(self, prefix: str) -> MarstekActuator:
        if prefix not in self._actuators:
            self._actuators[prefix] = MarstekActuator(self.hass, prefix)
        return self._actuators[prefix]

    async def async_set_control(self, enabled: bool) -> None:
        """Schakelaar "HEMS stuurt" aan of uit."""
        self.control_enabled = enabled
        await self._async_fast_tick(dt_util.now())

    # --------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.profile = LoadProfile.from_dict(
            stored.get("profile"), default_w=float(self._conf(CONF_DEFAULT_LOAD_W))
        )
        today = dt_util.now().date()
        if stored.get("cost_day") == today.isoformat():
            self._cost_day = today
            self._cost_today = float(stored.get("cost_today", 0.0))
            self._baseline_today = float(stored.get("baseline_today", 0.0))
            self._shadow_cost_today = float(stored.get("shadow_cost_today", self._cost_today))
        else:
            self._cost_day = today
        self._saving_total = float(stored.get("saving_total", 0.0))
        since = stored.get("saving_since")
        self._saving_since = dt_util.parse_datetime(since) if since else dt_util.now()
        self._shadow_saving_total = float(stored.get("shadow_saving_total", 0.0))
        self._hems_saving_total = float(stored.get("hems_saving_total", self._shadow_saving_total))
        saved_at = dt_util.parse_datetime(stored.get("saved_at") or "")
        if saved_at and (dt_util.now() - saved_at).total_seconds() <= SHADOW_MAX_RESTORE_SECONDS:
            self.shadow = ShadowBattery.from_dict(stored.get("shadow"))
        elif stored.get("shadow"):
            self._add_log(dt_util.now(), "info", "schaduwbatterij te oud; opnieuw gestart bij de echte stand")
        self._last_prices = _prices_from_store(stored.get("prices"))

        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_fast_tick, timedelta(seconds=FAST_LOOP_SECONDS))
        )
        self._unsubs.append(
            async_track_time_change(self.hass, self._async_quarter_tick, minute=[0, 15, 30, 45], second=5)
        )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self._conf(CONF_PRICE_SENSOR)], self._async_price_changed
            )
        )
        await self.async_replan("opstart")
        await self._async_fast_tick(dt_util.now())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._control_was_active:
            # Bij afsluiten of herladen niets achterlaten dat doorloopt: batterijen stil.
            await self._async_stop_controlled(dt_util.now(), "HEMS-integratie stopt")
        await self._store.async_save(self._storage_payload())

    async def _async_stop_controlled(self, now: datetime, reason: str) -> None:
        # Wat HEMS echt heeft aangestuurd, ook als de opties intussen zijn gewijzigd.
        for prefix in list(self._actuators) or self.controlled_prefixes:
            await self._actuator(prefix).async_stop(now)
        self._add_log(now, "info", f"gestuurde batterijen stilgezet: {reason}")

    def _storage_payload(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "cost_day": self._cost_day.isoformat() if self._cost_day else None,
            "cost_today": self._cost_today,
            "baseline_today": self._baseline_today,
            "saving_total": self._saving_total,
            "saving_since": self._saving_since.isoformat() if self._saving_since else None,
            "shadow": self.shadow.to_dict(),
            "shadow_cost_today": self._shadow_cost_today,
            "shadow_saving_total": self._shadow_saving_total,
            "hems_saving_total": self._hems_saving_total,
            "saved_at": dt_util.now().isoformat(),
            "prices": [
                [p.start.isoformat(), p.end.isoformat(), p.buy, p.buy_ex_tax] for p in self._last_prices
            ],
        }

    # ------------------------------------------------------------- read state
    def _state(self, entity_id: str) -> State | None:
        return self.hass.states.get(entity_id)

    def _fresh(self, state: State | None, max_age: float, now: datetime) -> bool:
        if state is None:
            return False
        seen = getattr(state, "last_reported", None) or state.last_updated
        return (now - seen).total_seconds() <= max_age

    def _read_p1(self, now: datetime) -> float | None:
        state = self._state(self._conf(CONF_P1_POWER))
        value = _float_state(state)
        if value is None or not self._fresh(state, STALE_P1_SECONDS, now):
            return None
        return value

    def _read_pv(self) -> float | None:
        entities = self._conf(CONF_PV_POWER) or []
        if isinstance(entities, str):
            entities = [entities]
        inverted = bool(self._conf(CONF_PV_INVERTED))
        total = 0.0
        seen = False
        for entity_id in entities:
            value = _float_state(self._state(entity_id))
            if value is None:
                continue
            seen = True
            total += max(0.0, -value if inverted else value)
        return total if seen else None

    def _read_batteries(self, now: datetime) -> list[BatteryState]:
        default_eff = float(self._conf(CONF_DEFAULT_EFFICIENCY))
        batteries: list[BatteryState] = []
        for prefix in self.battery_prefixes:
            ent = {k: v.format(p=prefix) for k, v in BATTERY_ENTITY_PATTERNS.items()}
            soc_state = self._state(ent["soc"])
            power_state = self._state(ent["ac_power"])
            soc = _float_state(soc_state)
            power = _float_state(power_state)
            capacity = _float_state(self._state(ent["capacity"])) or 5.12
            eff_pct = _float_state(self._state(ent["efficiency"]))
            efficiency = eff_pct / 100.0 if eff_pct and 50.0 <= eff_pct <= 100.0 else default_eff
            soc_min = _float_state(self._state(ent["soc_min"]))
            soc_max = _float_state(self._state(ent["soc_max"]))
            alarms = [
                text
                for suffix, text in BATTERY_ALARM_SUFFIXES.items()
                if (s := self._state(f"binary_sensor.{prefix}_{suffix}")) is not None and s.state == "on"
            ]
            rs485 = self._state(ent["rs485_mode"])
            if rs485 is not None and rs485.state == RS485_DISABLED:
                alarms.append("RS485-besturing staat uit (batterij volgt geen opdrachten)")
            heartbeat = any(
                self._fresh(self._state(pattern.format(p=prefix)), STALE_BATTERY_SECONDS, now)
                for pattern in BATTERY_HEARTBEAT_PATTERNS
            )
            available = soc is not None and power is not None and heartbeat
            batteries.append(
                BatteryState(
                    name=prefix,
                    soc_pct=soc,
                    capacity_kwh=capacity,
                    ac_power_w=power,
                    max_charge_w=_float_state(self._state(ent["max_charge"])) or 2500.0,
                    max_discharge_w=_float_state(self._state(ent["max_discharge"])) or 2500.0,
                    soc_min_pct=soc_min if soc_min is not None else 12.0,
                    soc_max_pct=soc_max if soc_max is not None else 100.0,
                    available=available,
                    alarms=alarms,
                    efficiency=efficiency,
                )
            )
        return batteries

    def _with_hbc_limits(self, batteries: list[BatteryState]) -> list[BatteryState]:
        """Dezelfde batterijen, maar met de laadgrenzen die HBC ervoor gebruikt (schaduw-HBC)."""
        result = []
        for b in batteries:
            low = _float_state(self._state(HBC_CUTOFF_PATTERNS["soc_min"].format(p=b.name)))
            high = _float_state(self._state(HBC_CUTOFF_PATTERNS["soc_max"].format(p=b.name)))
            result.append(
                replace(
                    b,
                    soc_min_pct=low if low is not None and 0 <= low <= 100 else b.soc_min_pct,
                    soc_max_pct=high if high is not None and 0 <= high <= 100 else b.soc_max_pct,
                )
            )
        return result

    def _hbc_targets(self) -> HbcTargets:
        charge = _float_state(self._state(HBC_CHARGE_TARGET_SOC_ENTITY))
        sell = _float_state(self._state(HBC_SELL_TARGET_SOC_ENTITY))
        return HbcTargets(
            charge_target_soc=charge if charge is not None else 100.0,
            sell_target_soc=sell if sell is not None else 0.0,
        )

    def _planned_batteries(self, batteries: list[BatteryState]) -> list[BatteryState]:
        """Waarover het plan gaat: bij sturen alleen de gestuurde batterijen, anders alle."""
        if self.control_active:
            controlled = set(self.controlled_prefixes)
            return [b for b in batteries if b.name in controlled]
        return batteries

    def _wear_eur_per_kwh(self) -> float:
        entity_id = self._conf(CONF_WEAR_ENTITY)
        if entity_id:
            value = _float_state(self._state(entity_id))
            if value is not None and 0.0 <= value <= 50.0:
                return value / 100.0
        return float(self._conf(CONF_WEAR_CT)) / 100.0

    def _battery_model(self, batteries: list[BatteryState]) -> BatteryModel | None:
        usable = [b for b in batteries if b.soc_pct is not None]
        if not usable:
            return None
        capacity = sum(b.capacity_kwh for b in usable)
        efficiency = sum(b.efficiency * b.capacity_kwh for b in usable) / capacity
        return BatteryModel(
            capacity_kwh=capacity,
            soc_min_kwh=sum(b.capacity_kwh * b.soc_min_pct / 100.0 for b in usable),
            soc_max_kwh=sum(b.capacity_kwh * b.soc_max_pct / 100.0 for b in usable),
            max_charge_kw=sum(b.max_charge_w for b in usable) / 1000.0,
            max_discharge_kw=sum(b.max_discharge_w for b in usable) / 1000.0,
            roundtrip_efficiency=efficiency,
            wear_eur_per_kwh=self._wear_eur_per_kwh(),
        )

    def _netting_end(self) -> datetime:
        raw = str(self._conf(CONF_NETTING_END))
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            day = date(2027, 1, 1)
        return dt_util.start_of_local_day(day)

    # ---------------------------------------------------------------- planning
    def _pv_periods(self, now: datetime) -> tuple[list[PvPeriod], str]:
        """Zonverwachting uit Helios, of uit Solcast als Helios niet compleet is.

        Helios telt alleen als elk ingesteld veld een verwachting voor de komende
        tijd heeft; met één veld zou de verwachting maar half zo groot zijn.
        """
        field_name = str(self._conf(CONF_SOLCAST_FIELD))
        reason = ""
        if self._conf(CONF_PV_SOURCE) == PV_SOURCE_HELIOS:
            entities = self._conf(CONF_HELIOS_ENTITIES) or []
            if isinstance(entities, str):
                entities = [entities]
            periods: list[PvPeriod] = []
            missing = []
            for entity_id in entities:
                state = self._state(entity_id)
                found = parse_helios_forecast(state.attributes if state else None, field_name)
                if not any(p.end > now for p in found):
                    missing.append(entity_id)
                periods.extend(found)
            if entities and not missing:
                return periods, PV_SOURCE_HELIOS
            reason = f" (terugval: geen Helios-verwachting van {', '.join(missing) or 'geen sensoren'})"
        periods = []
        for key in (CONF_SOLCAST_TODAY, CONF_SOLCAST_TOMORROW):
            state = self._state(self._conf(key))
            periods.extend(parse_solcast_detailed(state.attributes if state else None, field_name))
        return periods, PV_SOURCE_SOLCAST + reason

    async def async_replan(self, trigger: str) -> None:
        now = dt_util.now()
        all_batteries = self._read_batteries(now)
        batteries = self._planned_batteries(all_batteries)
        model = self._battery_model(batteries)
        price_state = self._state(self._conf(CONF_PRICE_SENSOR))
        fresh = parse_zonneplan_forecast(price_state.attributes if price_state else None)
        if fresh:
            self._last_prices = fresh
            self.data.price_source = "zonneplan"
        else:
            # Zonder internet zet Zonneplan de sensor op 'niet beschikbaar', terwijl de
            # prijzen tot en met morgen vaak al binnen waren: blijf daarmee plannen.
            fresh = self._last_prices
            self.data.price_source = "laatst bekend (Zonneplan niet bereikbaar)" if fresh else None
        prices = upcoming_slots(fresh, now)
        if model is None or not prices:
            self._plan = None
            self._shadow_plan = None
            missing = "geen prijzen" if not prices else "geen batterijdata"
            self._add_log(now, MODE_SAFETY, f"geen plan: {missing}")
            return

        pv_periods, self.data.pv_source = self._pv_periods(now)

        # Tijdens saldering is terugleveren evenveel waard als afnemen; daarna (of als
        # die optie uit staat, bijvoorbeeld bij een jaaroverschot) de kale prijs.
        netting_end = self._netting_end() if self._conf(CONF_NETTING_ACTIVE) else now
        feed_in_cost = float(self._conf(CONF_FEED_IN_COST_CT)) / 100.0
        slots: list[PlanSlot] = []
        for price in prices:
            start = max(price.start, now)
            end = price.end
            if (end - start).total_seconds() < 30:
                continue
            if price.start < netting_end:
                sell = price.buy  # saldering: belasting komt terug
            else:
                sell = price.buy_ex_tax - feed_in_cost  # alleen kale prijs, minus eventuele kosten
            slots.append(
                PlanSlot(
                    start=start,
                    end=end,
                    buy=price.buy,
                    sell=sell,
                    pv_kwh=pv_kwh_between(pv_periods, start, end),
                    load_kwh=self.profile.kwh_between(start, end),
                )
            )
        soc_now = sum(b.soc_kwh for b in batteries if b.soc_pct is not None)
        priorities = Priorities(
            solar_first_eur_per_kwh=float(self._conf(CONF_SOLAR_FIRST_CT)) / 100.0,
            trade_margin_eur_per_kwh=float(self._conf(CONF_TRADE_MARGIN_CT)) / 100.0,
        )
        plan = await self.hass.async_add_executor_job(
            make_plan, slots, model, soc_now, now, PLAN_STEP_KWH, priorities
        )
        self._plan = plan
        if self.control_active:
            # Bij echt sturen volgt de schaduw HBC; daar hoort geen tweede HEMS-plan bij.
            self._shadow_plan = None
        else:
            # Tweede, losse plan vanaf de schaduwstand: alleen voor de besparingsvergelijking.
            self.shadow.sync(batteries, now)
            shadow_soc = sum(b.soc_kwh for b in self.shadow.apply(batteries) if b.soc_pct is not None)
            self._shadow_plan = await self.hass.async_add_executor_job(
                make_plan, slots, model, shadow_soc, now, PLAN_STEP_KWH, priorities
            )
        saving = plan.expected_saving_eur(soc_now)
        self.data.expected_saving_eur = saving
        first = plan.steps[0] if plan.steps else None
        self._add_log(
            now,
            first.mode if first else MODE_SAFETY,
            f"nieuw plan ({trigger}): {len(plan.steps)} kwartieren, verwachte besparing "
            f"€{saving:.2f}, zon: {self.data.pv_source}; nu: {first.reason if first else '-'}",
        )

    @callback
    def _async_price_changed(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        count = len(new_state.attributes.get("forecast") or []) if new_state else 0
        if count != self._last_price_count:
            # Nieuwe prijzen (bijvoorbeeld die van morgen) binnen: opnieuw plannen.
            self._last_price_count = count
            self.hass.async_create_task(self.async_replan("nieuwe prijzen"))

    async def _async_quarter_tick(self, now: datetime) -> None:
        await self.async_replan("kwartier")

    # ---------------------------------------------------------------- fast loop
    async def _async_fast_tick(self, now: datetime) -> None:
        # Niet twee regelrondes door elkaar (de schakelaar start er ook een).
        async with self._tick_lock:
            await self._async_fast_tick_locked()

    async def _async_fast_tick_locked(self) -> None:
        now = dt_util.now()
        batteries = self._read_batteries(now)
        p1 = self._read_p1(now)
        pv = self._read_pv()
        battery_total = sum(b.ac_power_w or 0.0 for b in batteries)
        house = None
        if p1 is not None:
            house = p1 + (pv or 0.0) + battery_total

        active = self.control_active
        if active != self._control_was_active:
            await self._async_control_changed(now, active)
        controlled_names = set(self.controlled_prefixes) if active else set()
        planned = [b for b in batteries if b.name in controlled_names] if active else batteries
        others_w = sum(b.ac_power_w or 0.0 for b in batteries if b.name not in controlled_names)
        planned_w = sum(b.ac_power_w or 0.0 for b in planned)
        # Schaduw: bij meekijken HEMS op papier voor alle batterijen; bij sturen HBC op papier
        # voor de gestuurde batterijen, met de grenzen die HBC zelf gebruikt.
        shadow_basis = self._with_hbc_limits(planned) if active else batteries

        dt = (now - self._last_tick).total_seconds() if self._last_tick else 0.0
        if not 0 < dt <= 60:
            dt = 0.0
        prev_shadow = self._shadow_result
        self.shadow.sync(shadow_basis, now)
        if prev_shadow is not None:
            # Het vorige schaduwvoorstel gold tot nu: laat het op de schaduwstand inwerken.
            self.shadow.integrate(prev_shadow.per_battery_w, dt, shadow_basis)
        shadow_grid = None
        if p1 is not None and prev_shadow is not None:
            # Netmeting als de schaduw het voor het zeggen had gehad.
            shadow_grid = p1 + planned_w - prev_shadow.total_w
        self._learn(now, house, p1, battery_total, shadow_grid, hems_is_real=active)

        step = self._plan.step_at(now) if self._plan else None
        target = _target(step)
        sub_state = self._state(HBC_SUB_STRATEGY_ENTITY)
        sub = sub_state.state if sub_state else None
        if active and target is not None and target.fixed_w is not None:
            others = [b for b in batteries if b.name not in controlled_names]
            if hbc_counteracts(target.mode, sub, others):
                # Bijv. HEMS laadt M2 uit het net terwijl HBC M1 op zelfverbruik heeft:
                # HBC zou M1 ontladen om die afname te dekken. Dan alleen zelfverbruik
                # binnen de grenzen van het plan (bij net_laden: niet ontladen).
                verb = "uit het net laden" if target.mode == MODE_GRID_CHARGE else "verkopen"
                target = replace(
                    target,
                    mode=MODE_SELF_CONSUMPTION,
                    fixed_w=None,
                    reason=f"niet {verb}: HBC ({sub or 'onbekend'}) zou de andere batterij "
                    f"ertegenin laten werken; {target.reason}",
                )
        result = control_step(
            target,
            p1,
            planned,
            self._previous_total,
            self.settings,
            other_battery_w=others_w if active else 0.0,
        )
        self._previous_total = result.total_w if result.mode != MODE_SAFETY else None

        commands: dict[str, Command] = {}
        errors: list[str] = []
        if active:
            for b in planned:
                watt = result.per_battery_w.get(b.name, 0.0) if b.usable else 0.0
                max_w = b.max_discharge_w if watt > 0 else b.max_charge_w
                actuator = self._actuator(b.name)
                commands[b.name] = await actuator.async_apply(watt, max_w, now)
                if actuator.last_error:
                    errors.append(actuator.last_error)

        if active:
            shadow_batteries = self.shadow.apply(shadow_basis)
            residual = (p1 + planned_w) if p1 is not None else 0.0
            per = (
                hbc_step(sub, residual, shadow_batteries, self._hbc_targets(), self.settings)
                if p1 is not None
                else {b.name: 0.0 for b in shadow_batteries}
            )
            self._shadow_result = ControlResult(
                mode=f"hbc: {sub or 'onbekend'}",
                total_w=round(sum(per.values()), 0),
                per_battery_w=per,
                reason="schaduw-HBC volgt de actieve HBC-deelstrategie",
            )
        else:
            shadow_step = self._shadow_plan.step_at(now) if self._shadow_plan else None
            shadow_result = control_step(
                _target(shadow_step),
                p1,
                self.shadow.apply(batteries),
                self._shadow_previous_total,
                self.settings,
            )
            self._shadow_previous_total = shadow_result.total_w if shadow_result.mode != MODE_SAFETY else None
            self._shadow_result = shadow_result

        hbc_state = self._state(HBC_STRATEGY_ENTITY)
        hbc = hbc_state.state if hbc_state else None
        if result.mode != self._last_mode:
            self._add_log(now, result.mode, result.reason, hbc=hbc, total_w=result.total_w)
            self._last_mode = result.mode

        model = self._battery_model(planned)
        data = self.data
        data.result = result
        data.plan = self._plan
        data.p1_w = p1
        data.pv_w = pv
        data.house_w = house
        data.battery_actual_w = planned_w if planned else None
        data.battery_actual_per = {b.name: b.ac_power_w for b in batteries}
        data.soc_total_kwh = sum(b.soc_kwh for b in planned if b.soc_pct is not None)
        data.efficiency = model.roundtrip_efficiency if model else None
        data.wear_eur_per_kwh = model.wear_eur_per_kwh if model else None
        if model and step:
            data.break_even_ct = 100.0 * break_even_spread(
                step.buy, model.roundtrip_efficiency, model.wear_eur_per_kwh
            )
        data.actual_grid_cost_today_eur = self._cost_today
        data.baseline_cost_today_eur = self._baseline_today
        data.saving_total_eur = self._saving_total
        data.saving_since = self._saving_since
        data.investment_eur = float(self._conf(CONF_INVESTMENT_EUR))
        data.profile_filled_bins = self.profile.filled_bins
        data.hbc_strategy = hbc
        data.decision_log = list(self._log)
        data.shadow_soc_kwh = self.shadow.total_kwh
        data.shadow_result = self._shadow_result
        data.shadow_cost_today_eur = self._shadow_cost_today
        data.shadow_saving_total_eur = self._shadow_saving_total
        data.hems_saving_total_eur = self._hems_saving_total
        data.shadow_since = self.shadow.since
        data.control_active = active
        data.controlled = sorted(controlled_names)
        data.commands = commands
        data.control_errors = errors
        data.other_battery_w = others_w if active else None
        self.async_set_updated_data(data)

    async def _async_control_changed(self, now: datetime, active: bool) -> None:
        """Overgang tussen meekijken en sturen: schaduw en vergelijking opnieuw beginnen."""
        self._control_was_active = active
        if active:
            self._add_log(now, "info", f"HEMS stuurt nu: {', '.join(self.controlled_prefixes)}")
        else:
            await self._async_stop_controlled(now, "schakelaar uit of geen gestuurde batterijen meer")
            self._actuators.clear()
        # De schaduw wisselt van betekenis (HEMS op papier <-> HBC op papier).
        self.shadow = ShadowBattery()
        self._shadow_result = None
        self._shadow_previous_total = None
        self._previous_total = None
        self._shadow_cost_today = self._cost_today
        await self.async_replan("sturen aan" if active else "sturen uit")

    def _learn(
        self,
        now: datetime,
        house_w: float | None,
        p1_w: float | None,
        battery_w: float,
        shadow_grid_w: float | None = None,
        hems_is_real: bool = False,
    ) -> None:
        """Huisverbruik per kwartier leren, en netkosten met en zonder batterij optellen.

        shadow_grid_w is de netmeting als de schaduw aan het stuur had gezeten: bij
        meekijken HEMS op papier, bij sturen HBC op papier. hems_is_real zegt welke van
        de twee (echt of schaduw) de HEMS-kant van de vergelijking is.
        """
        dt = (now - self._last_tick).total_seconds() if self._last_tick else 0.0
        self._last_tick = now
        if dt <= 0 or dt > 60:
            dt = 0.0

        if self._cost_day != now.date():
            self._cost_day = now.date()
            self._cost_today = 0.0
            self._baseline_today = 0.0
            self._shadow_cost_today = 0.0
        if p1_w is not None and dt and self._plan:
            step = self._plan.step_at(now)
            if step:
                kwh = p1_w * dt / 3_600_000.0
                actual = kwh * (step.buy if kwh >= 0 else step.sell)
                # Zonder batterij was de netmeting P1 + wat de batterij leverde (of minus wat hij laadde).
                base_kwh = (p1_w + battery_w) * dt / 3_600_000.0
                baseline = base_kwh * (step.buy if base_kwh >= 0 else step.sell)
                self._cost_today += actual
                self._baseline_today += baseline
                self._saving_total += baseline - actual
                if shadow_grid_w is not None:
                    shadow_kwh = shadow_grid_w * dt / 3_600_000.0
                    shadow = shadow_kwh * (step.buy if shadow_kwh >= 0 else step.sell)
                    self._shadow_cost_today += shadow
                    self._shadow_saving_total += baseline - shadow
                    self._hems_saving_total += baseline - (actual if hems_is_real else shadow)

        quarter = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        if self._quarter_start is None:
            self._quarter_start = quarter
        if quarter != self._quarter_start:
            # Alleen kwartieren met genoeg metingen meenemen in het profiel.
            if self._quarter_seconds >= 600:
                avg_w = self._quarter_energy_wh / (self._quarter_seconds / 3600.0)
                self.profile.update(self._quarter_start, avg_w)
                self._store.async_delay_save(self._storage_payload, 60)
            self._quarter_start = quarter
            self._quarter_energy_wh = 0.0
            self._quarter_seconds = 0.0
        if house_w is not None and dt:
            self._quarter_energy_wh += house_w * dt / 3600.0
            self._quarter_seconds += dt

    def _add_log(self, now: datetime, mode: str, reason: str, **extra: Any) -> None:
        entry = {"tijd": now.isoformat(timespec="seconds"), "modus": mode, "reden": reason}
        entry.update({k: v for k, v in extra.items() if v is not None})
        self._log.append(entry)
        _LOGGER.info(
            "HEMS Batterij (%s): %s - %s", "stuurt" if self._control_was_active else "meekijken", mode, reason
        )
        self.data.decision_log = list(self._log)


def _prices_from_store(raw: Any) -> list[PriceSlot]:
    """Bewaarde prijzen terug; iets onleesbaars betekent gewoon geen reserve."""
    slots: list[PriceSlot] = []
    for item in raw or []:
        try:
            start, end = dt_util.parse_datetime(item[0]), dt_util.parse_datetime(item[1])
            if start and end:
                slots.append(PriceSlot(start, end, float(item[2]), float(item[3])))
        except (TypeError, ValueError, IndexError):
            continue
    return slots
