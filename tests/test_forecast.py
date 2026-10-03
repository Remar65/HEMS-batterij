from datetime import datetime, timedelta

from custom_components.hems_batterij.forecast import (
    LoadProfile,
    parse_solcast_detailed,
    pv_kwh_between,
)

T0 = datetime.fromisoformat("2026-10-04T12:00:00+02:00")


def test_solcast_overlap_per_quarter():
    attrs = {
        "detailedForecast": [
            {"period_start": "2026-10-04T12:00:00+02:00", "pv_estimate": 2.0, "pv_estimate10": 1.0},
            {"period_start": "2026-10-04T12:30:00+02:00", "pv_estimate": 3.0, "pv_estimate10": 1.5},
        ]
    }
    periods = parse_solcast_detailed(attrs)
    # Een kwartier van 2 kW = 0,5 kWh
    assert abs(pv_kwh_between(periods, T0, T0 + timedelta(minutes=15)) - 0.5) < 1e-9
    # Over de grens heen: 15 min x 2 kW + 15 min x 3 kW
    start = T0 + timedelta(minutes=15)
    assert abs(pv_kwh_between(periods, start, start + timedelta(minutes=30)) - 1.25) < 1e-9
    low = parse_solcast_detailed(attrs, "pv_estimate10")
    assert abs(pv_kwh_between(low, T0, T0 + timedelta(hours=1)) - 1.25) < 1e-9


def test_load_profile_fallbacks_and_learning():
    profile = LoadProfile(default_w=400, alpha=0.5)
    assert profile.expected_w(T0) == 400
    profile.update(T0, 1000)
    assert profile.expected_w(T0) == 1000
    # Andere dag, zelfde kwartier: gemiddelde van bekende dagen
    assert profile.expected_w(T0 + timedelta(days=1)) == 1000
    profile.update(T0, 2000)
    assert profile.expected_w(T0) == 1500
    profile.update(T0, -50)  # meetfout wordt genegeerd
    assert profile.expected_w(T0) == 1500
    assert abs(profile.kwh_between(T0, T0 + timedelta(minutes=15)) - 0.375) < 1e-9


def test_load_profile_roundtrip():
    profile = LoadProfile()
    profile.update(T0, 700)
    copy = LoadProfile.from_dict(profile.to_dict())
    assert copy.expected_w(T0) == 700
    assert copy.filled_bins == 1


def test_solcast_datetime_objects():
    attrs = {"detailedForecast": [{"period_start": T0, "pv_estimate": 1.0}]}
    periods = parse_solcast_detailed(attrs)
    assert abs(pv_kwh_between(periods, T0, T0 + timedelta(minutes=30)) - 0.5) < 1e-9
