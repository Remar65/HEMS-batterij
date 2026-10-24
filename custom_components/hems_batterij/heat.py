"""Verwachte stroom voor verwarming, uit de weersverwachting.

Het huisprofiel (forecast.LoadProfile) kent alleen "normaal" verbruik per kwartier van de
week. Verwarming hangt vooral af van het weer: een koude dag over drie dagen kost veel
meer stroom dan het gemiddelde zegt. Deze module leert daarom apart:

- per dag: hoeveel kWh de warmtepomp aan verwarming gebruikte, naast de gemiddelde
  buitentemperatuur van die dag. Een lijn door die punten (koud = meer) geeft de
  verwachte kWh voor een dag met een voorspelde temperatuur;
- per uur van de dag: welk deel van die kWh meestal in dat uur valt. De vloerbuffer
  stookt bij voorkeur op goedkope of zonnige uren; dat patroon komt zo vanzelf mee.
  Koude en zachte dagen krijgen elk een eigen patroon.

Puur Python (geen Home Assistant-imports), zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

# Minimaal aantal dagen, en dagen waarop echt gestookt werd, voordat de lijn telt.
MIN_DAYS = 7
MIN_HEATING_DAYS = 3
MAX_DAYS = 90
# Grens tussen "koud" en "zacht" voor het uurpatroon (°C daggemiddelde).
COLD_BELOW = 6.0
_SHAPE_ALPHA = 0.15


def parse_open_meteo(payload: Any) -> dict[datetime, float]:
    """Open-Meteo `hourly=temperature_2m&timeformat=unixtime`: {uur-start UTC: °C}."""
    if not isinstance(payload, dict):
        return {}
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return {}
    times, temps = hourly.get("time"), hourly.get("temperature_2m")
    if not isinstance(times, list) or not isinstance(temps, list):
        return {}
    result: dict[datetime, float] = {}
    for t, v in zip(times, temps, strict=False):
        if v is None:
            continue
        try:
            result[datetime.fromtimestamp(int(t), tz=UTC)] = float(v)
        except (TypeError, ValueError, OverflowError):
            continue
    return result


def daily_means(temps: dict[datetime, float], tz: Any) -> dict[date, float]:
    """Gemiddelde temperatuur per lokale kalenderdag (alleen dagen met minstens 20 uur)."""
    buckets: dict[date, list[float]] = {}
    for t, v in temps.items():
        buckets.setdefault(t.astimezone(tz).date(), []).append(v)
    return {d: sum(v) / len(v) for d, v in buckets.items() if len(v) >= 20}


def _band(mean_temp: float) -> str:
    return "koud" if mean_temp < COLD_BELOW else "zacht"


@dataclass
class HeatModel:
    """Leert kWh verwarming per dag tegen de buitentemperatuur, en de verdeling over de uren."""

    # dag (iso) -> [kWh verwarming, gemiddelde buitentemperatuur]
    days: dict[str, list[float]] = field(default_factory=dict)
    # "koud"/"zacht" -> 24 aandelen (som 1)
    shapes: dict[str, list[float]] = field(default_factory=dict)

    def add_day(self, day: date, kwh: float, mean_temp: float, hourly_kwh: list[float]) -> None:
        if kwh < 0:
            return
        self.days[day.isoformat()] = [round(kwh, 3), round(mean_temp, 2)]
        for old in sorted(self.days)[:-MAX_DAYS]:
            del self.days[old]
        total = sum(hourly_kwh)
        if len(hourly_kwh) != 24 or total < 0.2:
            return  # een dag zonder (noemenswaardig) stoken zegt niets over het patroon
        share = [max(0.0, h) / total for h in hourly_kwh]
        band = _band(mean_temp)
        old_shape = self.shapes.get(band)
        if old_shape is None:
            self.shapes[band] = [round(s, 4) for s in share]
        else:
            self.shapes[band] = [
                round(o + _SHAPE_ALPHA * (s - o), 4) for o, s in zip(old_shape, share, strict=True)
            ]

    def fit(self) -> tuple[float, float] | None:
        """kWh = a + b x temperatuur (b < 0), of None als er te weinig te leren valt."""
        points = list(self.days.values())
        if len(points) < MIN_DAYS or sum(1 for kwh, _ in points if kwh >= 0.5) < MIN_HEATING_DAYS:
            return None
        xs = [t for _, t in points]
        ys = [kwh for kwh, _ in points]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx < 1.0:
            return None  # te weinig verschil in temperatuur om iets te zien
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
        if b >= 0:
            return None  # warmer = meer stoken is geen verband om op te plannen
        return my - b * mx, b

    def day_kwh(self, mean_temp: float, line: tuple[float, float] | None = None) -> float:
        """Verwachte kWh voor een dag; zonder geleerde lijn het gemeten daggemiddelde."""
        line = line if line is not None else self.fit()
        if line is None:
            if not self.days:
                return 0.0
            return sum(kwh for kwh, _ in self.days.values()) / len(self.days)
        a, b = line
        return max(0.0, a + b * mean_temp)

    def share(self, hour: int, mean_temp: float) -> float:
        shape = self.shapes.get(_band(mean_temp)) or next(iter(self.shapes.values()), None)
        if not shape:
            return 1.0 / 24.0
        return shape[hour]

    def kwh_between(
        self,
        start: datetime,
        end: datetime,
        day_temps: dict[date, float],
        tz: Any,
        line: tuple[float, float] | None = None,
    ) -> float:
        """Verwachte kWh verwarming in [start, end), uur voor uur in lokale tijd.

        Geef `line` (uit fit()) mee als je dit voor veel blokken achter elkaar vraagt.
        """
        total = 0.0
        cursor = start.astimezone(tz)
        end = end.astimezone(tz)
        while cursor < end:
            hour_end = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            step_end = min(hour_end, end)
            mean = day_temps.get(cursor.date())
            if mean is not None:
                fraction = (step_end - cursor).total_seconds() / 3600.0
                total += self.day_kwh(mean, line) * self.share(cursor.hour, mean) * fraction
            cursor = step_end
        return total

    def summary(self) -> dict[str, Any]:
        line = self.fit()
        info: dict[str, Any] = {"dagen_gemeten": len(self.days), "geleerd": line is not None}
        if line is not None:
            a, b = line
            info["kwh_per_graad_kouder"] = round(-b, 2)
            info["kwh_bij_0_graden"] = round(max(0.0, a), 1)
            info["kwh_bij_10_graden"] = round(max(0.0, a + 10 * b), 1)
        return info

    def to_dict(self) -> dict[str, Any]:
        return {"days": self.days, "shapes": self.shapes}

    @classmethod
    def from_dict(cls, data: Any) -> HeatModel:
        model = cls()
        if not isinstance(data, dict):
            return model
        days = data.get("days")
        if isinstance(days, dict):
            for k, v in days.items():
                try:
                    kwh, temp = (float(x) for x in v)
                except (TypeError, ValueError):
                    continue
                model.days[str(k)] = [kwh, temp]
        shapes = data.get("shapes")
        if isinstance(shapes, dict):
            for k, v in shapes.items():
                try:
                    values = [float(x) for x in v]
                except (TypeError, ValueError):
                    continue
                if len(values) == 24:
                    model.shapes[str(k)] = values
        return model
