from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.hems_batterij.heat import HeatModel, daily_means, parse_open_meteo

TZ = ZoneInfo("Europe/Amsterdam")


def _hours(kwh: float, hours: range) -> list[float]:
    per = kwh / len(hours)
    return [per if h in hours else 0.0 for h in range(24)]


def test_parse_open_meteo_and_daily_means():
    start = int(datetime(2026, 10, 23, 22, 0, tzinfo=UTC).timestamp())  # 24-10 00:00 lokaal
    payload = {
        "hourly": {
            "time": [start + 3600 * i for i in range(30)],
            "temperature_2m": [10.0] * 12 + [14.0] * 12 + [None] + [5.0] * 5,
        }
    }
    temps = parse_open_meteo(payload)
    assert len(temps) == 29
    assert next(iter(temps)).tzinfo is not None
    means = daily_means(temps, TZ)
    assert means == {date(2026, 10, 24): 12.0}  # 25-10 heeft te weinig uren
    assert parse_open_meteo(None) == {}
    assert parse_open_meteo({"hourly": {"time": 1}}) == {}


def test_model_learns_more_heating_when_colder():
    model = HeatModel()
    day = date(2026, 11, 1)
    # 3 kWh per graad onder 15 °C; stoken vooral 's middags op zachte dagen.
    for i, temp in enumerate([14, 12, 10, 8, 6, 4, 2, 0, 15, 13]):
        kwh = max(0.0, 3.0 * (15 - temp))
        hours = range(11, 17) if temp >= 6 else range(0, 24)
        model.add_day(day + timedelta(days=i), kwh, temp, _hours(kwh, hours))
    line = model.fit()
    assert line is not None
    assert abs(model.day_kwh(5.0) - 30.0) < 1.5
    assert model.day_kwh(25.0) == 0.0
    # Zachte dag: alles in de middag; koude dag: over de hele dag.
    assert model.share(13, 10.0) > 0.1 and model.share(3, 10.0) == 0.0
    assert abs(model.share(3, 0.0) - 1 / 24) < 0.01
    summary = model.summary()
    assert summary["geleerd"] and abs(summary["kwh_per_graad_kouder"] - 3.0) < 0.2

    restored = HeatModel.from_dict(model.to_dict())
    assert restored.fit() == line


def test_kwh_between_uses_shape_and_day_temperature():
    model = HeatModel()
    for i in range(8):
        temp = 2.0 + i
        kwh = 2.0 * (15 - temp)
        model.add_day(date(2026, 11, 1) + timedelta(days=i), kwh, temp, _hours(kwh, range(0, 24)))
    days = {date(2026, 11, 20): 5.0}
    start = datetime(2026, 11, 20, 18, 0, tzinfo=TZ)
    two_hours = model.kwh_between(start, start + timedelta(hours=2), days, TZ)
    assert abs(two_hours - model.day_kwh(5.0) * 2 / 24) < 0.05
    # Dag zonder temperatuur: geen verwarming bekend.
    other = datetime(2026, 11, 21, 18, 0, tzinfo=TZ)
    assert model.kwh_between(other, other + timedelta(hours=2), days, TZ) == 0.0
    # Een UTC-begin telt in lokale uren.
    utc = start.astimezone(UTC)
    assert abs(model.kwh_between(utc, utc + timedelta(hours=2), days, TZ) - two_hours) < 1e-9


def test_without_enough_days_falls_back_to_measured_average():
    model = HeatModel()
    assert model.day_kwh(0.0) == 0.0
    model.add_day(date(2026, 11, 1), 4.0, 8.0, _hours(4.0, range(12, 16)))
    model.add_day(date(2026, 11, 2), 2.0, 9.0, _hours(2.0, range(12, 16)))
    assert model.fit() is None
    assert model.day_kwh(0.0) == 3.0
    assert model.summary()["geleerd"] is False
    # Warmer = meer stoken (bijv. houtkachel op koude dagen) is geen lijn om op te plannen.
    odd = HeatModel()
    for i in range(8):
        odd.add_day(date(2026, 11, 1) + timedelta(days=i), float(i), float(i), _hours(i + 1.0, range(24)))
    assert odd.fit() is None
