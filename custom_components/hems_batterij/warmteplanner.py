"""Warmteplanner: vooruit stoken rond dure uren (Marco koos "Planner in HEMS", 16 nov 2026).

De vloerbuffer-automatisering laadde alleen in de 6 goedkoopste uren van de kalenderdag
en keek niet vooruit. Op 16 nov stond de warmtepomp na de middagzon stil terwijl de
vloer afkoelde, en zou hij pas in de duurste avonduren weer starten.

Deze planner rekent voor de komende uren (zo ver als er prijzen of voorspellingen zijn)
uit wanneer stoken het goedkoopst is zonder dat de kamer onder het programma min een
marge zakt:

- de kamer (met dekvloer) is één warmtebuffer: C kWh warmte per graad kamer;
- per uur verliest het huis UA x (kamer - effectieve buitentemperatuur), waarbij wind
  boven 10 km/u de buitentemperatuur lager laat voelen (zelfde windfactor als de
  vloerbuffer);
- zon (verwachte PV-opbrengst als maat) en een vaste bijdrage (mensen, apparaten)
  warmen mee;
- een draaiuur levert een vaste hoeveelheid warmte (de warmtepomp draait op of rond
  zijn minimumvermogen) en kost prijs / COP(buitentemperatuur), met een straf bij
  ontdooiweer;
- elke start kost een kleine boete, zodat korte runs worden vermeden.

Met dynamisch programmeren over (kamertemperatuur, draait wel/niet) kiest de planner
per uur stoken of niet. Onder programma - marge zakken kost veel (comfort gaat voor),
boven programma + marge stoken mag niet. Wat er aan warmte overblijft aan het eind van
de horizon telt mee tegen de gemiddelde warmteprijs, zodat de planner het huis niet
"leeg" laat lopen.

Puur Python (geen Home Assistant-imports), zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Stapgrootte van de kamertemperatuur in de berekening.
STEP_C = 0.05
# Kosten per graaduur onder de ondergrens (euro): comfort gaat ruim voor de prijs.
COMFORT_PENALTY_EUR = 1.0
# Ontdooiweer zoals de automatiseringen het herkennen.
DEFROST_T_MIN = -2.0
DEFROST_T_MAX = 5.0
DEFROST_HUMID = 90.0
# Hooguit zoveel uur vooruit plannen.
MAX_HOURS = 36


@dataclass(frozen=True)
class HeatHour:
    """Eén uur in de planning."""

    start: datetime
    price: float  # euro per kWh stroom, all-in
    t_out: float  # °C
    setpoint: float  # programma van de VRC in dit uur
    wind: float = 0.0  # km/u
    humidity: float | None = None  # %
    pv_kwh: float = 0.0  # verwachte PV-opbrengst, als maat voor zon op het huis
    forecast: bool = False  # prijs is een voorspelling


@dataclass(frozen=True)
class HouseParams:
    """Hoe het huis en de warmtepomp zich gedragen."""

    kwh_per_degree: float = 5.0  # warmte per graad kamer, dekvloer inbegrepen
    loss_w_per_k: float = 100.0  # warmteverlies
    sun_deg_per_kwh: float = 0.0  # graden kamer per kWh PV-opbrengst
    base_deg_per_hour: float = 0.0  # mensen, apparaten
    wind_factor: float = 0.15  # graden kouder per km/u wind boven 10
    run_kwh_heat: float = 2.5  # warmte per draaiuur
    cop_a: float = 3.2  # COP = a + b x buitentemperatuur
    cop_b: float = 0.1
    cop_min: float = 1.5
    cop_max: float = 5.5
    defrost_penalty: float = 0.25  # COP-verlies bij ontdooiweer
    band_below: float = 0.5  # kamer mag zoveel onder het programma
    band_above: float = 0.5  # stoken tot hooguit zoveel boven het programma
    start_penalty_eur: float = 0.05  # per start
    source: str = "standaard"

    def cop(self, hour: HeatHour) -> float:
        cop = self.cop_a + self.cop_b * hour.t_out
        if (
            DEFROST_T_MIN <= hour.t_out <= DEFROST_T_MAX
            and hour.humidity is not None
            and hour.humidity >= DEFROST_HUMID
        ):
            cop *= 1.0 - self.defrost_penalty
        return max(self.cop_min, min(self.cop_max, cop))

    def t_eff(self, hour: HeatHour) -> float:
        return hour.t_out - self.wind_factor * max(0.0, hour.wind - 10.0)

    def next_temp(self, room: float, hour: HeatHour, run: bool) -> float:
        """Kamertemperatuur aan het eind van het uur."""
        loss = self.loss_w_per_k / 1000.0 * (room - self.t_eff(hour)) / self.kwh_per_degree
        gain = self.sun_deg_per_kwh * hour.pv_kwh + self.base_deg_per_hour
        if run:
            gain += self.run_kwh_heat / self.kwh_per_degree
        return room - loss + gain


@dataclass
class HeatPlanHour:
    start: datetime
    run: bool
    room_end: float
    price: float
    cop: float
    cost_eur: float
    setpoint: float
    forecast: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "uur": self.start.isoformat(),
            "stoken": self.run,
            "kamer": round(self.room_end, 2),
            "programma": self.setpoint,
            "prijs_ct": round(self.price * 100, 1),
            "ct_per_kwh_warmte": round(self.price / self.cop * 100, 1),
            "kosten_eur": round(self.cost_eur, 3),
            "voorspeld": self.forecast,
        }


@dataclass
class HeatPlan:
    hours: list[HeatPlanHour] = field(default_factory=list)
    cost_eur: float = 0.0
    thermostat_cost_eur: float = 0.0
    thermostat_runs: list[bool] = field(default_factory=list)

    @property
    def run_now(self) -> bool:
        return bool(self.hours) and self.hours[0].run

    @property
    def run_hours(self) -> int:
        return sum(1 for h in self.hours if h.run)

    def reason_now(self) -> str:
        if not self.hours:
            return "geen plan"
        first = self.hours[0]
        cheap = first.price / first.cop
        if first.run:
            later = [h for h in self.hours[1:] if not h.run]
            if first.room_end < first.setpoint:
                return "kamer zakt onder het programma, nu stoken"
            if later:
                dear = max(later, key=lambda h: h.price / h.cop)
                return (
                    f"vooruit laden: nu {cheap * 100:.1f} ct per kWh warmte, "
                    f"later tot {dear.price / dear.cop * 100:.1f} ct"
                )
            return f"stoken: nu {cheap * 100:.1f} ct per kWh warmte"
        nxt = next((h for h in self.hours if h.run), None)
        if nxt is None:
            return "warm genoeg voor de hele planning"
        return (
            f"wachten tot {nxt.start.strftime('%H:%M')}: dan "
            f"{nxt.price / nxt.cop * 100:.1f} ct per kWh warmte, nu {cheap * 100:.1f} ct"
        )


def _simulate_cost(
    hours: list[HeatHour], params: HouseParams, room: float, runs: list[bool], was_running: bool
) -> float:
    """Kosten van een stookreeks, inclusief comfortstraf en de warmte die overblijft."""
    cost = 0.0
    running = was_running
    for hour, run in zip(hours, runs, strict=True):
        nxt = params.next_temp(room, hour, run)
        if run:
            cost += params.run_kwh_heat / params.cop(hour) * hour.price
            if not running:
                cost += params.start_penalty_eur
        lower = hour.setpoint - params.band_below
        if nxt < lower:
            cost += COMFORT_PENALTY_EUR * (lower - nxt)
        room, running = nxt, run
    # Warmte die aan het eind meer of minder in huis zit, telt tegen de gemiddelde warmteprijs.
    mean_heat_cost = sum(h.price / params.cop(h) for h in hours) / len(hours)
    return cost - (room - hours[-1].setpoint) * params.kwh_per_degree * mean_heat_cost


def thermostat_runs(hours: list[HeatHour], params: HouseParams, room: float) -> list[bool]:
    """Wat een gewone thermostaat zou doen: stoken zodra de kamer onder het programma is."""
    runs: list[bool] = []
    for hour in hours:
        run = room < hour.setpoint
        runs.append(run)
        room = params.next_temp(room, hour, run)
    return runs


def make_heat_plan(
    hours: list[HeatHour], params: HouseParams, room_now: float, was_running: bool = False
) -> HeatPlan:
    """Goedkoopste stookplan dat de kamer binnen de marges houdt."""
    hours = hours[:MAX_HOURS]
    if not hours:
        return HeatPlan()
    lo = min(min(h.setpoint for h in hours) - params.band_below, room_now) - 2.0
    hi = max(max(h.setpoint for h in hours) + params.band_above, room_now) + 2.0
    n = int(round((hi - lo) / STEP_C)) + 1

    def idx(t: float) -> int:
        return max(0, min(n - 1, int(round((t - lo) / STEP_C))))

    def temp(i: int) -> float:
        return lo + i * STEP_C

    heat_cost = [h.price / params.cop(h) for h in hours]
    mean_heat_cost = sum(heat_cost) / len(heat_cost)
    end_setpoint = hours[-1].setpoint

    # value[i][r]: minimale kosten vanaf uur k met kamer i en r = draaide vorig uur.
    inf = float("inf")
    value = [[-(temp(i) - end_setpoint) * params.kwh_per_degree * mean_heat_cost] * 2 for i in range(n)]
    choice: list[list[list[bool]]] = []
    for k in range(len(hours) - 1, -1, -1):
        hour = hours[k]
        lower = hour.setpoint - params.band_below
        upper = hour.setpoint + params.band_above
        new_value = [[inf, inf] for _ in range(n)]
        new_choice = [[False, False] for _ in range(n)]
        for i in range(n):
            room = temp(i)
            for prev in (0, 1):
                best, best_run = inf, False
                for run in (False, True):
                    nxt = params.next_temp(room, hour, run)
                    if run and room >= upper:
                        continue  # boven de bovengrens niet bijstoken
                    cost = 0.0
                    if run:
                        cost += params.run_kwh_heat * heat_cost[k]
                        if not prev:
                            cost += params.start_penalty_eur
                    if nxt < lower:
                        cost += COMFORT_PENALTY_EUR * (lower - nxt)
                    cost += value[idx(nxt)][1 if run else 0]
                    if cost < best:
                        best, best_run = cost, run
                new_value[i][prev] = best
                new_choice[i][prev] = best_run
        value = new_value
        choice.insert(0, new_choice)

    plan = HeatPlan()
    room = room_now
    prev = 1 if was_running else 0
    runs: list[bool] = []
    for k, hour in enumerate(hours):
        run = choice[k][idx(room)][prev]
        nxt = params.next_temp(room, hour, run)
        cop = params.cop(hour)
        cost = params.run_kwh_heat / cop * hour.price if run else 0.0
        plan.hours.append(
            HeatPlanHour(
                start=hour.start,
                run=run,
                room_end=nxt,
                price=hour.price,
                cop=cop,
                cost_eur=cost,
                setpoint=hour.setpoint,
                forecast=hour.forecast,
            )
        )
        runs.append(run)
        room, prev = nxt, 1 if run else 0
    plan.cost_eur = _simulate_cost(hours, params, room_now, runs, was_running)
    plan.thermostat_runs = thermostat_runs(hours, params, room_now)
    plan.thermostat_cost_eur = _simulate_cost(hours, params, room_now, plan.thermostat_runs, was_running)
    return plan


def setpoint_at(program: Any, moment: datetime, default: float) -> float:
    """Setpoint uit het weekschema van de VRC (attribuut time_program_heating).

    Dagen 'monday'..'sunday', elk een lijst blokken met start_time/end_time in minuten
    sinds middernacht en setpoint. Blokken zijn dicts of objecten.
    """
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if program is None:
        return default
    day = days[moment.weekday()]
    blocks = program.get(day) if isinstance(program, dict) else getattr(program, day, None)
    if not blocks:
        return default
    minute = moment.hour * 60 + moment.minute
    for block in blocks:
        if isinstance(block, dict):
            start, end, sp = block.get("start_time"), block.get("end_time"), block.get("setpoint")
        else:
            start = getattr(block, "start_time", None)
            end = getattr(block, "end_time", None)
            sp = getattr(block, "setpoint", None)
        try:
            if int(start) <= minute < int(end):
                return float(sp)
        except (TypeError, ValueError):
            continue
    return default


def summary(plan: HeatPlan, params: HouseParams, room_now: float | None) -> dict[str, Any]:
    if not plan.hours:
        return {"advies": "geen plan"}
    saving = plan.thermostat_cost_eur - plan.cost_eur
    return {
        "advies": "stoken" if plan.run_now else "niet stoken",
        "reden": plan.reason_now(),
        "kamer_nu": None if room_now is None else round(room_now, 2),
        "stookuren": plan.run_hours,
        "kosten_plan_eur": round(plan.cost_eur, 2),
        "kosten_gewone_thermostaat_eur": round(plan.thermostat_cost_eur, 2),
        "besparing_eur": round(saving, 2),
        "uren": len(plan.hours),
        "model": {
            "bron": params.source,
            "kwh_warmte_per_graad": round(params.kwh_per_degree, 1),
            "warmteverlies_w_per_graad": round(params.loss_w_per_k),
            "warmte_per_draaiuur_kwh": round(params.run_kwh_heat, 2),
            "cop": f"{params.cop_a:.2f} + {params.cop_b:.3f} x T",
            "marge_onder": params.band_below,
            "marge_boven": params.band_above,
        },
        "plan": [h.as_dict() for h in plan.hours],
    }
