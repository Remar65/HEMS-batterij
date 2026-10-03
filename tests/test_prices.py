from datetime import datetime

from custom_components.hems_batterij.prices import parse_zonneplan_forecast, upcoming_slots


def _item(start, end, inc, ex):
    return {
        "start_date": start,
        "end_date": end,
        "price_tax_included": {"amount": inc},
        "price_tax_excluded": {"amount": ex},
    }


def test_parse_zonneplan_amounts_and_order():
    attrs = {
        "forecast": [
            _item("2026-10-03T11:15:00+02:00", "2026-10-03T11:30:00+02:00", 2998366, 1889885),
            _item("2026-10-03T11:00:00+02:00", "2026-10-03T11:15:00+02:00", 3241697, 2133216),
            {"start_date": "kapot"},
        ]
    }
    slots = parse_zonneplan_forecast(attrs)
    assert len(slots) == 2
    assert slots[0].start.minute == 0
    assert abs(slots[1].buy - 0.2998366) < 1e-9
    assert abs(slots[1].buy_ex_tax - 0.1889885) < 1e-9


def test_upcoming_slots_keeps_running_quarter():
    attrs = {
        "forecast": [
            _item("2026-10-03T11:00:00+02:00", "2026-10-03T11:15:00+02:00", 1, 1),
            _item("2026-10-03T11:15:00+02:00", "2026-10-03T11:30:00+02:00", 1, 1),
        ]
    }
    now = datetime.fromisoformat("2026-10-03T11:20:00+02:00")
    assert len(upcoming_slots(parse_zonneplan_forecast(attrs), now)) == 1


def test_empty_attributes():
    assert parse_zonneplan_forecast(None) == []
    assert parse_zonneplan_forecast({}) == []
