"""Zelflerend warmtemodel: COP, huis en boilervat, plus voorstellen.

Alleen meekijken: deze module stuurt niets. Ze verzamelt per uur wat de warmtepomp,
het huis en het boilervat deden en leert daaruit:

- COP per buitentemperatuur, apart voor verwarming en warm water. Warmte komt uit
  ebusd (CurrentYieldPower), stroom van de eigen kWh-meter. De automatiseringen
  gebruiken nu vaste formules (vloerbuffer 3,2 + 0,1 x T, warm water 2,3 + 0,05 x T).
- Hoe het huis reageert: per uur de stijging van de woonkamer tegen de geleverde
  warmte in dat uur en de twee uren ervoor (de dekvloer warmt na), de zon en het
  verschil binnen-buiten. Daaruit volgen het warmteverlies (W per graad), hoeveel
  warmte een graad kost en hoeveel er na een laadbeurt nog naloopt. Uren met de
  houtkachel aan tellen niet mee.
- Boven (werkkamer en slaapkamer, radiatoren op hetzelfde circuit): hoe snel die
  kamers afkoelen, hoeveel ze opwarmen per kWh warmte en wat de houtkachel beneden
  er boven aan doet.
- Het boilervat: afkoeling per uur buiten laadbeurten en tappen, kWh stroom per graad
  opwarmen, en op welke uren er warm water getapt wordt.
- Ontdooien: per klasse buitentemperatuur en luchtvochtigheid hoeveel minuten per
  draaiuur de warmtepomp ontdooit en hoeveel COP dat kost, vergeleken met uren zonder
  ontdooien. De automatiseringen rekenen nu met een vaste straf (vloerbuffer 25%,
  warm water 10%).

Uit het geleerde maakt voorstellen() concrete voorstellen ten opzichte van wat nu
ingesteld is. Die voert niemand automatisch uit; Marco beslist.

Puur Python (geen Home Assistant-imports), zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# Zoveel dagen uurmetingen bewaren en gebruiken.
KEEP_DAYS = 45
# Een uur telt pas mee met zoveel seconden metingen erin.
MIN_HOUR_SECONDS = 3000
# Houtkachel of zon: VRC-ruimtevoeler (vlak bij de kachel) zoveel warmer dan de keuken.
STOVE_DIFF = 1.0
# Een daling van het vat binnen een uur van meer dan dit (en geen laadbeurt) is tappen.
TAP_DROP = 1.5
# Tot deze daling binnen een uur is het gewoon afkoeling.
QUIET_DROP = 0.8
# Temperatuur van het cv-hok voor de afkoeling van het vat (gemeten 22-23 °C).
TANK_ROOM_TEMP = 21.0
# Vat 192 l x 1,163 Wh per liter per graad.
TANK_KWH_PER_K = 0.223

# Wat de automatiseringen in Home Assistant nu gebruiken (om voorstellen tegen af te zetten).
CURRENT_FLOOR_COP = (3.2, 0.1)  # vloerbuffer: COP = 3,2 + 0,1 x T
CURRENT_DHW_COP = (2.3, 0.05)  # warm water: COP = 2,3 + 0,05 x T
CURRENT_TANK_LOSS = 0.35  # °C per uur, marge nachtbijwarmen
CURRENT_DEFROST_FLOOR = 0.25  # COP-straf vloer vooruit laden bij ontdooiweer
CURRENT_DEFROST_DHW = 0.10  # input_number.warm_water_ontdooicorrectie

# Ontdooiweer zoals de automatiseringen het herkennen: -2 tot 5 °C en vochtig.
DEFROST_T_MIN = -2.0
DEFROST_T_MAX = 5.0
DEFROST_HUMID = 90.0
# Klassen buitentemperatuur voor het ontdooioverzicht (ondergrens, bovengrens).
DEFROST_BINS = ((-30.0, -2.0), (-2.0, 1.0), (1.0, 3.0), (3.0, 5.0), (5.0, 8.0), (8.0, 40.0))
DEFROST_MIN_RUN_HOURS = 3.0  # draaiuren per klasse voor een uitspraak
DEFROST_MIN_PENALTY_HOURS = 10  # uren ontdooiweer voor een voorstel

# Minimale hoeveelheid gegevens voor een uitspraak.
COP_MIN_HOURS = 12
COP_MIN_KWH = 8.0
ROOM_MIN_ROWS = 96
TANK_MIN_HOURS = 24
LADEN_MIN_HOURS = 6


@dataclass
class HourSample:
    """Wat er in één uur gebeurde."""

    start: datetime
    seconds: float = 0.0
    t_out: float | None = None
    room_start: float | None = None
    room_end: float | None = None
    heat_elec_kwh: float = 0.0
    dhw_elec_kwh: float = 0.0
    heat_yield_kwh: float = 0.0
    dhw_yield_kwh: float = 0.0
    pv_kwh: float = 0.0
    stove: bool = False
    tank_start: float | None = None
    tank_end: float | None = None
    tank_min: float | None = None
    dhw_active: bool = False
    laden_seconds: float = 0.0
    run_seconds: float = 0.0  # compressor aan (verwarmen, warm water of ontdooien)
    deice_seconds: float = 0.0
    humidity: float | None = None  # gemiddeld over het uur
    wind: float | None = None  # km/h, gemiddeld over het uur
    up1_start: float | None = None  # boven kamer 1 (werkkamer)
    up1_end: float | None = None
    up2_start: float | None = None  # boven kamer 2 (slaapkamer)
    up2_end: float | None = None

    def to_list(self) -> list[Any]:
        def r(x: float | None, d: int = 3) -> float | None:
            return None if x is None else round(x, d)

        return [
            self.start.isoformat(),
            round(self.seconds),
            r(self.t_out, 2),
            r(self.room_start, 2),
            r(self.room_end, 2),
            r(self.heat_elec_kwh),
            r(self.dhw_elec_kwh),
            r(self.heat_yield_kwh),
            r(self.dhw_yield_kwh),
            r(self.pv_kwh),
            int(self.stove),
            r(self.tank_start, 2),
            r(self.tank_end, 2),
            r(self.tank_min, 2),
            int(self.dhw_active),
            round(self.laden_seconds),
            round(self.run_seconds),
            round(self.deice_seconds),
            r(self.humidity, 1),
            r(self.wind, 1),
            r(self.up1_start, 2),
            r(self.up1_end, 2),
            r(self.up2_start, 2),
            r(self.up2_end, 2),
        ]

    @classmethod
    def from_list(cls, raw: Any) -> HourSample | None:
        # 16 velden: v0.5.0 (nog zonder ontdooien en weer); 20: v0.5.1 (nog zonder boven).
        if not isinstance(raw, list) or len(raw) not in (16, 20, 24):
            return None
        try:
            start = datetime.fromisoformat(raw[0])
        except (TypeError, ValueError):
            return None

        def f(x: Any) -> float | None:
            try:
                return None if x is None else float(x)
            except (TypeError, ValueError):
                return None

        return cls(
            start=start,
            seconds=f(raw[1]) or 0.0,
            t_out=f(raw[2]),
            room_start=f(raw[3]),
            room_end=f(raw[4]),
            heat_elec_kwh=f(raw[5]) or 0.0,
            dhw_elec_kwh=f(raw[6]) or 0.0,
            heat_yield_kwh=f(raw[7]) or 0.0,
            dhw_yield_kwh=f(raw[8]) or 0.0,
            pv_kwh=f(raw[9]) or 0.0,
            stove=bool(raw[10]),
            tank_start=f(raw[11]),
            tank_end=f(raw[12]),
            tank_min=f(raw[13]),
            dhw_active=bool(raw[14]),
            laden_seconds=f(raw[15]) or 0.0,
            run_seconds=(f(raw[16]) or 0.0) if len(raw) >= 20 else 0.0,
            deice_seconds=(f(raw[17]) or 0.0) if len(raw) >= 20 else 0.0,
            humidity=f(raw[18]) if len(raw) >= 20 else None,
            wind=f(raw[19]) if len(raw) >= 20 else None,
            up1_start=f(raw[20]) if len(raw) >= 24 else None,
            up1_end=f(raw[21]) if len(raw) >= 24 else None,
            up2_start=f(raw[22]) if len(raw) >= 24 else None,
            up2_end=f(raw[23]) if len(raw) >= 24 else None,
        )


@dataclass
class Readings:
    """Eén meting van de regellus (None = niet beschikbaar)."""

    t_out: float | None = None
    room: float | None = None
    vrc_room: float | None = None
    kitchen: float | None = None
    heat_w: float | None = None
    dhw_w: float | None = None
    yield_kw: float | None = None
    status: str | None = None
    pv_w: float | None = None
    tank: float | None = None
    laden: bool = False
    humidity: float | None = None
    wind: float | None = None
    up1: float | None = None
    up2: float | None = None


class HourAccumulator:
    """Telt metingen op tot een uur vol is."""

    def __init__(self) -> None:
        self.current: HourSample | None = None
        self._t_out_sum = 0.0
        self._t_out_s = 0.0
        self._hum_sum = self._hum_s = 0.0
        self._wind_sum = self._wind_s = 0.0

    def add(self, now: datetime, dt: float, r: Readings) -> HourSample | None:
        """Meting toevoegen; geeft het afgeronde vorige uur terug zodra een nieuw uur begint."""
        hour = now.replace(minute=0, second=0, microsecond=0)
        finished = None
        if self.current is not None and self.current.start != hour:
            finished = self._finish()
        if self.current is None:
            self.current = HourSample(start=hour, room_start=r.room, tank_start=r.tank, tank_min=r.tank)
            self._t_out_sum = self._t_out_s = 0.0
            self._hum_sum = self._hum_s = 0.0
            self._wind_sum = self._wind_s = 0.0
        s = self.current
        if s.room_start is None:
            s.room_start = r.room
        if s.tank_start is None:
            s.tank_start = r.tank
        if r.room is not None:
            s.room_end = r.room
        if r.up1 is not None:
            if s.up1_start is None:
                s.up1_start = r.up1
            s.up1_end = r.up1
        if r.up2 is not None:
            if s.up2_start is None:
                s.up2_start = r.up2
            s.up2_end = r.up2
        if r.tank is not None:
            s.tank_end = r.tank
            s.tank_min = r.tank if s.tank_min is None else min(s.tank_min, r.tank)
        if r.vrc_room is not None and r.kitchen is not None and r.vrc_room - r.kitchen > STOVE_DIFF:
            s.stove = True
        hwc = bool(r.status and r.status.startswith("hwc"))
        if hwc:
            s.dhw_active = True
        if dt > 0:
            s.seconds += dt
            h = dt / 3600.0
            if r.heat_w is not None and r.heat_w > 0:
                s.heat_elec_kwh += r.heat_w * h / 1000.0
            if r.dhw_w is not None and r.dhw_w > 0:
                s.dhw_elec_kwh += r.dhw_w * h / 1000.0
            # Ontdooien kost warmte (negatieve opbrengst): gewoon meetellen.
            if r.yield_kw is not None:
                if hwc:
                    s.dhw_yield_kwh += r.yield_kw * h
                else:
                    s.heat_yield_kwh += r.yield_kw * h
            if r.pv_w is not None and r.pv_w > 0:
                s.pv_kwh += r.pv_w * h / 1000.0
            if r.t_out is not None:
                self._t_out_sum += r.t_out * dt
                self._t_out_s += dt
            if r.laden:
                s.laden_seconds += dt
            if r.status and ("compressor_active" in r.status or "deicing" in r.status):
                s.run_seconds += dt
            if r.status and "deicing" in r.status:
                s.deice_seconds += dt
            if r.humidity is not None:
                self._hum_sum += r.humidity * dt
                self._hum_s += dt
            if r.wind is not None:
                self._wind_sum += r.wind * dt
                self._wind_s += dt
        return finished

    def _finish(self) -> HourSample | None:
        s = self.current
        self.current = None
        if s is None:
            return None
        if self._t_out_s > 0:
            s.t_out = self._t_out_sum / self._t_out_s
        if self._hum_s > 0:
            s.humidity = self._hum_sum / self._hum_s
        if self._wind_s > 0:
            s.wind = self._wind_sum / self._wind_s
        return s if s.seconds >= MIN_HOUR_SECONDS else None


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gauss-eliminatie met pivotering; None als het stelsel (bijna) singulier is."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-9:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                factor = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _least_squares(rows: list[list[float]], ys: list[float], ridge: float = 1e-6) -> list[float] | None:
    k = len(rows[0])
    ata = [[sum(r[i] * r[j] for r in rows) + (ridge if i == j else 0.0) for j in range(k)] for i in range(k)]
    aty = [sum(r[i] * y for r, y in zip(rows, ys, strict=True)) for i in range(k)]
    return _solve(ata, aty)


@dataclass
class CopFit:
    a: float
    b: float
    hours: int
    kwh: float
    t_min: float
    t_max: float

    def at(self, t: float) -> float:
        return max(1.0, min(7.0, self.a + self.b * t))


def _pure_hour(s: HourSample, mode: str) -> tuple[float, float] | None:
    """(warmte, stroom) van een uur met alleen verwarming of alleen warm water."""
    elec = s.heat_elec_kwh if mode == "verwarming" else s.dhw_elec_kwh
    heat = s.heat_yield_kwh if mode == "verwarming" else s.dhw_yield_kwh
    # Gemengde uren (deels warm water, deels verwarming) zijn onzuiver.
    other = s.dhw_elec_kwh if mode == "verwarming" else s.heat_elec_kwh
    if elec < 0.15 or other > 0.1 * elec:
        return None
    return heat, elec


def fit_cop(samples: list[HourSample], mode: str, without_deice: bool = False) -> CopFit | None:
    """COP = a + b x buitentemperatuur, gewogen naar stroomverbruik per uur.

    without_deice: alleen uren zonder ontdooien (de COP die de warmtepomp haalt als
    hij niet hoeft te ontdooien), om de ontdooistraf tegen af te zetten.
    """
    pts: list[tuple[float, float, float]] = []  # (t, yield, elec)
    for s in samples:
        if s.t_out is None:
            continue
        if without_deice and (s.deice_seconds > 0 or s.run_seconds <= 0):
            continue
        pure = _pure_hour(s, mode)
        if pure is None or pure[0] <= 0:
            continue
        heat, elec = pure
        pts.append((s.t_out, heat, elec))
    total = sum(e for _, _, e in pts)
    if len(pts) < COP_MIN_HOURS or total < COP_MIN_KWH:
        return None
    t_min, t_max = min(p[0] for p in pts), max(p[0] for p in pts)
    mean_cop = sum(h for _, h, _ in pts) / total
    wt = sum(t * e for t, _, e in pts) / total
    sxx = sum(e * (t - wt) ** 2 for t, _, e in pts)
    b = 0.0
    if sxx / total >= 4.0:  # minstens ~2 graden spreiding
        sxy = sum(e * (t - wt) * (h / e - mean_cop) for t, h, e in pts)
        b = max(0.0, min(0.25, sxy / sxx))
    return CopFit(a=mean_cop - b * wt, b=b, hours=len(pts), kwh=total, t_min=t_min, t_max=t_max)


@dataclass
class RoomFit:
    c: tuple[float, float, float]  # graden per kWh warmte, nu / 1 uur / 2 uur geleden
    sun: float  # graden per kWh zon
    loss: float  # fractie van (binnen - buiten) per uur
    base: float  # vaste graden per uur (mensen, apparaten)
    rows: int
    r2: float

    @property
    def kwh_per_degree(self) -> float:
        return 1.0 / sum(self.c)

    @property
    def loss_w_per_k(self) -> float:
        return self.loss * self.kwh_per_degree * 1000.0

    @property
    def afterglow_share(self) -> float:
        return (self.c[1] + self.c[2]) / sum(self.c)

    def afterglow_degrees(self, kwh_per_hour: float) -> float:
        """Hoeveel de kamer na het stoppen nog stijgt door nawarmen van de dekvloer."""
        return (self.c[1] + 2 * self.c[2]) * kwh_per_hour


def fit_room(samples: list[HourSample]) -> RoomFit | None:
    by_start = {s.start: s for s in samples}
    rows: list[list[float]] = []
    ys: list[float] = []
    for s in samples:
        p1 = by_start.get(s.start - timedelta(hours=1))
        p2 = by_start.get(s.start - timedelta(hours=2))
        if p1 is None or p2 is None or s.stove or p1.stove or p2.stove:
            continue
        if s.room_start is None or s.room_end is None or s.t_out is None:
            continue
        heat = [s.heat_yield_kwh, p1.heat_yield_kwh, p2.heat_yield_kwh]
        rows.append([*heat, s.pv_kwh, -(s.room_start - s.t_out), 1.0])
        ys.append(s.room_end - s.room_start)
    if len(rows) < ROOM_MIN_ROWS:
        return None
    coef = _least_squares(rows, ys)
    if coef is None:
        return None
    c0, c1, c2, sun, loss, base = coef
    c1, c2 = max(0.0, c1), max(0.0, c2)
    if c0 + c1 + c2 < 0.005 or loss <= 0:
        return None
    mean_y = sum(ys) / len(ys)
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    predicted = [sum(c * x for c, x in zip(coef, r, strict=True)) for r in rows]
    ss_res = sum((y - p) ** 2 for p, y in zip(predicted, ys, strict=True))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return RoomFit(c=(max(0.0, c0), c1, c2), sun=sun, loss=loss, base=base, rows=len(rows), r2=r2)


@dataclass
class UpstairsFit:
    per_kwh: float  # graden per kWh warmte (dit en vorig uur samen)
    loss: float  # fractie van (kamer - buiten) per uur
    base: float  # vaste graden per uur
    stove: float  # graden per uur extra als de kachel beneden brandt
    rows: int
    r2: float

    def cooling_per_hour(self, diff: float) -> float:
        """Afkoeling per uur zonder verwarming en zonder kachel bij dit verschil binnen-buiten."""
        return self.loss * diff - self.base


def fit_upstairs(samples: list[HourSample], room: int) -> UpstairsFit | None:
    """Kamer boven: stijging per uur tegen warmte (dit + vorig uur), kachel en verschil met buiten."""
    by_start = {s.start: s for s in samples}
    rows: list[list[float]] = []
    ys: list[float] = []
    for s in samples:
        start, end = (s.up1_start, s.up1_end) if room == 1 else (s.up2_start, s.up2_end)
        p1 = by_start.get(s.start - timedelta(hours=1))
        if start is None or end is None or s.t_out is None or p1 is None:
            continue
        rows.append([s.heat_yield_kwh, p1.heat_yield_kwh, float(s.stove), -(start - s.t_out), 1.0])
        ys.append(end - start)
    if len(rows) < ROOM_MIN_ROWS:
        return None
    coef = _least_squares(rows, ys)
    if coef is None:
        return None
    c0, c1, stove, loss, base = coef
    if loss <= 0:
        return None
    mean_y = sum(ys) / len(ys)
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    predicted = [sum(c * x for c, x in zip(coef, r, strict=True)) for r in rows]
    ss_res = sum((y - p) ** 2 for p, y in zip(predicted, ys, strict=True))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return UpstairsFit(
        per_kwh=max(0.0, c0) + max(0.0, c1), loss=loss, base=base, stove=stove, rows=len(rows), r2=r2
    )


@dataclass
class TankFit:
    loss_rate: float | None  # fractie van (vat - hok) per uur
    loss_hours: int
    kwh_per_degree: float | None  # stroom per graad opwarmen
    taps_per_hour: list[float]  # gemiddeld aantal tapbeurten per uur van de dag
    days: float

    def loss_at(self, tank: float) -> float | None:
        return None if self.loss_rate is None else self.loss_rate * (tank - TANK_ROOM_TEMP)


def fit_tank(samples: list[HourSample]) -> TankFit:
    num = den = 0.0
    quiet = 0
    rise = elec = 0.0
    taps = [0] * 24
    for s in samples:
        if s.tank_start is None or s.tank_end is None or s.tank_min is None:
            continue
        drop_peak = s.tank_start - s.tank_min
        if s.dhw_active:
            if s.tank_end > s.tank_start:
                rise += s.tank_end - s.tank_start
                elec += s.dhw_elec_kwh
            continue
        if drop_peak > TAP_DROP:
            taps[s.start.hour] += 1
            continue
        if drop_peak <= QUIET_DROP:
            excess = (s.tank_start + s.tank_end) / 2 - TANK_ROOM_TEMP
            if excess > 5:
                num += (s.tank_start - s.tank_end) * excess
                den += excess * excess
                quiet += 1
    days = len(samples) / 24.0
    rate = num / den if quiet >= TANK_MIN_HOURS and den > 0 and num > 0 else None
    return TankFit(
        loss_rate=rate,
        loss_hours=quiet,
        kwh_per_degree=elec / rise if rise >= 20 else None,
        taps_per_hour=[round(t / days, 2) if days >= 3 else 0.0 for t in taps],
        days=days,
    )


def is_defrost_weather(s: HourSample) -> bool:
    """Ontdooiweer zoals de automatiseringen het herkennen."""
    return (
        s.t_out is not None
        and s.humidity is not None
        and DEFROST_T_MIN <= s.t_out <= DEFROST_T_MAX
        and s.humidity >= DEFROST_HUMID
    )


@dataclass
class DefrostClass:
    t_low: float
    t_high: float
    humid: bool
    run_hours: float
    minutes_per_run_hour: float

    @property
    def label(self) -> str:
        if self.t_low <= -30:
            temp = f"onder {_fmt(self.t_high, 0)} °C"
        elif self.t_high >= 40:
            temp = f"vanaf {_fmt(self.t_low, 0)} °C"
        else:
            temp = f"{_fmt(self.t_low, 0)} tot {_fmt(self.t_high, 0)} °C"
        return f"{temp}, {'vochtig' if self.humid else 'droog'}"


def defrost_classes(samples: list[HourSample]) -> list[DefrostClass]:
    """Minuten ontdooien per draaiuur, per klasse buitentemperatuur en vochtigheid."""
    result: list[DefrostClass] = []
    for lo, hi in DEFROST_BINS:
        for humid in (True, False):
            run = deice = 0.0
            for s in samples:
                if s.t_out is None or s.humidity is None or s.run_seconds <= 0:
                    continue
                if not lo <= s.t_out < hi or (s.humidity >= DEFROST_HUMID) != humid:
                    continue
                run += s.run_seconds
                deice += s.deice_seconds
            run_hours = run / 3600.0
            if run_hours >= DEFROST_MIN_RUN_HOURS:
                result.append(DefrostClass(lo, hi, humid, run_hours, deice / run_hours / 60.0))
    return result


@dataclass
class DefrostPenalty:
    penalty: float  # fractie COP die ontdooiweer kost
    hours: int


def defrost_penalty(samples: list[HourSample], mode: str, clean: CopFit | None) -> DefrostPenalty | None:
    """COP-verlies bij ontdooiweer ten opzichte van de COP zonder ontdooien."""
    if clean is None:
        return None
    actual = expected = 0.0
    hours = 0
    for s in samples:
        if s.run_seconds <= 0 or not is_defrost_weather(s):
            continue
        pure = _pure_hour(s, mode)
        if pure is None:
            continue
        heat, elec = pure
        actual += heat
        expected += elec * clean.at(s.t_out or 0.0)
        hours += 1
    if hours == 0 or expected <= 0:
        return None
    return DefrostPenalty(penalty=max(0.0, min(0.8, 1.0 - actual / expected)), hours=hours)


def _defrost_proposal(
    found: DefrostPenalty | None, current: float, key: str, name: str, where: str
) -> dict[str, Any] | None:
    if found is None or found.hours < DEFROST_MIN_PENALTY_HOURS:
        return None
    if abs(found.penalty - current) < 0.08:
        return None
    return {
        "id": key,
        "onderwerp": f"Ontdooistraf {name}",
        "nu": f"{round(current * 100)}%",
        "voorstel": f"{round(found.penalty * 100)}%",
        "reden": (
            f"Gemeten over {found.hours} uur met {_fmt(DEFROST_T_MIN, 0)} tot {_fmt(DEFROST_T_MAX, 0)} °C "
            f"en luchtvochtigheid vanaf {round(DEFROST_HUMID)}%: de COP lag "
            f"{round(found.penalty * 100)}% onder die van uren zonder ontdooien. {where}"
        ),
        "zekerheid": _confidence(found.hours, 20, 60),
    }


def _confidence(amount: float, low: float, high: float) -> str:
    return "hoog" if amount >= high else ("middel" if amount >= low else "laag")


def _fmt(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _cop_proposal(
    fit: CopFit | None, current: tuple[float, float], key: str, name: str
) -> dict[str, Any] | None:
    if fit is None:
        return None
    lo, hi = fit.t_min, max(fit.t_max, fit.t_min + 1)
    temps = [lo + (hi - lo) * i / 4 for i in range(5)]
    a0, b0 = current
    diffs = [abs(fit.at(t) - (a0 + b0 * t)) / (a0 + b0 * t) for t in temps]
    if sum(diffs) / len(diffs) < 0.10:
        return None
    return {
        "id": key,
        "onderwerp": f"COP-formule {name}",
        "nu": f"{_fmt(a0, 1)} + {_fmt(b0)} x T",
        "voorstel": f"{_fmt(fit.a, 1)} + {_fmt(fit.b)} x T",
        "reden": (
            f"Gemeten tussen {_fmt(lo, 0)} en {_fmt(fit.t_max, 0)} °C over {fit.hours} uur: "
            f"COP {_fmt(fit.at(lo), 1)} bij {_fmt(lo, 0)} °C en {_fmt(fit.at(hi), 1)} bij {_fmt(hi, 0)} °C, "
            f"de formule zegt {_fmt(a0 + b0 * lo, 1)} en {_fmt(a0 + b0 * hi, 1)}."
        ),
        "zekerheid": _confidence(fit.kwh, 20, 60),
    }


def voorstellen(
    cop_heat: CopFit | None,
    cop_dhw: CopFit | None,
    room: RoomFit | None,
    tank: TankFit,
    laden_kwh_per_hour: float | None,
    laden_hours: int,
    settings: dict[str, float | None],
    defrost_heat: DefrostPenalty | None = None,
    defrost_dhw: DefrostPenalty | None = None,
) -> list[dict[str, Any]]:
    """Voorstellen ten opzichte van wat nu ingesteld is (settings uit Home Assistant)."""
    result: list[dict[str, Any]] = []
    current_dhw_defrost = settings.get("ontdooicorrectie_ww")
    for p in (
        _cop_proposal(cop_heat, CURRENT_FLOOR_COP, "cop_vloerbuffer", "vloerbuffer"),
        _cop_proposal(cop_dhw, CURRENT_DHW_COP, "cop_warm_water", "warm water"),
        _defrost_proposal(
            defrost_heat,
            CURRENT_DEFROST_FLOOR,
            "ontdooistraf_vloer",
            "vloer vooruit laden",
            "Dit bepaalt of de vloer de avond ervoor al geladen wordt.",
        ),
        _defrost_proposal(
            defrost_dhw,
            CURRENT_DEFROST_DHW if current_dhw_defrost is None else current_dhw_defrost / 100.0,
            "ontdooistraf_warm_water",
            "warm water",
            "Dit is input_number.warm_water_ontdooicorrectie (middag of nacht laden).",
        ),
    ):
        if p:
            result.append(p)

    loss50 = tank.loss_at(50.0)
    if loss50 is not None and abs(loss50 - CURRENT_TANK_LOSS) / CURRENT_TANK_LOSS >= 0.25:
        result.append(
            {
                "id": "vat_afkoeling",
                "onderwerp": "Afkoeling vat in marge nachtbijwarmen",
                "nu": f"{_fmt(CURRENT_TANK_LOSS)} °C per uur",
                "voorstel": f"{_fmt(loss50)} °C per uur",
                "reden": (
                    f"Gemeten over {tank.loss_hours} rustige uren (geen laden of tappen), "
                    "omgerekend naar een vat van 50 °C."
                ),
                "zekerheid": _confidence(tank.loss_hours, 48, 150),
            }
        )

    voorsprong = settings.get("voorsprong")
    if room is not None and laden_kwh_per_hour and laden_hours >= LADEN_MIN_HOURS and voorsprong is not None:
        est = max(0.2, min(1.0, round(room.afterglow_degrees(laden_kwh_per_hour), 1)))
        if abs(est - voorsprong) >= 0.2:
            result.append(
                {
                    "id": "voorsprong_naverwarming",
                    "onderwerp": "Voorsprong naverwarming vloerbuffer",
                    "nu": f"{_fmt(voorsprong, 1)} °C",
                    "voorstel": f"{_fmt(est, 1)} °C",
                    "reden": (
                        "Bij laden levert de warmtepomp gemiddeld "
                        f"{_fmt(laden_kwh_per_hour, 1)} kWh warmte per uur; "
                        f"{round(room.afterglow_share * 100)}% van het effect op de kamer "
                        "komt pas 1 tot 2 uur later. "
                        f"Na het stoppen loopt de kamer daardoor nog ongeveer {_fmt(est, 1)} °C op."
                    ),
                    "zekerheid": "laag" if room.r2 < 0.3 else _confidence(laden_hours, 12, 40),
                }
            )
    return result


@dataclass
class HeatLearner:
    """Bewaart de uurmetingen en rekent alles uit."""

    samples: list[HourSample] = field(default_factory=list)
    acc: HourAccumulator = field(default_factory=HourAccumulator)

    def add(self, now: datetime, dt: float, readings: Readings) -> bool:
        """Meting verwerken; True als er een uur bij kwam (dan opslaan)."""
        done = self.acc.add(now, dt, readings)
        if done is None:
            return False
        self.samples = [s for s in self.samples if s.start != done.start]
        self.samples.append(done)
        cutoff = done.start - timedelta(days=KEEP_DAYS)
        self.samples = [s for s in self.samples if s.start > cutoff]
        return True

    def summary(self, settings: dict[str, float | None]) -> dict[str, Any]:
        cop_heat = fit_cop(self.samples, "verwarming")
        cop_dhw = fit_cop(self.samples, "warm_water")
        room = fit_room(self.samples)
        tank = fit_tank(self.samples)
        pen_heat = defrost_penalty(
            self.samples, "verwarming", fit_cop(self.samples, "verwarming", without_deice=True)
        )
        pen_dhw = defrost_penalty(
            self.samples, "warm_water", fit_cop(self.samples, "warm_water", without_deice=True)
        )
        laden = [s for s in self.samples if s.laden_seconds >= 1800 and not s.stove]
        laden_kwh = sum(s.heat_yield_kwh for s in laden) / len(laden) if laden else None

        info: dict[str, Any] = {
            "uren_gemeten": len(self.samples),
            "sinds": self.samples[0].start.isoformat() if self.samples else None,
        }
        for key, fit in (("cop_verwarming", cop_heat), ("cop_warm_water", cop_dhw)):
            if fit is None:
                info[key] = "nog aan het leren"
            else:
                info[key] = {
                    "formule": f"{_fmt(fit.a)} + {_fmt(fit.b, 3)} x T",
                    "bij_0_graden": round(fit.at(0), 2),
                    "bij_5_graden": round(fit.at(5), 2),
                    "bij_10_graden": round(fit.at(10), 2),
                    "gemeten_tussen": [round(fit.t_min, 1), round(fit.t_max, 1)],
                    "uren": fit.hours,
                    "kwh_stroom": round(fit.kwh, 1),
                }
        if room is None:
            info["huis"] = "nog aan het leren"
        else:
            info["huis"] = {
                "warmteverlies_w_per_graad": round(room.loss_w_per_k),
                "kwh_warmte_per_graad_kamer": round(room.kwh_per_degree, 1),
                "nawarming_aandeel": round(room.afterglow_share, 2),
                "afkoeling_per_uur_bij_15_graden_verschil": round(room.loss * 15 - room.base, 2),
                "zon_graden_per_kwh": round(room.sun, 3),
                "verklaarde_variatie": round(room.r2, 2),
                "uren": room.rows,
            }
        for key, idx in (("boven_werkkamer", 1), ("boven_slaapkamer", 2)):
            up = fit_upstairs(self.samples, idx)
            if up is None:
                info[key] = "nog aan het leren"
            else:
                info[key] = {
                    "afkoeling_per_uur_bij_15_graden_verschil": round(up.cooling_per_hour(15.0), 2),
                    "graden_per_kwh_warmte": round(up.per_kwh, 3),
                    "kachel_graden_per_uur": round(up.stove, 2),
                    "verklaarde_variatie": round(up.r2, 2),
                    "uren": up.rows,
                }
        info["vat"] = {
            "afkoeling_per_uur_bij_50": None if tank.loss_at(50) is None else round(tank.loss_at(50), 2),
            "verlies_w_bij_50": None
            if tank.loss_at(50) is None
            else round(tank.loss_at(50) * TANK_KWH_PER_K * 1000),
            "rustige_uren": tank.loss_hours,
            "kwh_stroom_per_graad": None if tank.kwh_per_degree is None else round(tank.kwh_per_degree, 3),
            "taps_per_uur_van_de_dag": tank.taps_per_hour,
        }
        info["laden"] = {
            "uren": len(laden),
            "kwh_warmte_per_uur": None if laden_kwh is None else round(laden_kwh, 2),
        }
        risk = [s for s in self.samples if s.run_seconds > 0 and is_defrost_weather(s)]
        risk_run = sum(s.run_seconds for s in risk)
        risk_deice = sum(s.deice_seconds for s in risk)
        enough = risk_run >= DEFROST_MIN_RUN_HOURS * 3600
        info["ontdooien"] = {
            "uren_ontdooiweer": len(risk),
            "minuten_per_draaiuur_bij_ontdooiweer": round(risk_deice / risk_run * 60, 1) if enough else None,
            "cop_straf_verwarming": None if pen_heat is None else round(pen_heat.penalty, 2),
            "cop_straf_warm_water": None if pen_dhw is None else round(pen_dhw.penalty, 2),
            "per_klasse": {
                c.label: {
                    "minuten_per_draaiuur": round(c.minutes_per_run_hour, 1),
                    "draaiuren": round(c.run_hours, 1),
                }
                for c in defrost_classes(self.samples)
            },
        }
        info["voorstellen"] = voorstellen(
            cop_heat, cop_dhw, room, tank, laden_kwh, len(laden), settings, pen_heat, pen_dhw
        )
        return info

    def to_dict(self) -> dict[str, Any]:
        return {"samples": [s.to_list() for s in self.samples]}

    @classmethod
    def from_dict(cls, raw: Any) -> HeatLearner:
        learner = cls()
        if isinstance(raw, dict) and isinstance(raw.get("samples"), list):
            for item in raw["samples"]:
                s = HourSample.from_list(item)
                if s is not None:
                    learner.samples.append(s)
        return learner
