"""Prijsdata inlezen.

Puur Python (geen Home Assistant-imports), zodat dit los te testen is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Zonneplan levert bedragen als gehele getallen in 1/10.000.000 euro per kWh.
ZONNEPLAN_AMOUNT_DIVISOR = 10_000_000


def _as_datetime(value: Any) -> datetime:
    """Na een herstart geeft Zonneplan datetime-objecten in plaats van tekst."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


@dataclass(frozen=True)
class PriceSlot:
    """Eén prijsblok (meestal een kwartier)."""

    start: datetime
    end: datetime
    buy: float  # €/kWh inclusief belastingen (wat je betaalt bij afname)
    buy_ex_tax: float  # €/kWh kale prijs (basis voor terugleververgoeding na saldering)


def parse_zonneplan_forecast(attributes: dict[str, Any] | None) -> list[PriceSlot]:
    """Zet het `forecast`-attribuut van de Zonneplan-tariefsensor om in PriceSlots.

    Onbruikbare regels worden overgeslagen; het resultaat is gesorteerd op start.
    """
    if not attributes:
        return []
    raw = attributes.get("forecast") or []
    slots: list[PriceSlot] = []
    for item in raw:
        try:
            start = _as_datetime(item["start_date"])
            end = _as_datetime(item["end_date"])
            buy = item["price_tax_included"]["amount"] / ZONNEPLAN_AMOUNT_DIVISOR
            ex = item.get("price_tax_excluded", {}).get("amount")
            buy_ex = ex / ZONNEPLAN_AMOUNT_DIVISOR if ex is not None else buy
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        slots.append(PriceSlot(start=start, end=end, buy=buy, buy_ex_tax=buy_ex))
    slots.sort(key=lambda s: s.start)
    return slots


def upcoming_slots(slots: list[PriceSlot], now: datetime) -> list[PriceSlot]:
    """Alleen het lopende en de toekomstige blokken."""
    return [s for s in slots if s.end > now]
