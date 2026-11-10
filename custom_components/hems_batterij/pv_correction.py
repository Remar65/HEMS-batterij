"""Zelflerende uurcorrectie op de zonverwachting.

Bomen schaduwen de panelen 's middags (vanaf ~13 uur) en de zon zakt tot eind
december nog verder. Helios en Solcast zitten daardoor per uur van de dag
stelselmatig naast de echte opbrengst: 20 okt - 9 nov 2026 gaf Helios 's ochtends
~15% te weinig en om 15 uur ~50% te veel. Eén dagfactor lost dat niet op; een
factor per uur wel. Die factor leren we hier zelf, per bron, over de laatste
week, zodat hij meeschuift met de stand van de zon.

Puur Python (geen Home Assistant-imports), zodat dit los te testen is.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# Over hoeveel dagen we leren. Getoetst op 20 okt - 9 nov 2026 (elke dag alleen met de
# dagen ervoor): 7 dagen gaf de kleinste fout (Solcast per uur 0,203 kWh, per dag 1,11),
# 14 dagen 0,217/1,25 en 21 dagen 0,219/1,35. De zon zakt in het najaar snel; een kort
# venster schuift beter mee.
WINDOW_DAYS = 7
# Pas een factor gebruiken met genoeg dagen en genoeg verwachte opbrengst in dat uur.
MIN_DAYS = 5
MIN_FORECAST_KWH = 0.5
# Onder dit (kWh in een uur) telt een uur niet mee: nacht of vrijwel donker.
MIN_HOUR_KWH = 0.01
# Nooit wilder dan dit; een kapotte meting mag de verwachting niet wegvagen.
MIN_FACTOR = 0.1
MAX_FACTOR = 1.5


class PvHourCorrection:
    """Per bron en per uur van de dag: werkelijke kWh gedeeld door verwachte kWh."""

    def __init__(self) -> None:
        # bron -> datum -> uur -> [werkelijk_kwh, verwacht_kwh]
        self._data: dict[str, dict[date, dict[int, list[float]]]] = {}

    def record(self, source: str, hour_start: datetime, actual_kwh: float, forecast_kwh: float) -> None:
        """Eén afgelopen uur bijschrijven (hour_start in lokale tijd)."""
        if actual_kwh < MIN_HOUR_KWH and forecast_kwh < MIN_HOUR_KWH:
            return
        days = self._data.setdefault(source, {})
        days.setdefault(hour_start.date(), {})[hour_start.hour] = [
            max(0.0, actual_kwh),
            max(0.0, forecast_kwh),
        ]
        self.prune(hour_start.date())

    def prune(self, today: date) -> None:
        for days in self._data.values():
            for day in [d for d in days if (today - d).days >= WINDOW_DAYS]:
                del days[day]

    def factor(self, source: str, hour: int) -> float | None:
        """Geleerde factor voor dit uur, of None als er nog te weinig gegevens zijn."""
        actual = forecast = 0.0
        n = 0
        for hours in self._data.get(source, {}).values():
            pair = hours.get(hour)
            if pair is None or pair[1] < MIN_HOUR_KWH:
                continue
            actual += pair[0]
            forecast += pair[1]
            n += 1
        if n < MIN_DAYS or forecast < MIN_FORECAST_KWH:
            return None
        return min(MAX_FACTOR, max(MIN_FACTOR, actual / forecast))

    def factors(self, source: str) -> dict[int, float]:
        """Alle geleerde factoren van een bron, per uur (voor de attributen)."""
        result = {}
        for hour in range(24):
            value = self.factor(source, hour)
            if value is not None:
                result[hour] = round(value, 2)
        return result

    def days(self, source: str) -> int:
        return len(self._data.get(source, {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            source: {
                day.isoformat(): {str(h): [round(a, 4), round(f, 4)] for h, (a, f) in hours.items()}
                for day, hours in days.items()
            }
            for source, days in self._data.items()
        }

    @classmethod
    def from_dict(cls, raw: Any) -> PvHourCorrection:
        result = cls()
        if not isinstance(raw, dict):
            return result
        for source, days in raw.items():
            if not isinstance(days, dict):
                continue
            for day_raw, hours in days.items():
                try:
                    day = date.fromisoformat(day_raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(hours, dict):
                    continue
                for hour_raw, pair in hours.items():
                    try:
                        hour = int(hour_raw)
                        actual, forecast = float(pair[0]), float(pair[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if 0 <= hour < 24:
                        hours_of_day = result._data.setdefault(str(source), {}).setdefault(day, {})
                        hours_of_day[hour] = [actual, forecast]
        return result
