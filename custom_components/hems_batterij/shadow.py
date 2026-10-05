"""Schaduwbatterij: wat de batterijen zouden doen als HEMS ze echt aanstuurde.

In de meekijkmodus stuurt HBC de echte batterijen. Zou HEMS elk kwartier met
de echte laadstand plannen, dan ziet het bijvoorbeeld 's avonds steeds weer een
volle batterij en blijft het "verkopen" voorstellen, ook als die batterij in
HEMS' eigen plan allang leeg zou zijn. Daardoor zijn voorstel en besparing niet
eerlijk te vergelijken met HBC.

De schaduwbatterij houdt per batterij een laadstand op papier bij die alleen
verandert door wat HEMS zelf voorstelt. Plan en regellus rekenen met die stand;
capaciteit, grenzen, rendement en alarmen komen gewoon van de echte batterij.

Geen Home Assistant-imports, zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import math
from typing import Any

from .controller import BatteryState


@dataclass
class ShadowBattery:
    """Laadstand op papier per batterij, in kWh."""

    soc_kwh: dict[str, float] = field(default_factory=dict)
    since: datetime | None = None

    def sync(self, batteries: list[BatteryState], now: datetime) -> None:
        """Nieuwe batterijen beginnen op hun echte stand; verdwenen batterijen vervallen."""
        names = {b.name for b in batteries}
        for name in list(self.soc_kwh):
            if name not in names:
                del self.soc_kwh[name]
        for b in batteries:
            if b.name not in self.soc_kwh and b.soc_pct is not None:
                self.soc_kwh[b.name] = b.soc_kwh
                if self.since is None:
                    self.since = now

    def apply(self, batteries: list[BatteryState]) -> list[BatteryState]:
        """Kopieën van de echte batterijen, maar met de schaduwstand."""
        result = []
        for b in batteries:
            kwh = self.soc_kwh.get(b.name)
            if kwh is None or b.soc_pct is None or b.capacity_kwh <= 0:
                result.append(b)
            else:
                result.append(replace(b, soc_pct=100.0 * kwh / b.capacity_kwh))
        return result

    def integrate(
        self, per_battery_w: dict[str, float], seconds: float, batteries: list[BatteryState]
    ) -> None:
        """Laat het voorgestelde vermogen `seconds` lang op de schaduwstand inwerken.

        + = ontladen, - = laden (AC-zijde). Het rendement wordt gelijk over laden
        en ontladen verdeeld (wortel van het rondrendement).
        """
        if seconds <= 0:
            return
        by_name = {b.name: b for b in batteries}
        for name, watt in per_battery_w.items():
            b = by_name.get(name)
            if b is None or name not in self.soc_kwh or not watt:
                continue
            leg = math.sqrt(max(0.01, min(1.0, b.efficiency)))
            ac_kwh = watt * seconds / 3_600_000.0
            delta = -ac_kwh / leg if ac_kwh > 0 else -ac_kwh * leg
            low = b.capacity_kwh * b.soc_min_pct / 100.0
            high = b.capacity_kwh * b.soc_max_pct / 100.0
            self.soc_kwh[name] = min(high, max(low, self.soc_kwh[name] + delta))

    @property
    def total_kwh(self) -> float | None:
        return sum(self.soc_kwh.values()) if self.soc_kwh else None

    def to_dict(self) -> dict[str, Any]:
        return {"soc_kwh": dict(self.soc_kwh), "since": self.since.isoformat() if self.since else None}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> ShadowBattery:
        if not raw:
            return cls()
        since = raw.get("since")
        try:
            since_dt = datetime.fromisoformat(since) if since else None
        except ValueError:
            since_dt = None
        soc = {str(k): float(v) for k, v in (raw.get("soc_kwh") or {}).items()}
        return cls(soc_kwh=soc, since=since_dt)
