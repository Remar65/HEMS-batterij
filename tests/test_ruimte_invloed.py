"""Ruimte-invloed VRC: blokken beoordelen, drempel leren, advies en kachel herkennen."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hems_batterij import meetlogger, ruimte_invloed as ri, vaillant_actuator
from custom_components.hems_batterij.ruimte_invloed_ha import RoomInfluence

TZ = dt_util.get_time_zone("Europe/Amsterdam")
COLS = [
    "tijd",
    "ruimte_invloed",
    "vrc_setpoint_c",
    "vrc_kamer_c",
    "keuken_c",
    "buiten_gem_c",
    "wind_kmh",
    "pv_aiko_w",
    "pv_jinko_w",
    "kachel_c",
]


def _night(start: datetime, setting: str, t_out: float, vrc, kitchen=None, stove=None, wind=5.0):
    """40 kwartieren van 22:00 tot 08:00; vrc/kitchen/stove mogen een functie van i zijn."""
    rows = []
    for i in range(40):
        t = start + timedelta(minutes=15 * i)
        v = vrc(i) if callable(vrc) else vrc
        k = kitchen(i) if callable(kitchen) else (kitchen if kitchen is not None else v)
        s = stove(i) if callable(stove) else stove
        rows.append(
            [
                t.isoformat(),
                setting,
                "20",
                str(v),
                str(k),
                str(t_out),
                str(wind),
                "0",
                "0",
                "" if s is None else str(s),
            ]
        )
    return rows


def _blocks(rows, now=None):
    quarters = ri.quarters_from_rows(COLS, rows)
    return ri.make_blocks(quarters, now or datetime(2027, 1, 1, tzinfo=TZ))


def test_expanded_cold_night_means_active_better():
    rows = _night(datetime(2026, 11, 20, 22, tzinfo=TZ), "expanded", 5.3, lambda i: 20.1 if i < 12 else 19.6)
    [b] = _blocks(rows)
    assert b.part == "nacht" and b.setting == "expanded"
    assert b.label == "actief_beter" and b.cold_q == 28


def test_active_overshoot_from_stove_does_not_count():
    start = datetime(2026, 11, 21, 22, tzinfo=TZ)
    # kachel brandt de eerste 3 uur; kamer en keuken lopen op tot 21,5
    rows = _night(
        start,
        "active",
        9.0,
        lambda i: 21.5 if i < 24 else 20.2,
        lambda i: 21.0 if i < 24 else 20.1,
        stove=lambda i: 45 if i < 12 else 24,
    )
    [b] = _blocks(rows)
    assert b.warm_q == 0 and b.label == "actief_ok"


def test_active_overshoot_without_stove_means_expanded_better():
    rows = _night(datetime(2026, 11, 22, 22, tzinfo=TZ), "active", 11.0, 20.9, 20.6, stove=22)
    [b] = _blocks(rows)
    assert b.label == "uitgebreid_beter" and b.warm_q == 40


def test_unfinished_block_is_skipped():
    start = datetime(2026, 11, 20, 22, tzinfo=TZ)
    rows = _night(start, "expanded", 5.0, 19.5)
    assert _blocks(rows, now=start + timedelta(hours=5)) == []


def test_stove_detection_with_and_without_sensor():
    t = datetime(2026, 11, 20, 18, tzinfo=TZ)
    qs = [
        ri.Quarter(t + timedelta(minutes=15 * i), "active", 20, 20, 20, 5, 0, 0, 22 + (10 if i >= 4 else 0))
        for i in range(6)
    ]
    assert ri.stove_flags(qs) == [False] * 4 + [True] * 2
    q = ri.Quarter(t, "active", 20, 21.5, 20.0, 5, 0, 0, None)
    assert ri.stove_flags([q]) == [True]
    sunny = ri.Quarter(t, "active", 20, 21.5, 20.0, 5, 0, 2000, None)
    assert ri.stove_flags([sunny]) == [False]


def test_prior_until_enough_blocks():
    fitted = ri.fit([])
    assert not fitted.reliable and fitted.threshold == ri.PRIOR_THRESHOLD
    assert ri.advise("expanded", fitted, 5.0, 5.0)[0] == "active"
    assert ri.advise("active", fitted, 8.3, 5.0)[0] == "active"  # binnen hysterese
    assert ri.advise("active", fitted, 9.0, 5.0)[0] == "expanded"


def test_fit_learns_threshold_from_blocks():
    rows = []
    day = datetime(2026, 11, 1, 22, tzinfo=TZ)
    # koude nachten op Uitgebreid te koud, zachte nachten op Uitgebreid prima, warme op Actief te warm
    for n, t_out in enumerate((2, 3, 4, 5)):
        rows += _night(day + timedelta(days=n), "expanded", t_out, 19.5)
    for n, t_out in enumerate((7.5, 8, 9, 10)):
        rows += _night(day + timedelta(days=4 + n), "expanded", t_out, 20.1)
    for n, t_out in enumerate((11, 12)):
        rows += _night(day + timedelta(days=8 + n), "active", t_out, 20.8, 20.6, stove=22)
    fitted = ri.fit(_blocks(rows))
    assert fitted.reliable
    assert 5 < fitted.threshold <= 7.5
    assert fitted.errors == 0
    summary = ri.summary(_blocks(rows), fitted, "expanded", "active", 4.0, {"buiten_c": 4.0, "wind_kmh": 5})
    assert summary["advies"] == "actief" and summary["betrouwbaar"] is True
    assert len(summary["recente_blokken"]) == 10


def test_wind_factor_found_when_wind_explains_cold():
    rows = []
    day = datetime(2026, 11, 1, 22, tzinfo=TZ)
    # zelfde buitentemperatuur; alleen stormachtige nachten zijn op Uitgebreid te koud
    for n in range(3):
        rows += _night(day + timedelta(days=n), "expanded", 9.0, 19.5, wind=45)
    for n in range(4):
        rows += _night(day + timedelta(days=3 + n), "expanded", 9.0, 20.1, wind=5)
    fitted = ri.fit(_blocks(rows))
    assert fitted.reliable and fitted.wind_factor > 0 and fitted.errors == 0


async def test_manager_learns_from_logger_and_notifies(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    folder = meetlogger.folder(hass)
    now = dt_util.now()
    start = (now - timedelta(days=2)).replace(hour=22, minute=0, second=0, microsecond=0)
    rows = _night(start, "expanded", 4.0, 19.5)
    header = list(meetlogger.HEADER)
    for r in rows:
        rec = dict(zip(COLS, r, strict=True))
        meetlogger.append_row(folder, datetime.fromisoformat(rec["tijd"]), [rec.get(c, "") for c in header])
    hass.states.async_set("sensor.heating_ebusd_basv2_hc1roomtempmodulation", "expanded")

    class Coord:
        _temps = {
            dt_util.as_utc(now).replace(minute=0, second=0, microsecond=0) + timedelta(hours=h): 3.0
            for h in range(1, 13)
        }
        _wind = {}

    manager = RoomInfluence(hass, Coord())
    with patch.object(vaillant_actuator, "async_set_room_influence") as setter:
        await manager.async_update(now)
    setter.assert_not_called()  # automatisch staat uit
    assert manager.data["advies"] == "actief"
    assert manager.data["recente_blokken"][-1]["oordeel"] == "actief_beter"
    assert manager._notified == "active"


async def test_auto_switch_only_when_reliable(hass: HomeAssistant) -> None:
    manager = RoomInfluence(hass, None)
    manager.auto_enabled = True
    now = dt_util.now()
    with (
        patch.object(vaillant_actuator, "async_set_room_influence", return_value=True) as setter,
        patch.object(vaillant_actuator, "async_request_read", return_value=True),
    ):
        await manager._async_act(now, "expanded", "active", reliable=False, stove_now=False)
        setter.assert_not_called()
        await manager._async_act(now, "expanded", "active", reliable=True, stove_now=True)
        setter.assert_not_called()
        await manager._async_act(now, "expanded", "active", reliable=True, stove_now=False)
        setter.assert_called_once_with(hass, "active")
        await manager._async_act(
            now + timedelta(hours=1), "active", "expanded", reliable=True, stove_now=False
        )
        assert setter.call_count == 1  # niet binnen 12 uur opnieuw


async def test_set_rejects_other_values(hass: HomeAssistant) -> None:
    with pytest.raises(ValueError):
        await vaillant_actuator.async_set_room_influence(hass, "inactive")
    assert await vaillant_actuator.async_request_read(hass) is False  # geen mqtt in de test
