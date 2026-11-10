"""Zelflerende uurcorrectie op de zonverwachting."""

from datetime import datetime, timedelta

from custom_components.hems_batterij.pv_correction import (
    MAX_FACTOR,
    MIN_DAYS,
    WINDOW_DAYS,
    PvHourCorrection,
)

START = datetime(2026, 11, 9, 15, 0)


def test_no_factor_until_enough_days() -> None:
    corr = PvHourCorrection()
    for d in range(MIN_DAYS - 1):
        corr.record("helios", START - timedelta(days=d), 0.5, 1.0)
    assert corr.factor("helios", 15) is None
    corr.record("helios", START - timedelta(days=MIN_DAYS - 1), 0.5, 1.0)
    assert corr.factor("helios", 15) == 0.5
    assert corr.factor("helios", 14) is None
    assert corr.factor("solcast", 15) is None


def test_factor_is_energy_weighted_and_clamped() -> None:
    corr = PvHourCorrection()
    for d in range(MIN_DAYS):
        # Zonnige dag telt zwaarder dan een grijze dag.
        corr.record("solcast", START - timedelta(days=d), 3.0 if d == 0 else 0.2, 4.0 if d == 0 else 0.4)
    assert abs(corr.factor("solcast", 15) - (3.0 + 0.8) / (4.0 + 1.6)) < 1e-9
    wild = PvHourCorrection()
    for d in range(MIN_DAYS):
        wild.record("solcast", START - timedelta(days=d), 9.0, 1.0)
    assert wild.factor("solcast", 15) == MAX_FACTOR


def test_old_days_drop_out_of_the_window() -> None:
    corr = PvHourCorrection()
    for d in range(MIN_DAYS):
        corr.record("helios", START - timedelta(days=WINDOW_DAYS + d), 0.1, 1.0)
    for d in range(MIN_DAYS):
        corr.record("helios", START - timedelta(days=d), 0.8, 1.0)
    assert abs(corr.factor("helios", 15) - 0.8) < 1e-9
    assert corr.days("helios") == MIN_DAYS


def test_dark_hours_are_ignored() -> None:
    corr = PvHourCorrection()
    corr.record("helios", START.replace(hour=2), 0.0, 0.0)
    assert corr.days("helios") == 0


def test_round_trip() -> None:
    corr = PvHourCorrection()
    for d in range(MIN_DAYS):
        corr.record("helios", START - timedelta(days=d), 0.6, 1.0)
    again = PvHourCorrection.from_dict(corr.to_dict())
    assert again.factors("helios") == {15: 0.6}
    assert PvHourCorrection.from_dict({"helios": {"bad": {"x": [1]}}}).factors("helios") == {}
    assert PvHourCorrection.from_dict(None).days("helios") == 0
