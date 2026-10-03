"""Voorspellingen: zon (Solcast) en huisverbruik (zelflerend profiel).

Puur Python (geen Home Assistant-imports), zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

SOLCAST_PERIOD = timedelta(minutes=30)


@dataclass(frozen=True)
class PvPeriod:
    start: datetime
    end: datetime
    kw: float  # gemiddeld vermogen in de periode


def parse_solcast_detailed(attributes: dict[str, Any] | None, field: str = "pv_estimate") -> list[PvPeriod]:
    """Lees `detailedForecast` (halfuurs, kW-gemiddelde) van een Solcast-dagsensor."""
    if not attributes:
        return []
    periods: list[PvPeriod] = []
    for item in attributes.get("detailedForecast") or []:
        try:
            start = item["period_start"]
            if isinstance(start, str):
                start = datetime.fromisoformat(start)
            kw = float(item.get(field, item.get("pv_estimate", 0.0)) or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        periods.append(PvPeriod(start=start, end=start + SOLCAST_PERIOD, kw=max(kw, 0.0)))
    periods.sort(key=lambda p: p.start)
    return periods


def pv_kwh_between(periods: list[PvPeriod], start: datetime, end: datetime) -> float:
    """Verwachte PV-opbrengst (kWh) in [start, end) op basis van overlap."""
    total = 0.0
    for p in periods:
        if p.end <= start or p.start >= end:
            continue
        overlap = (min(p.end, end) - max(p.start, start)).total_seconds() / 3600.0
        total += p.kw * overlap
    return total


class LoadProfile:
    """Huisverbruik per kwartier van de week, geleerd met een voortschrijdend gemiddelde.

    Het huisverbruik is alles wat het huis zelf vraagt (inclusief warmtepomp),
    dus net + zon + batterij-ontlading. Zolang een vak nog niet gevuld is,
    valt het profiel terug op het gemiddelde van hetzelfde kwartier op andere
    dagen, en daarna op `default_w`.
    """

    SLOTS_PER_DAY = 96

    def __init__(self, default_w: float = 400.0, alpha: float = 0.25) -> None:
        self.default_w = default_w
        self.alpha = alpha
        self._bins: dict[str, float] = {}

    @staticmethod
    def _key(weekday: int, quarter: int) -> str:
        return f"{weekday}:{quarter}"

    @staticmethod
    def quarter_of_day(moment: datetime) -> int:
        return moment.hour * 4 + moment.minute // 15

    def update(self, moment: datetime, avg_w: float) -> None:
        if avg_w < 0:
            # Negatief huisverbruik kan niet; meetfout (bijvoorbeeld verlopen data).
            return
        key = self._key(moment.weekday(), self.quarter_of_day(moment))
        old = self._bins.get(key)
        self._bins[key] = avg_w if old is None else old + self.alpha * (avg_w - old)

    def expected_w(self, moment: datetime) -> float:
        quarter = self.quarter_of_day(moment)
        value = self._bins.get(self._key(moment.weekday(), quarter))
        if value is not None:
            return value
        same_quarter = [v for d in range(7) if (v := self._bins.get(self._key(d, quarter))) is not None]
        if same_quarter:
            return sum(same_quarter) / len(same_quarter)
        return self.default_w

    def kwh_between(self, start: datetime, end: datetime) -> float:
        """Verwacht verbruik in [start, end), per kwartier opgeteld."""
        total = 0.0
        cursor = start
        while cursor < end:
            boundary = cursor.replace(minute=(cursor.minute // 15) * 15, second=0, microsecond=0) + timedelta(
                minutes=15
            )
            step_end = min(boundary, end)
            hours = (step_end - cursor).total_seconds() / 3600.0
            total += self.expected_w(cursor) / 1000.0 * hours
            cursor = step_end
        return total

    @property
    def filled_bins(self) -> int:
        return len(self._bins)

    def to_dict(self) -> dict[str, Any]:
        return {"default_w": self.default_w, "alpha": self.alpha, "bins": dict(self._bins)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, default_w: float = 400.0) -> LoadProfile:
        profile = cls(default_w=default_w)
        if data:
            profile.alpha = float(data.get("alpha", profile.alpha))
            profile._bins = {str(k): float(v) for k, v in (data.get("bins") or {}).items()}
        return profile
