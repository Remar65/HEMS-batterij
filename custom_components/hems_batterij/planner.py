"""Planner: kiest per kwartier wat de batterijen (samen) het beste kunnen doen.

Werkwijze: dynamisch programmeren over de laadtoestand (kWh in de cellen) voor
alle bekende prijskwartieren. Per kwartier telt:

- netkosten: afname x inkoopprijs, teruglevering x terugleverprijs
  (tot het einde van de saldering is terugleveren evenveel waard als afnemen);
- slijtage: per kWh die uit de batterij komt (DC), bijvoorbeeld 4,7 ct;
- rendement: laden en ontladen kosten elk de wortel van het gemeten
  round-trip-rendement.

Wat aan het einde van de horizon nog in de batterij zit, krijgt een
restwaarde (gemiddelde inkoopprijs x ontlaadrendement - slijtage), zodat de
planner de batterij niet om middernacht leeggooit omdat de prijzen van
morgen nog niet bekend zijn.

Puur Python (geen Home Assistant-imports), zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math

# Modi voor de snelle regellus.
MODE_SELF_CONSUMPTION = "zelfverbruik"  # P1 naar 0 regelen binnen de grenzen
MODE_GRID_CHARGE = "net_laden"  # vast laadvermogen, ook uit het net
MODE_SELL = "verkopen"  # vast ontlaadvermogen, ook naar het net
MODE_HOLD = "vasthouden"  # niet ontladen, zon-overschot mag wel de batterij in
MODE_SAFETY = "vangnet"  # er klopt iets niet: niets voorstellen

ALL_MODES = [MODE_SELF_CONSUMPTION, MODE_GRID_CHARGE, MODE_SELL, MODE_HOLD, MODE_SAFETY]

# Kleine straf per kWh verplaatste energie, zodat de planner bij gelijke kosten
# kiest voor minder heen-en-weer schuiven.
_TIE_BREAK_EUR_PER_KWH = 0.0001
# Onder deze hoeveelheid (kWh per kwartier) telt iets als "niets".
_TOLERANCE_KWH = 0.03


@dataclass(frozen=True)
class BatteryModel:
    """De batterijen samen gezien als één grote batterij."""

    capacity_kwh: float
    soc_min_kwh: float
    soc_max_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    roundtrip_efficiency: float
    wear_eur_per_kwh: float

    @property
    def charge_efficiency(self) -> float:
        return math.sqrt(self.roundtrip_efficiency)

    @property
    def discharge_efficiency(self) -> float:
        return math.sqrt(self.roundtrip_efficiency)


@dataclass(frozen=True)
class PlanSlot:
    """Invoer voor één kwartier."""

    start: datetime
    end: datetime
    buy: float  # €/kWh
    sell: float  # €/kWh
    pv_kwh: float
    load_kwh: float

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


@dataclass
class PlanStep:
    """Uitkomst voor één kwartier."""

    start: datetime
    end: datetime
    buy: float
    sell: float
    pv_kwh: float
    load_kwh: float
    soc_start_kwh: float
    soc_end_kwh: float
    battery_ac_kwh: float  # + = ontladen naar huis/net, - = laden
    grid_kwh: float  # + = afname, - = teruglevering
    cost_eur: float  # netkosten + slijtage in dit kwartier
    baseline_cost_eur: float  # netkosten als de batterij niets zou doen
    mode: str = MODE_SELF_CONSUMPTION
    fixed_w: float | None = None  # alleen bij net_laden / verkopen
    max_charge_w: float = 0.0
    max_discharge_w: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "modus": self.mode,
            "batterij_kwh": round(self.battery_ac_kwh, 3),
            "soc_eind_kwh": round(self.soc_end_kwh, 2),
            "net_kwh": round(self.grid_kwh, 3),
            "prijs": round(self.buy, 4),
            "reden": self.reason,
        }


@dataclass
class Plan:
    created: datetime
    steps: list[PlanStep] = field(default_factory=list)
    terminal_value_eur_per_kwh: float = 0.0

    @property
    def cost_eur(self) -> float:
        return sum(s.cost_eur for s in self.steps)

    @property
    def baseline_cost_eur(self) -> float:
        return sum(s.baseline_cost_eur for s in self.steps)

    @property
    def soc_end_kwh(self) -> float:
        return self.steps[-1].soc_end_kwh if self.steps else 0.0

    def expected_saving_eur(self, soc_now_kwh: float) -> float:
        """Besparing t.o.v. niets doen, inclusief verschil in restwaarde."""
        if not self.steps:
            return 0.0
        stored_delta = (self.soc_end_kwh - soc_now_kwh) * self.terminal_value_eur_per_kwh
        return self.baseline_cost_eur - self.cost_eur + stored_delta

    def step_at(self, moment: datetime) -> PlanStep | None:
        for step in self.steps:
            if step.start <= moment < step.end:
                return step
        return None


def break_even_spread(buy_price: float, roundtrip_efficiency: float, wear: float) -> float:
    """Minimale prijsverschil (€/kWh) waarbij laden en later ontladen loont."""
    return buy_price * (1.0 / roundtrip_efficiency - 1.0) + wear


def _grid_cost(grid_kwh: float, slot: PlanSlot) -> float:
    return grid_kwh * slot.buy if grid_kwh >= 0 else grid_kwh * slot.sell


def make_plan(
    slots: list[PlanSlot],
    battery: BatteryModel,
    soc_now_kwh: float,
    created: datetime,
    step_kwh: float = 0.05,
) -> Plan:
    """Bereken het goedkoopste laad/ontlaadschema over alle slots."""
    plan = Plan(created=created)
    if not slots:
        return plan

    lo, hi = battery.soc_min_kwh, battery.soc_max_kwh
    n_levels = max(1, int(round((hi - lo) / step_kwh))) + 1
    levels = [lo + i * (hi - lo) / (n_levels - 1) for i in range(n_levels)] if n_levels > 1 else [lo]
    level_step = levels[1] - levels[0] if n_levels > 1 else 1.0

    def nearest_level(soc: float) -> int:
        if n_levels == 1:
            return 0
        return min(n_levels - 1, max(0, int(round((soc - lo) / level_step))))

    eta_c, eta_d = battery.charge_efficiency, battery.discharge_efficiency
    avg_buy = sum(s.buy for s in slots) / len(slots)
    terminal_value = max(0.0, avg_buy * eta_d - battery.wear_eur_per_kwh)
    plan.terminal_value_eur_per_kwh = terminal_value

    n = len(slots)
    inf = float("inf")
    # value[i][k] = minimale kosten vanaf slot i met laadtoestand levels[k]
    value_next = [-(lv - lo) * terminal_value for lv in levels]
    choices: list[list[int]] = [[0] * n_levels for _ in range(n)]

    for i in range(n - 1, -1, -1):
        slot = slots[i]
        max_up = battery.max_charge_kw * slot.hours * eta_c  # DC kWh erbij
        max_down = battery.max_discharge_kw * slot.hours / eta_d  # DC kWh eraf
        up_steps = int(max_up / level_step + 1e-9) if n_levels > 1 else 0
        down_steps = int(max_down / level_step + 1e-9) if n_levels > 1 else 0
        net_load = slot.load_kwh - slot.pv_kwh
        value_here = [inf] * n_levels
        choice_here = choices[i]
        for k in range(n_levels):
            best, best_j = inf, k
            for j in range(max(0, k - down_steps), min(n_levels - 1, k + up_steps) + 1):
                delta = levels[j] - levels[k]
                if delta >= 0:
                    ac = -delta / eta_c
                    wear = 0.0
                else:
                    ac = -delta * eta_d
                    wear = -delta * battery.wear_eur_per_kwh
                grid = net_load - ac
                cost = _grid_cost(grid, slot) + wear + abs(delta) * _TIE_BREAK_EUR_PER_KWH
                total = cost + value_next[j]
                if total < best:
                    best, best_j = total, j
            value_here[k] = best
            choice_here[k] = best_j
        value_next = value_here

    # Vooruit lopen vanaf de huidige laadtoestand.
    soc_clamped = min(hi, max(lo, soc_now_kwh))
    k = nearest_level(soc_clamped)
    for i, slot in enumerate(slots):
        j = choices[i][k]
        soc_start = soc_clamped if i == 0 else levels[k]
        soc_end = levels[j]
        delta = soc_end - soc_start
        if delta >= 0:
            ac = -delta / eta_c
            wear = 0.0
        else:
            ac = -delta * eta_d
            wear = -delta * battery.wear_eur_per_kwh
        net_load = slot.load_kwh - slot.pv_kwh
        grid = net_load - ac
        step = PlanStep(
            start=slot.start,
            end=slot.end,
            buy=slot.buy,
            sell=slot.sell,
            pv_kwh=slot.pv_kwh,
            load_kwh=slot.load_kwh,
            soc_start_kwh=soc_start,
            soc_end_kwh=soc_end,
            battery_ac_kwh=ac,
            grid_kwh=grid,
            cost_eur=_grid_cost(grid, slot) + wear,
            baseline_cost_eur=_grid_cost(net_load, slot),
        )
        _classify(step, slot, battery)
        plan.steps.append(step)
        k = j

    _explain(plan, battery)
    return plan


def _classify(step: PlanStep, slot: PlanSlot, battery: BatteryModel) -> None:
    """Vertaal de geplande energie naar een opdracht voor de snelle regellus."""
    hours = slot.hours
    surplus = slot.pv_kwh - slot.load_kwh  # + = zon over
    ac = step.battery_ac_kwh
    full_charge_w = battery.max_charge_kw * 1000.0
    full_discharge_w = battery.max_discharge_kw * 1000.0

    if ac < -_TOLERANCE_KWH and -ac > max(surplus, 0.0) + _TOLERANCE_KWH:
        step.mode = MODE_GRID_CHARGE
        step.fixed_w = ac / hours * 1000.0  # negatief = laden
        step.max_charge_w = -step.fixed_w
        return
    if ac > _TOLERANCE_KWH and ac > max(-surplus, 0.0) + _TOLERANCE_KWH:
        step.mode = MODE_SELL
        step.fixed_w = ac / hours * 1000.0
        step.max_discharge_w = step.fixed_w
        return

    deficit = -surplus
    has_energy = step.soc_start_kwh > battery.soc_min_kwh + _TOLERANCE_KWH
    if has_energy and deficit > _TOLERANCE_KWH and ac < deficit - _TOLERANCE_KWH and ac <= _TOLERANCE_KWH:
        # Er is vraag, maar de planner bewaart de energie voor later.
        step.mode = MODE_HOLD
        step.max_discharge_w = 0.0
        step.max_charge_w = full_charge_w
        return

    step.mode = MODE_SELF_CONSUMPTION
    step.max_discharge_w = full_discharge_w
    if surplus > _TOLERANCE_KWH and -ac < surplus - _TOLERANCE_KWH:
        # Niet al het zon-overschot opslaan (bijvoorbeeld omdat later goedkoper laden kan).
        step.max_charge_w = max(0.0, -ac / hours * 1000.0)
    else:
        step.max_charge_w = full_charge_w


def _explain(plan: Plan, battery: BatteryModel) -> None:
    """Geef elke stap een korte, leesbare reden."""
    steps = plan.steps
    for idx, step in enumerate(steps):
        later = steps[idx + 1 :]
        if step.mode == MODE_GRID_CHARGE:
            peak = max((s.buy for s in later), default=step.buy)
            spread = peak - step.buy
            need = break_even_spread(step.buy, battery.roundtrip_efficiency, battery.wear_eur_per_kwh)
            step.reason = (
                f"laden uit net à {step.buy * 100:.1f} ct; later tot {peak * 100:.1f} ct "
                f"(verschil {spread * 100:.1f} ct, nodig {need * 100:.1f} ct)"
            )
        elif step.mode == MODE_SELL:
            step.reason = (
                f"verkopen à {step.sell * 100:.1f} ct: hoger dan wat de opgeslagen kWh later nog oplevert"
            )
        elif step.mode == MODE_HOLD:
            peak = max((s.buy for s in later), default=step.buy)
            step.reason = (
                f"niet ontladen bij {step.buy * 100:.1f} ct; bewaren voor later (tot {peak * 100:.1f} ct)"
            )
        elif step.soc_end_kwh >= battery.soc_max_kwh - _TOLERANCE_KWH and step.pv_kwh > step.load_kwh:
            step.reason = "batterij vol: zon-overschot gaat naar het net"
        elif step.soc_start_kwh <= battery.soc_min_kwh + _TOLERANCE_KWH and step.load_kwh > step.pv_kwh:
            step.reason = "batterij op minimum: tekort uit het net"
        elif step.max_charge_w < battery.max_charge_kw * 1000.0 - 1:
            step.reason = f"zelfverbruik, zon-overschot deels terugleveren à {step.sell * 100:.1f} ct"
        else:
            step.reason = "zelfverbruik: overschot opslaan, tekort uit batterij"
