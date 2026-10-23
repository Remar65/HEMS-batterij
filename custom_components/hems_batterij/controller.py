"""Snelle regellus: van plan + actuele P1-meting naar een vermogen per batterij.

Dit rekent alleen; het sturen zelf gebeurt in actuator.py.

Tekenafspraak overal: batterijvermogen + = ontladen (levert aan huis/net),
- = laden. P1 + = afname uit het net, - = teruglevering.

Geen Home Assistant-imports, zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

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
    other_active_w: float = 50.0  # een niet door HEMS gestuurde batterij telt als actief boven dit
    other_share: float = 0.5  # deel van de P1-afwijking dat HEMS oppakt als een ander ook regelt


def desired_total_w(
    target: ControlTarget,
    p1_w: float,
    battery_total_w: float,
    previous_total_w: float | None,
    settings: ControllerSettings,
    other_battery_w: float = 0.0,
) -> float:
    """Gewenst totaalvermogen van alle door HEMS gestuurde batterijen samen.

    other_battery_w is het vermogen van batterijen die iemand anders regelt (HBC),
    + = ontladen. Die regelen op dezelfde P1-meting. Om niet tegen elkaar in te
    werken pakt HEMS dan maar een deel van de afwijking op, en gaat het nooit laden
    terwijl de ander ontlaadt (of andersom): dat zou alleen rendement kosten.
    """
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
        other_active = abs(other_battery_w) >= settings.other_active_w
        if other_active:
            error *= settings.other_share
        raw = battery_total_w + error
        if previous_total_w is not None:
            raw = previous_total_w + (1.0 - settings.smoothing) * (raw - previous_total_w)
        total = raw
        if other_active and (total > 0) != (other_battery_w > 0) and total != 0:
            total = 0.0
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


@dataclass
class _Cap:
    watt: float
    since: datetime


class PowerCapLearner:
    """Leert wanneer een batterij structureel minder levert dan gevraagd.

    Voorbeeld: een Venus E V3 die na een storing terugvalt op 800 W ontladen, terwijl
    de ingestelde grens 2500 W zegt. Zonder dit blijft HEMS het grootste deel aan die
    batterij vragen en komt het tekort uit het net, terwijl de andere batterij nog
    ruimte heeft. Na `confirm_seconds` aanhoudend tekort geldt wat hij wél levert als
    grens; na `retry_after` proberen we het opnieuw met de gewone grens.
    """

    def __init__(
        self,
        shortfall_w: float = 150.0,
        confirm_seconds: float = 30.0,
        retry_after: timedelta = timedelta(minutes=30),
    ) -> None:
        self.shortfall_w = shortfall_w
        self.confirm_seconds = confirm_seconds
        self.retry_after = retry_after
        self._caps: dict[tuple[str, str], _Cap] = {}
        self._short_since: dict[tuple[str, str], datetime] = {}

    def observe(self, name: str, commanded_w: float, actual_w: float | None, now: datetime) -> str | None:
        """Vergelijk de vorige opdracht met wat de batterij nu doet.

        Geeft een korte tekst terug als er net een grens is geleerd.
        """
        if actual_w is None or abs(commanded_w) < self.shortfall_w:
            for direction in ("ontladen", "laden"):
                self._short_since.pop((name, direction), None)
            return None
        direction = "ontladen" if commanded_w > 0 else "laden"
        key = (name, direction)
        delivered = max(0.0, actual_w if commanded_w > 0 else -actual_w)
        if abs(commanded_w) - delivered < self.shortfall_w:
            self._short_since.pop(key, None)
            return None
        start = self._short_since.setdefault(key, now)
        if (now - start).total_seconds() < self.confirm_seconds:
            return None
        self._short_since.pop(key, None)
        self._caps[key] = _Cap(watt=round(delivered, 0), since=now)
        return f"{name} levert bij {direction} maar {delivered:.0f} W van {abs(commanded_w):.0f} W gevraagd"

    def caps(self, now: datetime) -> dict[tuple[str, str], float]:
        for key in [k for k, c in self._caps.items() if now - c.since >= self.retry_after]:
            del self._caps[key]
        return {key: cap.watt for key, cap in self._caps.items()}

    def apply(self, batteries: list[BatteryState], now: datetime) -> list[BatteryState]:
        """Dezelfde batterijen, met geleerde grenzen in plaats van de ingestelde."""
        caps = self.caps(now)
        result = []
        for b in batteries:
            down = caps.get((b.name, "ontladen"))
            up = caps.get((b.name, "laden"))
            result.append(
                replace(
                    b,
                    max_discharge_w=min(b.max_discharge_w, down) if down is not None else b.max_discharge_w,
                    max_charge_w=min(b.max_charge_w, up) if up is not None else b.max_charge_w,
                )
            )
        return result


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
    other_battery_w: float = 0.0,
) -> ControlResult:
    """Eén doorloop van de snelle regellus.

    `batteries` zijn de batterijen waarover HEMS beslist; other_battery_w is het
    vermogen van de overige (door HBC geregelde) batterijen, zie desired_total_w.
    """
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
    total = desired_total_w(target, p1_w, battery_total, previous_total_w, settings, other_battery_w)
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
    "PowerCapLearner",
    "allocate",
    "control_step",
    "desired_total_w",
    "safety_reasons",
]
