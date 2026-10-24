"""Prijsvoorspelling voor de dagen na de bekende Zonneplan-prijzen.

Zonneplan kent de prijzen tot en met morgen. Een externe voorspelling (nu EpexPredictor,
later jeroen.nl) geeft de kale beursprijs voor de dagen daarna. Deze module:

- leest die voorspelling in;
- rekent de kale prijs om naar wat Zonneplan zou rekenen, met een lineaire
  verhouding die geleerd wordt uit de kwartieren waarvoor beide bekend zijn;
- houdt bij hoe goed eerdere voorspellingen waren, per aantal dagen vooruit, en
  maakt daar een vertrouwen van (0 = negeren, 1 = volledig meetellen);
- dempt de voorspelde prijzen met dat vertrouwen: ze schuiven naar het gemiddelde,
  zodat een onzekere dag weinig prijsverschil overhoudt en de planner er niet
  "voor overmorgen" op gaat laden.

Puur Python (geen Home Assistant-imports), zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .prices import PriceSlot

MAX_LEAD_DAYS = 7
# Vertrouwen per dag vooruit zolang er nog niets gemeten is. Voorzichtig, en aflopend
# met de horizon (de gepubliceerde fout van zulke modellen loopt op van ~1,6 naar ~2,7 ct).
DEFAULT_TRUST = {1: 0.6, 2: 0.5, 3: 0.45, 4: 0.4, 5: 0.35, 6: 0.3, 7: 0.25}
# Hoe snel het gemeten vertrouwen de standaard vervangt: na zoveel gemeten uren telt
# de meting voor de helft mee.
_PRIOR_HOURS = 24.0
# Tariefomrekening als er nog nooit een overlap was: ongeveer Zonneplan 2026
# (kale prijs + inkoopvergoeding + energiebelasting, plus 21% btw).
DEFAULT_TARIFF = (1.21, 0.135, 1.0, 0.02)
_MIN_OVERLAP = 16


@dataclass(frozen=True)
class ForecastPoint:
    """Voorspelde kale beursprijs voor één blok."""

    start: datetime
    end: datetime
    raw: float  # €/kWh, kale beursprijs


def parse_epexpredictor(payload: Any) -> list[ForecastPoint]:
    """EpexPredictor `prices_short`: {"s": [unix-seconden], "t": [€/kWh]} per kwartier."""
    if not isinstance(payload, dict):
        return []
    starts, values = payload.get("s"), payload.get("t")
    if not isinstance(starts, list) or not isinstance(values, list):
        return []
    points: list[ForecastPoint] = []
    for i, (s, t) in enumerate(zip(starts, values, strict=False)):
        try:
            start = datetime.fromtimestamp(float(s), tz=UTC)
            raw = float(t)
        except (TypeError, ValueError, OverflowError):
            continue
        if i + 1 < len(starts):
            try:
                end = datetime.fromtimestamp(float(starts[i + 1]), tz=UTC)
            except (TypeError, ValueError, OverflowError):
                end = start + timedelta(minutes=15)
            if not timedelta(0) < end - start <= timedelta(hours=1):
                end = start + timedelta(minutes=15)
        else:
            end = start + timedelta(minutes=15)
        points.append(ForecastPoint(start=start, end=end, raw=raw))
    points.sort(key=lambda p: p.start)
    return points


@dataclass(frozen=True)
class Tariff:
    """buy = a_buy x kaal + b_buy; kale Zonneplan-prijs (ex. belasting) = a_ex x kaal + b_ex."""

    a_buy: float
    b_buy: float
    a_ex: float
    b_ex: float
    fitted_on: int = 0  # aantal kwartieren waarop dit geleerd is (0 = standaard)

    def buy(self, raw: float) -> float:
        return self.a_buy * raw + self.b_buy

    def ex(self, raw: float) -> float:
        return self.a_ex * raw + self.b_ex

    def to_list(self) -> list[float]:
        return [self.a_buy, self.b_buy, self.a_ex, self.b_ex, float(self.fitted_on)]

    @classmethod
    def from_list(cls, data: Any) -> Tariff | None:
        try:
            a, b, c, d, n = (float(x) for x in data)
        except (TypeError, ValueError):
            return None
        return cls(a, b, c, d, int(n))


def default_tariff() -> Tariff:
    return Tariff(*DEFAULT_TARIFF)


def _linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-6:
        return None
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return a, my - a * mx


def fit_tariff(known: list[PriceSlot], forecast: list[ForecastPoint]) -> Tariff | None:
    """Leer de omrekening kaal -> Zonneplan uit kwartieren die in beide zitten.

    De voorspeller geeft voor dagen waarvan de veiling al geweest is de echte kale prijs;
    daarmee is de verhouding (btw, belasting, opslag) vrijwel exact te leren.
    """
    by_start = {p.start: p.raw for p in forecast}
    xs: list[float] = []
    buys: list[float] = []
    exs: list[float] = []
    for slot in known:
        raw = by_start.get(slot.start)
        if raw is None:
            continue
        xs.append(raw)
        buys.append(slot.buy)
        exs.append(slot.buy_ex_tax)
    if len(xs) < _MIN_OVERLAP:
        return None
    fit_buy, fit_ex = _linear_fit(xs, buys), _linear_fit(xs, exs)
    if fit_buy is None or fit_ex is None:
        return None
    # Btw-factor hoort rond 1,2 te liggen; wijkt het ver af, dan klopt de overlap niet.
    if not 0.8 <= fit_buy[0] <= 1.6:
        return None
    return Tariff(fit_buy[0], fit_buy[1], fit_ex[0], fit_ex[1], len(xs))


def lead_days(start: datetime, today_local: datetime) -> int:
    """Hoeveel kalenderdagen na vandaag (lokale tijd) dit blok valt, minimaal 1."""
    local = start.astimezone(today_local.tzinfo)
    return max(1, min(MAX_LEAD_DAYS, (local.date() - today_local.date()).days))


@dataclass
class ForecastSkill:
    """Meet per aantal dagen vooruit hoe goed de voorspelling het prijsverloop trof.

    Per voorspelling bewaren we de voorspelde Zonneplan-prijs per uur. Zodra Zonneplan
    de echte prijs van dat uur kent, vergelijken we per dag: de fout tegenover de
    spreiding van de echte prijzen rond hun daggemiddelde. Vertrouwen = 1 - fout/spreiding:
    1 is perfect, 0 is niet beter dan "elk uur het daggemiddelde".
    """

    # uitgiftedatum -> {uur-start iso: voorspelde prijs}
    snapshots: dict[str, dict[str, float]] = field(default_factory=dict)
    # dagen vooruit -> [som abs fout, som abs afwijking van daggemiddelde, aantal uren]
    stats: dict[int, list[float]] = field(default_factory=dict)
    # al beoordeelde (uitgiftedatum, dag) paren
    scored: set[str] = field(default_factory=set)

    def record(self, issued: datetime, hourly: dict[datetime, float]) -> None:
        """Bewaar de eerste voorspelling van een dag (latere van dezelfde dag niet)."""
        key = issued.date().isoformat()
        if key in self.snapshots or not hourly:
            return
        self.snapshots[key] = {t.isoformat(): round(v, 5) for t, v in hourly.items()}
        for old in sorted(self.snapshots)[:-(MAX_LEAD_DAYS + 2)]:
            del self.snapshots[old]
        self.scored = {s for s in self.scored if s.split("|")[0] in self.snapshots}

    def score(self, actual: list[PriceSlot], tz: Any) -> int:
        """Vergelijk bewaarde voorspellingen met bekende echte prijzen. Geeft aantal nieuwe dagen."""
        hourly_actual: dict[datetime, list[float]] = {}
        for slot in actual:
            hour = slot.start.astimezone(tz).replace(minute=0, second=0, microsecond=0)
            hourly_actual.setdefault(hour, []).append(slot.buy)
        actual_avg = {h: sum(v) / len(v) for h, v in hourly_actual.items()}
        new = 0
        for issued_key, predicted in self.snapshots.items():
            issued_day = datetime.fromisoformat(issued_key).date()
            per_day: dict[Any, list[tuple[float, float]]] = {}
            for iso, value in predicted.items():
                hour = datetime.fromisoformat(iso).astimezone(tz)
                if hour in actual_avg:
                    per_day.setdefault(hour.date(), []).append((value, actual_avg[hour]))
            for day, pairs in per_day.items():
                tag = f"{issued_key}|{day.isoformat()}"
                if tag in self.scored or len(pairs) < 20:
                    continue  # pas beoordelen als (vrijwel) de hele dag bekend is
                lead = max(1, min(MAX_LEAD_DAYS, (day - issued_day).days))
                mean_actual = sum(a for _, a in pairs) / len(pairs)
                err = sum(abs(p - a) for p, a in pairs)
                spread = sum(abs(a - mean_actual) for _, a in pairs)
                acc = self.stats.setdefault(lead, [0.0, 0.0, 0.0])
                # Oude metingen tellen steeds minder (ongeveer een maand geheugen).
                decay = 0.97
                acc[0] = acc[0] * decay + err
                acc[1] = acc[1] * decay + spread
                acc[2] = acc[2] * decay + len(pairs)
                self.scored.add(tag)
                new += 1
        return new

    def trust(self, lead: int) -> float:
        prior = DEFAULT_TRUST.get(lead, DEFAULT_TRUST[MAX_LEAD_DAYS])
        acc = self.stats.get(lead)
        if not acc or acc[2] <= 0 or acc[1] <= 0:
            return prior
        measured = max(0.0, min(1.0, 1.0 - acc[0] / acc[1]))
        weight = acc[2] / (acc[2] + _PRIOR_HOURS)
        return round(weight * measured + (1.0 - weight) * prior, 3)

    def mae_ct(self, lead: int) -> float | None:
        acc = self.stats.get(lead)
        if not acc or acc[2] <= 0:
            return None
        return round(acc[0] / acc[2] * 100.0, 2)

    def summary(self) -> dict[str, Any]:
        return {
            str(d): {"vertrouwen": self.trust(d), "fout_ct": self.mae_ct(d)}
            for d in range(1, MAX_LEAD_DAYS + 1)
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshots": self.snapshots,
            "stats": {str(k): v for k, v in self.stats.items()},
            "scored": sorted(self.scored),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ForecastSkill:
        skill = cls()
        if not isinstance(data, dict):
            return skill
        snaps = data.get("snapshots")
        if isinstance(snaps, dict):
            skill.snapshots = {str(k): dict(v) for k, v in snaps.items() if isinstance(v, dict)}
        stats = data.get("stats")
        if isinstance(stats, dict):
            for k, v in stats.items():
                try:
                    skill.stats[int(k)] = [float(x) for x in v][:3]
                except (TypeError, ValueError):
                    continue
        scored = data.get("scored")
        if isinstance(scored, list):
            skill.scored = {str(s) for s in scored}
        return skill


@dataclass(frozen=True)
class ForecastBlock:
    """Eén uur voorspelde prijs, klaar voor de planner."""

    start: datetime
    end: datetime
    buy: float  # gedempt met het vertrouwen
    buy_ex_tax: float
    buy_raw: float  # ongedempt, voor weergave
    lead: int
    trust: float


def hourly_buy(points: list[ForecastPoint], tariff: Tariff, tz: Any) -> dict[datetime, tuple[float, float]]:
    """Gemiddelde voorspelde (buy, ex) per heel uur in lokale tijd."""
    buckets: dict[datetime, list[tuple[float, float]]] = {}
    for p in points:
        hour = p.start.astimezone(tz).replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append((tariff.buy(p.raw), tariff.ex(p.raw)))
    return {
        h: (sum(b for b, _ in v) / len(v), sum(e for _, e in v) / len(v)) for h, v in sorted(buckets.items())
    }


def forecast_blocks(
    points: list[ForecastPoint],
    tariff: Tariff,
    skill: ForecastSkill,
    known_until: datetime,
    now: datetime,
    reference_price: float,
    max_days: int = MAX_LEAD_DAYS,
) -> list[ForecastBlock]:
    """Uurblokken na de bekende prijzen, gedempt naar `reference_price` met het vertrouwen."""
    tz = now.tzinfo
    limit = now + timedelta(days=max_days)
    blocks: list[ForecastBlock] = []
    for hour, (buy, _ex) in hourly_buy(points, tariff, tz).items():
        end = hour + timedelta(hours=1)
        if hour < known_until or end > limit:
            continue
        lead = lead_days(hour, now)
        trust = skill.trust(lead)
        damped = reference_price + trust * (buy - reference_price)
        damped_ex = tariff.ex((damped - tariff.b_buy) / tariff.a_buy)
        blocks.append(
            ForecastBlock(
                start=hour, end=end, buy=damped, buy_ex_tax=damped_ex, buy_raw=buy, lead=lead, trust=trust
            )
        )
    # Alleen een aaneengesloten reeks direct na de bekende prijzen gebruiken.
    contiguous: list[ForecastBlock] = []
    expected = known_until
    for block in blocks:
        if block.start > expected + timedelta(minutes=1):
            if not contiguous and block.start - expected <= timedelta(hours=1):
                pass  # een half uur gat aan het begin (kwartier-uitlijning) mag
            else:
                break
        contiguous.append(block)
        expected = block.end
    return contiguous
