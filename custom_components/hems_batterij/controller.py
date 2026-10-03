"""Snelle regellus: van plan + actuele P1-meting naar een vermogen per batterij.

In deze versie wordt het resultaat alleen getoond en gelogd (meekijkmodus);
er is geen code die iets naar de batterijen stuurt.

Tekenafspraak overal: batterijvermogen + = ontladen (levert aan huis/net),
- = laden. P1 + = afname uit het net, - = teruglevering.

Geen Home Assistant-imports, zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .planner import (
    MODE_GRID_CHARGE,
    MODE_SAFETY,
    MODE_SELF_CONSUMPTION,
    MODE_SELL,
)


@dataclass
class BatteryState:
    name: str
    soc_pct: float | None
    capacity_kwh: float
    ac_power_w: float | None  # + = ontladen
    max_charge_w: float
    max_discharge_w: float
    soc_min_pct: float
    soc_max_pct: float
    available: bool = True
    alarms: list[str] = field(default_factory=list)
    efficiency: float = 0.8

    @property
    def soc_kwh(self) -> float:
        return (self.soc_pct or 0.0) / 100.0 * self.capacity_kwh

    @property
    def energy_above_min_kwh(self) -> float:
        return max(0.0, (self.soc_pct or 0.0) - self.soc_min_pct) / 100.0 * self.capacity_kwh

    @property
    def room_below_max_kwh(self) -> float:
        return max(0.0, self.soc_max_pct - (self.soc_pct or 0.0)) / 100.0 * self.capacity_kwh

    @property
    def usable(self) -> bool:
        return self.available and not self.alarms and self.soc_pct is not None

    def can_discharge(self) -> bool:
        return self.usable and self.energy_above_min_kwh > 0.05

    def can_charge(self) -> bool:
        return self.usable and self.room_below_max_kwh > 0.05


@dataclass
class ControlTarget:
    """Opdracht uit het plan voor het lopende kwartier."""

    mode: str
    fixed_w: float | None = None
    max_charge_w: float = 0.0
    max_discharge_w: float = 0.0
    reason: str = ""


@dataclass
class ControlResult:
    mode: str
    total_w: float
    per_battery_w: dict[str, float]
    reason: str
    safety_reasons: list[str] = field(default_factory=list)


@dataclass
class ControllerSettings:
    deadband_w: float = 25.0  # kleine afwijkingen op P1 negeren
    single_battery_below_w: float = 800.0  # onder dit vermogen één batterij gebruiken
    max_grid_import_w: float = 16000.0  # 3x25A met marge
    smoothing: float = 0.5  # 0 = direct, 1 = nooit bijsturen


def desired_total_w(
    target: ControlTarget,
    p1_w: float,
    battery_total_w: float,
    previous_total_w: float | None,
    settings: ControllerSettings,
) -> float:
    """Gewenst totaalvermogen van alle batterijen samen."""
    if target.mode == MODE_SAFETY:
        return 0.0
    if target.mode in (MODE_GRID_CHARGE, MODE_SELL) and target.fixed_w is not None:
        total = target.fixed_w
    else:
        # Zelfverbruik en vasthouden: het huis zou zonder batterij p1 + batterij afnemen;
        # de batterij moet dat compenseren zodat P1 naar 0 gaat.
        error = p1_w
        if abs(error) < settings.deadband_w:
            error = 0.0
        raw = battery_total_w + error
        if previous_total_w is not None:
            raw = previous_total_w + (1.0 - settings.smoothing) * (raw - previous_total_w)
        total = raw
    total = min(total, target.max_discharge_w)
    total = max(total, -target.max_charge_w)
    # Niet zoveel laden dat de hoofdzekering in gevaar komt.
    import_after = p1_w - (total - battery_total_w)
    if import_after > settings.max_grid_import_w and total < 0:
        total = min(0.0, total + (import_after - settings.max_grid_import_w))
    return total


def allocate(total_w: float, batteries: list[BatteryState], settings: ControllerSettings) -> dict[str, float]:
    """Verdeel het totaalvermogen over de batterijen.

    Kleine vermogens gaan naar één batterij (Marsteks zijn bij laag vermogen
    inefficiënt): bij ontladen de volste, bij laden de leegste; bij gelijke
    stand de batterij met het beste rendement. Grotere vermogens worden naar
    rato van beschikbare energie (ontladen) of ruimte (laden) verdeeld.
    """
    result = {b.name: 0.0 for b in batteries}
    if abs(total_w) < 1.0:
        return result
    discharging = total_w > 0
    if discharging:
        candidates = [b for b in batteries if b.can_discharge()]
        weight = {b.name: b.energy_above_min_kwh for b in candidates}
        limit = {b.name: b.max_discharge_w for b in candidates}
    else:
        candidates = [b for b in batteries if b.can_charge()]
        weight = {b.name: b.room_below_max_kwh for b in candidates}
        limit = {b.name: b.max_charge_w for b in candidates}
    if not candidates:
        return result

    need = abs(total_w)
    ranked = sorted(candidates, key=lambda b: (weight[b.name], b.efficiency), reverse=True)
    if need < settings.single_battery_below_w or len(candidates) == 1:
        first = ranked[0]
        give = min(need, limit[first.name])
        result[first.name] = give
        need -= give
        for b in ranked[1:]:
            if need <= 0:
                break
            give = min(need, limit[b.name])
            result[b.name] = give
            need -= give
    else:
        remaining = list(candidates)
        while need > 0.5 and remaining:
            total_weight = sum(weight[b.name] for b in remaining) or float(len(remaining))
            capped = []
            for b in remaining:
                share = need * ((weight[b.name] / total_weight) if total_weight else 1 / len(remaining))
                room = limit[b.name] - result[b.name]
                if share >= room:
                    capped.append(b)
            if not capped:
                for b in remaining:
                    share = need * ((weight[b.name] / total_weight) if total_weight else 1 / len(remaining))
                    result[b.name] += share
                need = 0.0
                break
            for b in capped:
                room = limit[b.name] - result[b.name]
                result[b.name] += room
                need -= room
                remaining.remove(b)

    sign = 1.0 if discharging else -1.0
    return {name: round(sign * w, 0) for name, w in result.items()}


def safety_reasons(
    batteries: list[BatteryState],
    p1_available: bool,
    plan_available: bool,
) -> list[str]:
    """Redenen om niets voor te stellen (vangnetten)."""
    reasons: list[str] = []
    if not p1_available:
        reasons.append("P1-meting ontbreekt of is verouderd")
    if not plan_available:
        reasons.append("geen geldig plan (prijzen ontbreken?)")
    for b in batteries:
        if not b.available:
            reasons.append(f"{b.name}: geen actuele data")
        for alarm in b.alarms:
            reasons.append(f"{b.name}: {alarm}")
    usable = [b for b in batteries if b.usable]
    if not usable and batteries:
        reasons.append("geen enkele batterij bruikbaar")
    return reasons


def control_step(
    target: ControlTarget | None,
    p1_w: float | None,
    batteries: list[BatteryState],
    previous_total_w: float | None,
    settings: ControllerSettings,
) -> ControlResult:
    """Eén doorloop van de snelle regellus."""
    reasons = safety_reasons(batteries, p1_available=p1_w is not None, plan_available=target is not None)
    usable = [b for b in batteries if b.usable]
    # Zonder P1 of zonder bruikbare batterij valt er niets te regelen.
    # Ontbreekt alleen het plan, dan vallen we hieronder terug op zelfverbruik.
    if p1_w is None or not usable:
        return ControlResult(
            mode=MODE_SAFETY,
            total_w=0.0,
            per_battery_w={b.name: 0.0 for b in batteries},
            reason="; ".join(reasons) or "vangnet",
            safety_reasons=reasons,
        )
    if target is None:
        # Geen plan: terugvallen op gewoon zelfverbruik, zoals HBC zonder prijzen zou doen.
        target = ControlTarget(
            mode=MODE_SELF_CONSUMPTION,
            max_charge_w=sum(b.max_charge_w for b in usable),
            max_discharge_w=sum(b.max_discharge_w for b in usable),
            reason="geen plan; terugval op zelfverbruik",
        )
    # Grenzen niet hoger dan wat de bruikbare batterijen aankunnen.
    target = ControlTarget(
        mode=target.mode,
        fixed_w=target.fixed_w,
        max_charge_w=min(target.max_charge_w, sum(b.max_charge_w for b in usable if b.can_charge())),
        max_discharge_w=min(
            target.max_discharge_w, sum(b.max_discharge_w for b in usable if b.can_discharge())
        ),
        reason=target.reason,
    )
    battery_total = sum(b.ac_power_w or 0.0 for b in batteries)
    total = desired_total_w(target, p1_w, battery_total, previous_total_w, settings)
    per_battery = allocate(total, batteries, settings)
    return ControlResult(
        mode=target.mode,
        total_w=round(sum(per_battery.values()), 0),
        per_battery_w=per_battery,
        reason=target.reason,
        safety_reasons=reasons,
    )


__all__ = [
    "BatteryState",
    "ControlResult",
    "ControlTarget",
    "ControllerSettings",
    "allocate",
    "control_step",
    "desired_total_w",
    "safety_reasons",
]
