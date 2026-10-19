"""Schaduw-HBC: wat HBC met de door HEMS gestuurde batterij had gedaan.

Zodra HEMS een batterij echt aanstuurt, draait de vergelijking om: de echte
batterij volgt HEMS, en op papier houden we bij wat diezelfde batterij onder HBC
had gedaan. HBC blijft voor de andere batterij gewoon draaien en publiceert zijn
actieve deelstrategie (zelfverbruik, laden, verkopen, ...). Die volgen we hier,
met de grenzen die HBC zelf per batterij gebruikt.

Het is een benadering: HBC regelt met een eigen PID-regelaar en deelt het werk
anders over batterijen. Hier reageert de schaduwbatterij direct op wat het huis
zonder de echte HEMS-batterij van het net had gevraagd.

Geen Home Assistant-imports, zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass

from .controller import BatteryState, ControllerSettings, allocate
from .planner import MODE_GRID_CHARGE, MODE_SELL

SUB_SELF_CONSUMPTION = "Self-consumption"
SUB_CHARGE_PV = "Charge PV"
SUB_CHARGE = "Charge"
SUB_SELL = "Sell"
SUB_ZERO_IMPORT = "Zero import"
SUB_IDLE = ("Full stop", "Standby / peak shave", "Standby")

# Deelstrategieën waarin HBC netafname dekt uit zijn eigen batterij, of een overschot
# erin opslaat. Laadt HEMS dan uit het net (of verkoopt het), dan ontlaadt (of laadt)
# HBC zijn batterij om dat op P1 weg te werken: energie gaat van de ene batterij naar
# de andere en kost alleen rendement.
_COVERS_IMPORT = (SUB_SELF_CONSUMPTION, SUB_ZERO_IMPORT, SUB_SELL)
_ABSORBS_EXPORT = (SUB_SELF_CONSUMPTION, SUB_CHARGE_PV, SUB_CHARGE)


@dataclass
class HbcTargets:
    charge_target_soc: float = 100.0  # "Charge": laden tot deze stand
    sell_target_soc: float = 0.0  # "Sell": verkopen tot deze stand


def hbc_total_w(
    sub_strategy: str | None,
    residual_w: float,
    batteries: list[BatteryState],
    targets: HbcTargets,
) -> float:
    """Gewenst totaalvermogen onder HBC (+ = ontladen).

    residual_w: wat het huis zonder de gestuurde batterij(en) van het net vraagt
    (+ = afname, - = overschot).
    """
    sub = (sub_strategy or SUB_SELF_CONSUMPTION).strip()
    can_dis = sum(b.max_discharge_w for b in batteries if b.can_discharge())
    can_chg = sum(b.max_charge_w for b in batteries if b.can_charge())
    soc_pct = _avg_soc(batteries)

    if sub in SUB_IDLE:
        return 0.0
    if sub == SUB_CHARGE:
        if soc_pct is not None and soc_pct >= targets.charge_target_soc:
            return _clamp(min(0.0, residual_w), can_chg, can_dis)
        return -can_chg
    if sub == SUB_SELL:
        if soc_pct is not None and soc_pct > targets.sell_target_soc:
            return can_dis
        return _clamp(max(0.0, residual_w), can_chg, can_dis)  # daarna "Zero import"
    if sub == SUB_CHARGE_PV:
        return _clamp(min(0.0, residual_w), can_chg, can_dis)
    if sub == SUB_ZERO_IMPORT:
        return _clamp(max(0.0, residual_w), can_chg, can_dis)
    return _clamp(residual_w, can_chg, can_dis)


def hbc_step(
    sub_strategy: str | None,
    residual_w: float,
    batteries: list[BatteryState],
    targets: HbcTargets,
    settings: ControllerSettings,
) -> dict[str, float]:
    """Vermogen per schaduwbatterij onder HBC."""
    return allocate(hbc_total_w(sub_strategy, residual_w, batteries, targets), batteries, settings)


def hbc_counteracts(mode: str, sub_strategy: str | None, others: list[BatteryState]) -> bool:
    """Werkt HBC met de andere batterij(en) een vaste HEMS-opdracht op P1 tegen?"""
    sub = (sub_strategy or SUB_SELF_CONSUMPTION).strip()
    if mode == MODE_GRID_CHARGE:
        return sub in _COVERS_IMPORT and any(b.usable and b.can_discharge() for b in others)
    if mode == MODE_SELL:
        return sub in _ABSORBS_EXPORT and any(b.usable and b.can_charge() for b in others)
    return False


def _clamp(watt: float, max_charge_w: float, max_discharge_w: float) -> float:
    return max(-max_charge_w, min(max_discharge_w, watt))


def _avg_soc(batteries: list[BatteryState]) -> float | None:
    usable = [b for b in batteries if b.soc_pct is not None and b.capacity_kwh > 0]
    if not usable:
        return None
    capacity = sum(b.capacity_kwh for b in usable)
    return 100.0 * sum(b.soc_kwh for b in usable) / capacity
