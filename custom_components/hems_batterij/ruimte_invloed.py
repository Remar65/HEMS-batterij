"""Zelflerend advies voor de ruimte-invloed van de VRC 720: Actief of Uitgebreid.

Wens Marco (21-11-2026): HEMS moet uitzoeken bij welk weer (buitentemperatuur, wind en
zon) de kamerbijschakeling beter op Actief of op Uitgebreid staat, en dat uiteindelijk
zelf instellen.

- Uitgebreid: de warmtevraag gaat uit zodra de kamer net boven het setpoint komt
  (+2/16 K). In koude nachten koelt de dekvloer dan af en haalt de WP de 20 °C daarna
  niet meer (gezien 20/21-11: VRC bleef op 19,6 na een kachelavond).
- Actief: de WP blijft draaien en past alleen de aanvoer aan. Bij zacht weer kan dat
  doorschieten, omdat hij niet uitschakelt.

Het leren gebeurt per blok (nacht 22-08 uur, dag 08-22 uur) op de kwartierregels van de
meetlogger:

- Uitgebreid en de VRC minstens 2 uur onder setpoint - 0,3: hier was Actief beter.
  Ook vlak na een stooksessie, want juist dan slaat Uitgebreid de WP uit.
- Actief en minstens 2 uur boven setpoint + 0,5, terwijl ook de keuken boven het setpoint
  zit: hier was Uitgebreid beter. Kwartieren met de houtkachel aan (plus 3 uur naloop) of
  met veel zon tellen dan niet mee: dat warmte-overschot komt niet door Actief.
- Uitgebreid zonder problemen: een zwakke aanwijzing dat Uitgebreid daar volstaat (die
  stand verbruikt minder).

Daaruit zoekt het een drempel op een "gevoelstemperatuur" voor het huis: buiten min een
windterm plus een zonterm. De wind- en zonfactor kiest het zelf uit een klein rooster,
zodat die combinatie volgt uit de metingen. Tot er genoeg blokken zijn, geldt Marco's
vuistregel: onder 8 °C Actief.

Puur Python (geen Home Assistant-imports), zodat dit los te testen is.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

ACTIVE = "active"
EXPANDED = "expanded"
INACTIVE = "inactive"
SETTINGS = (INACTIVE, ACTIVE, EXPANDED)

PRIOR_THRESHOLD = 8.0  # vuistregel Marco: onder ~8 °C buiten Actief
HYSTERESIS = 0.5  # pas wisselen als het weer de drempel zoveel voorbij is
COLD_MARGIN = 0.3  # VRC zoveel onder setpoint = te koud
WARM_MARGIN = 0.5  # VRC zoveel boven setpoint = te warm (en keuken boven setpoint)
MIN_BAD_QUARTERS = 8  # 2 uur
STOVE_ABS = 32.0  # kachelsensor boven deze temperatuur = stoken
STOVE_RISE = 6.0  # of zoveel boven het minimum van de 6 uur ervoor
STOVE_AFTER = timedelta(hours=3)
VRC_KITCHEN_STOVE = 1.0  # zonder kachelsensor: VRC zoveel warmer dan keuken (en weinig zon)
SUN_W = 500.0  # PV boven dit vermogen = zon in huis
MIN_COVERAGE = 0.75  # deel van de kwartieren in een blok dat er moet zijn
MIN_SAME_SETTING = 0.8  # deel van het blok met dezelfde stand
WEAK_WEIGHT = 0.3  # Uitgebreid zonder problemen
WIND_FACTORS = (0.0, 0.05, 0.1, 0.2)  # °C per km/h boven 10 km/h
SUN_FACTORS = (0.0, 1.0, 2.0, 3.0)  # °C per kW gemiddeld PV-vermogen
WIND_FREE_KMH = 10.0
KEEP_BLOCKS = 12  # zoveel recente blokken meegeven als onderbouwing


@dataclass(frozen=True)
class Quarter:
    t: datetime
    setting: str | None
    setpoint: float | None
    vrc: float | None
    kitchen: float | None
    t_out: float | None
    wind: float | None
    pv_w: float | None
    stove_c: float | None


@dataclass(frozen=True)
class Block:
    start: datetime
    part: str  # "nacht" of "dag"
    setting: str
    setpoint: float
    t_out: float
    wind: float
    pv_kw: float
    stove_share: float
    cold_q: int
    warm_q: int
    label: str  # actief_beter / uitgebreid_beter / uitgebreid_ok / actief_ok / actief_te_koud

    def t_eff(self, wind_factor: float, sun_factor: float) -> float:
        return feels_like(self.t_out, self.wind, self.pv_kw, wind_factor, sun_factor)


def feels_like(t_out: float, wind: float, pv_kw: float, wind_factor: float, sun_factor: float) -> float:
    return t_out - wind_factor * max(0.0, wind - WIND_FREE_KMH) + sun_factor * pv_kw


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def quarters_from_rows(columns: Sequence[str], rows: Iterable[Sequence[str]]) -> list[Quarter]:
    """Kwartierregels van de meetlogger (zoals read_rows ze geeft) omzetten."""
    idx = {name: i for i, name in enumerate(columns)}

    def get(row: Sequence[str], name: str) -> str | None:
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    out: list[Quarter] = []
    for row in rows:
        try:
            t = datetime.fromisoformat(get(row, "tijd") or "")
        except ValueError:
            continue
        aiko, jinko = _num(get(row, "pv_aiko_w")), _num(get(row, "pv_jinko_w"))
        pv = None if aiko is None and jinko is None else abs(aiko or 0.0) + abs(jinko or 0.0)
        setting = get(row, "ruimte_invloed") or None
        out.append(
            Quarter(
                t=t,
                setting=setting if setting in SETTINGS else None,
                setpoint=_num(get(row, "vrc_setpoint_c")),
                vrc=_num(get(row, "vrc_kamer_c")),
                kitchen=_num(get(row, "keuken_c")),
                t_out=_num(get(row, "buiten_gem_c")),
                wind=_num(get(row, "wind_kmh")),
                pv_w=pv,
                stove_c=_num(get(row, "kachel_c")),
            )
        )
    out.sort(key=lambda q: q.t)
    return out


def stove_flags(quarters: Sequence[Quarter]) -> list[bool]:
    """Per kwartier: brandt de houtkachel? Met kachelsensor, anders VRC tegen keuken."""
    flags: list[bool] = []
    for i, q in enumerate(quarters):
        if q.stove_c is not None:
            window = [
                p.stove_c
                for p in quarters[max(0, i - 24) : i]
                if p.stove_c is not None and q.t - p.t <= timedelta(hours=6)
            ]
            base = min(window) if window else q.stove_c
            flags.append(q.stove_c >= STOVE_ABS or q.stove_c - base >= STOVE_RISE)
        elif q.vrc is not None and q.kitchen is not None:
            sunny = q.pv_w is not None and q.pv_w >= SUN_W
            flags.append(q.vrc - q.kitchen > VRC_KITCHEN_STOVE and not sunny)
        else:
            flags.append(False)
    return flags


def _block_key(t: datetime) -> tuple[datetime, str]:
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if t.hour >= 22:
        return day.replace(hour=22), "nacht"
    if t.hour < 8:
        return (day - timedelta(days=1)).replace(hour=22), "nacht"
    return day.replace(hour=8), "dag"


def make_blocks(quarters: Sequence[Quarter], now: datetime | None = None) -> list[Block]:
    """Afgeronde nacht- en dagblokken beoordelen."""
    flags = stove_flags(quarters)
    stove_until: datetime | None = None
    after: list[bool] = []
    for q, stove in zip(quarters, flags, strict=True):
        if stove:
            stove_until = q.t + STOVE_AFTER
        after.append(not stove and stove_until is not None and q.t <= stove_until)

    groups: dict[tuple[datetime, str], list[int]] = {}
    for i, q in enumerate(quarters):
        groups.setdefault(_block_key(q.t), []).append(i)

    blocks: list[Block] = []
    for (start, part), ids in sorted(groups.items()):
        hours = 10 if part == "nacht" else 14
        if now is not None and start + timedelta(hours=hours) > now:
            continue  # blok nog niet af
        if len(ids) < MIN_COVERAGE * hours * 4:
            continue
        qs = [quarters[i] for i in ids]
        settings = [q.setting for q in qs if q.setting]
        if not settings:
            continue
        setting = max(set(settings), key=settings.count)
        if settings.count(setting) < MIN_SAME_SETTING * len(qs) or setting == INACTIVE:
            continue
        sps = [q.setpoint for q in qs if q.setpoint is not None]
        temps = [q.t_out for q in qs if q.t_out is not None]
        if not sps or not temps:
            continue
        sp = median(sps)
        cold_q = sum(
            1
            for i in ids
            if not flags[i] and quarters[i].vrc is not None and quarters[i].vrc < sp - COLD_MARGIN
        )
        warm_q = 0
        for i in ids:
            q = quarters[i]
            if flags[i] or after[i] or (q.pv_w is not None and q.pv_w >= SUN_W):
                continue
            if q.vrc is not None and q.kitchen is not None and q.vrc > sp + WARM_MARGIN and q.kitchen > sp:
                warm_q += 1
        if setting == EXPANDED:
            label = "actief_beter" if cold_q >= MIN_BAD_QUARTERS else "uitgebreid_ok"
        elif warm_q >= MIN_BAD_QUARTERS:
            label = "uitgebreid_beter"
        elif cold_q >= MIN_BAD_QUARTERS:
            label = "actief_te_koud"  # ook op Actief te koud: dan is de stooklijn aan de beurt
        else:
            label = "actief_ok"
        winds = [q.wind for q in qs if q.wind is not None]
        pvs = [q.pv_w for q in qs if q.pv_w is not None]
        blocks.append(
            Block(
                start=start,
                part=part,
                setting=setting,
                setpoint=sp,
                t_out=sum(temps) / len(temps),
                wind=sum(winds) / len(winds) if winds else 0.0,
                pv_kw=(sum(pvs) / len(pvs) / 1000.0) if pvs else 0.0,
                stove_share=sum(flags[i] for i in ids) / len(ids),
                cold_q=cold_q,
                warm_q=warm_q,
                label=label,
            )
        )
    return blocks


@dataclass(frozen=True)
class Fit:
    threshold: float
    wind_factor: float
    sun_factor: float
    reliable: bool
    n_active_better: int
    n_expanded_better: int
    n_expanded_ok: int
    errors: float  # gewogen aantal blokken aan de verkeerde kant


def fit(blocks: Sequence[Block]) -> Fit:
    """Drempel en wind-/zonfactor die de blokken het best scheiden."""
    need_a = [b for b in blocks if b.label == "actief_beter"]
    need_u = [b for b in blocks if b.label == "uitgebreid_beter"]
    ok_u = [b for b in blocks if b.label == "uitgebreid_ok"]
    reliable = len(need_a) >= 2 and (len(need_u) >= 2 or len(ok_u) >= 4)
    if not reliable:
        return Fit(PRIOR_THRESHOLD, 0.0, 0.0, False, len(need_a), len(need_u), len(ok_u), 0.0)

    candidates = [x / 2 for x in range(-10, 41)]  # -5 tot 20 °C
    best: tuple[float, float, float, float, float] | None = None  # errors, |T-prior|, w, s, T
    for w in WIND_FACTORS:
        for s in SUN_FACTORS:
            pts = (
                [(b.t_eff(w, s), "A", 1.0) for b in need_a]
                + [(b.t_eff(w, s), "U", 1.0) for b in need_u]
                + [(b.t_eff(w, s), "U", WEAK_WEIGHT) for b in ok_u]
            )
            for thr in candidates:
                errors = sum(wt for t, kind, wt in pts if (kind == "A") != (t < thr))
                key = (round(errors, 6), abs(thr - PRIOR_THRESHOLD), w, s, thr)
                if best is None or key < best:
                    best = key
    assert best is not None
    errors, _, w, s, thr = best
    return Fit(thr, w, s, True, len(need_a), len(need_u), len(ok_u), errors)


def advise(
    current: str | None, fitted: Fit, t_out: float, wind: float, pv_kw: float = 0.0
) -> tuple[str, float]:
    """Advies voor het komende weer, met hysterese rond de drempel. Geeft (stand, gevoelstemp)."""
    t_eff = feels_like(t_out, wind, pv_kw, fitted.wind_factor, fitted.sun_factor)
    if current == ACTIVE:
        return (EXPANDED if t_eff > fitted.threshold + HYSTERESIS else ACTIVE), t_eff
    if current == EXPANDED:
        return (ACTIVE if t_eff < fitted.threshold - HYSTERESIS else EXPANDED), t_eff
    return (ACTIVE if t_eff < fitted.threshold else EXPANDED), t_eff


NAMES = {ACTIVE: "actief", EXPANDED: "uitgebreid", INACTIVE: "inactief"}


def summary(
    blocks: Sequence[Block],
    fitted: Fit,
    current: str | None,
    advice: str | None,
    t_eff: float | None,
    forecast: dict[str, float] | None,
) -> dict[str, Any]:
    """Attributen voor de sensor, in Marco's woorden."""
    if fitted.reliable:
        basis = (
            f"geleerd uit {fitted.n_active_better} blokken waar Actief beter was, "
            f"{fitted.n_expanded_better} waar Uitgebreid beter was en "
            f"{fitted.n_expanded_ok} waar Uitgebreid volstond"
        )
    else:
        basis = (
            f"nog te weinig blokken ({fitted.n_active_better} Actief beter, "
            f"{fitted.n_expanded_better} Uitgebreid beter, {fitted.n_expanded_ok} Uitgebreid ok); "
            f"vuistregel {PRIOR_THRESHOLD:g} °C"
        )
    recent = [
        {
            "blok": f"{b.start:%d-%m} {b.part}",
            "stand": NAMES.get(b.setting, b.setting),
            "buiten_c": round(b.t_out, 1),
            "wind_kmh": round(b.wind),
            "zon_kw": round(b.pv_kw, 2),
            "kachel_pct": round(100 * b.stove_share),
            "uur_te_koud": b.cold_q / 4,
            "uur_te_warm": b.warm_q / 4,
            "oordeel": b.label,
        }
        for b in list(blocks)[-KEEP_BLOCKS:]
    ]
    return {
        "huidige_stand": NAMES.get(current or "", current),
        "advies": NAMES.get(advice or "", advice),
        "drempel_c": fitted.threshold,
        "windfactor": fitted.wind_factor,
        "zonfactor": fitted.sun_factor,
        "betrouwbaar": fitted.reliable,
        "gevoelstemperatuur_komend_c": round(t_eff, 1) if t_eff is not None else None,
        "verwachting": forecast,
        "basis": basis,
        "blokken_te_koud_op_actief": sum(1 for b in blocks if b.label == "actief_te_koud"),
        "recente_blokken": recent,
    }
