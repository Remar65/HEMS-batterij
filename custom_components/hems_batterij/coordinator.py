"""Coördinator: leest Home Assistant, maakt het plan en draait de regellus.

MEEKIJKMODUS: deze klasse leest alleen entiteiten en publiceert eigen
sensoren. Er wordt nergens een service aangeroepen en er wordt niets naar
de batterijen gestuurd (zie ook tests/test_no_writes.py).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
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

from .const import (
    BATTERY_ALARM_SUFFIXES,
    BATTERY_ENTITY_PATTERNS,
    BATTERY_HEARTBEAT_PATTERNS,
    CONF_BATTERIES,
    CONF_DEFAULT_EFFICIENCY,
    CONF_DEFAULT_LOAD_W,
    CONF_FEED_IN_COST_CT,
    CONF_INVESTMENT_EUR,
    CONF_NETTING_ACTIVE,
    CONF_NETTING_END,
    CONF_P1_POWER,
    CONF_PRICE_SENSOR,
    CONF_PV_INVERTED,
    CONF_PV_POWER,
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
    PLAN_STEP_KWH,
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
from .forecast import LoadProfile, parse_solcast_detailed, pv_kwh_between
from .planner import (
    MODE_SAFETY,
    BatteryModel,
    Plan,
    PlanSlot,
    Priorities,
    break_even_spread,
    make_plan,
)
from .prices import parse_zonneplan_forecast, upcoming_slots

_LOGGER = logging.getLogger(__name__)

HBC_STRATEGY_ENTITY = "input_select.house_battery_strategy"


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
        else:
            self._cost_day = today
        self._saving_total = float(stored.get("saving_total", 0.0))
        since = stored.get("saving_since")
        self._saving_since = dt_util.parse_datetime(since) if since else dt_util.now()

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
        await self._store.async_save(self._storage_payload())

    def _storage_payload(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "cost_day": self._cost_day.isoformat() if self._cost_day else None,
            "cost_today": self._cost_today,
            "baseline_today": self._baseline_today,
            "saving_total": self._saving_total,
            "saving_since": self._saving_since.isoformat() if self._saving_since else None,
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
    async def async_replan(self, trigger: str) -> None:
        now = dt_util.now()
        batteries = self._read_batteries(now)
        model = self._battery_model(batteries)
        price_state = self._state(self._conf(CONF_PRICE_SENSOR))
        prices = upcoming_slots(
            parse_zonneplan_forecast(price_state.attributes if price_state else None), now
        )
        if model is None or not prices:
            self._plan = None
            missing = "geen prijzen" if not prices else "geen batterijdata"
            self._add_log(now, MODE_SAFETY, f"geen plan: {missing}")
            return

        field_name = str(self._conf(CONF_SOLCAST_FIELD))
        pv_periods = []
        for key in (CONF_SOLCAST_TODAY, CONF_SOLCAST_TOMORROW):
            state = self._state(self._conf(key))
            pv_periods.extend(parse_solcast_detailed(state.attributes if state else None, field_name))

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
        saving = plan.expected_saving_eur(soc_now)
        self.data.expected_saving_eur = saving
        first = plan.steps[0] if plan.steps else None
        self._add_log(
            now,
            first.mode if first else MODE_SAFETY,
            f"nieuw plan ({trigger}): {len(plan.steps)} kwartieren, verwachte besparing "
            f"€{saving:.2f}; nu: {first.reason if first else '-'}",
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
        now = dt_util.now()
        batteries = self._read_batteries(now)
        p1 = self._read_p1(now)
        pv = self._read_pv()
        battery_total = sum(b.ac_power_w or 0.0 for b in batteries)
        house = None
        if p1 is not None:
            house = p1 + (pv or 0.0) + battery_total

        self._learn(now, house, p1, battery_total)

        step = self._plan.step_at(now) if self._plan else None
        target = (
            ControlTarget(
                mode=step.mode,
                fixed_w=step.fixed_w,
                max_charge_w=step.max_charge_w,
                max_discharge_w=step.max_discharge_w,
                reason=step.reason,
            )
            if step
            else None
        )
        result = control_step(target, p1, batteries, self._previous_total, self.settings)
        self._previous_total = result.total_w if result.mode != MODE_SAFETY else None

        hbc_state = self._state(HBC_STRATEGY_ENTITY)
        hbc = hbc_state.state if hbc_state else None
        if result.mode != self._last_mode:
            self._add_log(now, result.mode, result.reason, hbc=hbc, total_w=result.total_w)
            self._last_mode = result.mode

        model = self._battery_model(batteries)
        data = self.data
        data.result = result
        data.plan = self._plan
        data.p1_w = p1
        data.pv_w = pv
        data.house_w = house
        data.battery_actual_w = battery_total if batteries else None
        data.battery_actual_per = {b.name: b.ac_power_w for b in batteries}
        data.soc_total_kwh = sum(b.soc_kwh for b in batteries if b.soc_pct is not None)
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
        self.async_set_updated_data(data)

    def _learn(self, now: datetime, house_w: float | None, p1_w: float | None, battery_w: float) -> None:
        """Huisverbruik per kwartier leren, en netkosten met en zonder batterij optellen."""
        dt = (now - self._last_tick).total_seconds() if self._last_tick else 0.0
        self._last_tick = now
        if dt <= 0 or dt > 60:
            dt = 0.0

        if self._cost_day != now.date():
            self._cost_day = now.date()
            self._cost_today = 0.0
            self._baseline_today = 0.0
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
        _LOGGER.info("HEMS Batterij (meekijken): %s - %s", mode, reason)
        self.data.decision_log = list(self._log)
