"""Meetlogger verwarming: CSV-regels per kwartier en export via de service."""

from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hems_batterij import meetlogger
from custom_components.hems_batterij.const import DEFAULTS, DOMAIN

from .test_init import _set_states

TZ = dt_util.get_time_zone("Europe/Amsterdam")


def _getter(states: dict[str, State]):
    return states.get


def test_build_row_reads_states_and_attributes():
    now = datetime(2026, 11, 19, 10, 15, tzinfo=TZ)
    states = {
        "sensor.buitentemperatuur_gemiddeld": State("sensor.buitentemperatuur_gemiddeld", "8.25"),
        "weather.forecast_thuis": State(
            "weather.forecast_thuis", "rainy", {"wind_speed": 10.4, "wind_bearing": 110}
        ),
        "climate.my_home_zone_wonen_circuit_0_climate": State(
            "climate.my_home_zone_wonen_circuit_0_climate", "heat_cool", {"temperature": 20.0}
        ),
        "number.my_home_circuit_0_heating_curve": State("number.my_home_circuit_0_heating_curve", "0.25"),
        "sensor.heating_ebusd_basv2_hc1flowtemp": State(
            "sensor.heating_ebusd_basv2_hc1flowtemp", "unavailable"
        ),
    }
    row = dict(zip(meetlogger.HEADER, meetlogger.build_row(_getter(states), now), strict=True))
    assert row["tijd"] == "2026-11-19T10:15:00+01:00"
    assert row["buiten_gem_c"] == "8.25"
    assert row["wind_kmh"] == "10.4"
    assert row["windrichting_gr"] == "110"
    assert row["weer"] == "rainy"
    assert row["vrc_modus"] == "heat_cool"
    assert row["vrc_setpoint_c"] == "20"
    assert row["stooklijn"] == "0.25"
    assert row["aanvoer_c"] == ""  # unavailable
    assert row["keuken_c"] == ""  # ontbreekt


def test_append_and_read_rows(tmp_path: Path):
    start = datetime(2026, 11, 30, 23, 30, tzinfo=TZ)
    for i in range(4):  # loopt over de maandgrens
        t = start + timedelta(minutes=15 * i)
        meetlogger.append_row(tmp_path, t, meetlogger.build_row(lambda _e: None, t))
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["verwarming_2026-11.csv", "verwarming_2026-12.csv"]

    out = meetlogger.read_rows(tmp_path, start, start + timedelta(hours=1), ["aanvoer_c"])
    assert out["kolommen"] == ["tijd", "aanvoer_c"]
    assert out["aantal"] == 4
    assert out["rijen"][0][0] == "2026-11-30T23:30:00+01:00"
    assert out["afgekapt"] is False

    limited = meetlogger.read_rows(tmp_path, start, start + timedelta(hours=1), limit=2)
    assert limited["aantal"] == 2 and limited["afgekapt"] is True


def test_changed_columns_start_new_file(tmp_path: Path):
    t = datetime(2026, 11, 19, 10, 0, tzinfo=TZ)
    old = "tijd,oud\n2026-11-19T09:45:00+01:00,1\n"
    (tmp_path / "verwarming_2026-11.csv").write_text(old, encoding="utf-8")
    path = meetlogger.append_row(tmp_path, t, meetlogger.build_row(lambda _e: None, t))
    assert path.name == "verwarming_2026-11_2.csv"
    out = meetlogger.read_rows(tmp_path, t - timedelta(hours=1), t + timedelta(hours=1))
    assert out["aantal"] == 2  # oude regel (met lege nieuwe kolommen) + nieuwe regel


async def test_service_exports_logged_rows(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    _set_states(hass)
    hass.states.async_set("sensor.buitentemperatuur_gemiddeld", "7.5")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            k: DEFAULTS[k]
            for k in (
                "p1_power",
                "pv_power",
                "pv_inverted",
                "price_sensor",
                "solcast_today",
                "solcast_tomorrow",
                "batteries",
            )
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    now = dt_util.now().replace(second=0, microsecond=0)
    await meetlogger.async_log_measurement(hass, now)

    result = await hass.services.async_call(
        DOMAIN,
        "meetdata_exporteren",
        {"start": (now - timedelta(minutes=1)).isoformat(), "kolommen": ["buiten_gem_c"]},
        blocking=True,
        return_response=True,
    )
    assert result["kolommen"] == ["tijd", "buiten_gem_c"]
    assert result["aantal"] == 1
    assert result["rijen"][0][1] == "7.5"
    assert Path(hass.config.path(meetlogger.FOLDER)).exists()
