"""Meetlogger verwarming: elk kwartier één regel met het klimaat in en om het huis.

Doel (wens Marco, 19-11-2026): over enkele weken een rapportage kunnen draaien om het
verwarmingssysteem te beoordelen en in te regelen (stooklijn, minimale aanvoer,
ruimte-invloed). Home Assistant bewaart kwartierdata maar 10 dagen; deze logger
schrijft zelf een CSV per maand in <config>/hems_meetdata/ en houdt die onbeperkt.

Alleen lezen: de logger stuurt niets aan. Tellerstanden (kWh, starts, uren) staan
erin als absolute stand, zodat de rapportage per kwartier het verschil kan nemen.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import csv
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

FOLDER = "hems_meetdata"
PREFIX = "verwarming"
MAX_EXPORT_ROWS = 6000  # ruim 2 maanden kwartieren per opvraag


@dataclass(frozen=True)
class Column:
    name: str
    entity_id: str
    attribute: str | None = None
    numeric: bool = True


# Volgorde = kolomvolgorde in de CSV. Nieuwe kolommen achteraan toevoegen.
COLUMNS: tuple[Column, ...] = (
    # buiten
    Column("buiten_gem_c", "sensor.buitentemperatuur_gemiddeld"),
    Column("buiten_vaillant_c", "sensor.heating_ebusd_basv2_outsidetemp"),
    Column("buiten_vaillant_gem_c", "sensor.heating_ebusd_basv2_outsidetempavg"),
    Column("buiten_switchbot_c", "sensor.indoor_outdoor_meter_2066_temperatuur"),
    Column("buiten_rv_pct", "sensor.indoor_outdoor_meter_2066_luchtvochtigheid"),
    Column("wind_kmh", "weather.forecast_thuis", "wind_speed"),
    Column("windstoten_kmh", "weather.forecast_thuis", "wind_gust_speed"),
    Column("windrichting_gr", "weather.forecast_thuis", "wind_bearing"),
    Column("bewolking_pct", "weather.forecast_thuis", "cloud_coverage"),
    Column("weer", "weather.forecast_thuis", None, numeric=False),
    Column("pv_aiko_w", "sensor.kwh_meter_power"),
    Column("pv_jinko_w", "sensor.kwh_meter_power_2"),
    # binnen
    Column("woonkamer_c", "sensor.meter_pro_co2_203d_temperatuur"),
    Column("vrc_kamer_c", "sensor.my_home_zone_wonen_circuit_0_current_temperature"),
    Column("keuken_c", "sensor.keuken_temperatuur"),
    Column("werkkamer_c", "sensor.sonoff_snzb_02d_temperatuur"),
    Column("slaapkamer_c", "sensor.sonoff_snzb_02d_temperatuur_2"),
    Column("woonkamer_rv_pct", "sensor.heating_ebusd_basv2_z1roomhumidity"),
    # instellingen
    Column("vrc_modus", "climate.my_home_zone_wonen_circuit_0_climate", None, numeric=False),
    Column("vrc_setpoint_c", "climate.my_home_zone_wonen_circuit_0_climate", "temperature"),
    Column("vrc_preset", "climate.my_home_zone_wonen_circuit_0_climate", "preset_mode", numeric=False),
    Column("stooklijn", "number.my_home_circuit_0_heating_curve"),
    Column("min_aanvoer_c", "sensor.heating_ebusd_basv2_hc1heatingflowtempmin"),
    Column("max_aanvoer_c", "sensor.heating_ebusd_basv2_hc1heatingflowtempmax"),
    Column("ruimte_invloed", "sensor.heating_ebusd_basv2_hc1roomtempmodulation", None, numeric=False),
    Column("vloerbuffer_modus", "input_select.vloerbuffer_modus", None, numeric=False),
    # warmtepomp
    Column("status", "sensor.heating_ebusd_hmu_rundatastatuscode", None, numeric=False),
    Column("aanvoer_c", "sensor.heating_ebusd_basv2_hc1flowtemp"),
    Column("aanvoer_gewenst_c", "sensor.heating_ebusd_basv2_hc1flowtempdesired"),
    Column("aanvoer_wp_c", "sensor.heating_ebusd_hmu_flowtemp"),
    Column("wp_status01_temp_c", "sensor.heating_ebusd_hmu_status01_temp"),
    Column("wp_status01_temp1_c", "sensor.heating_ebusd_hmu_status01_temp_1"),
    Column("hmu_doel_aanvoer_c", "sensor.heating_ebusd_hmu_setmode_flowtempdesired"),
    Column("energie_integraal", "sensor.heating_ebusd_hmu_energyintegral"),
    Column("luchtinlaat_c", "sensor.heating_ebusd_hmu_rundataairinlettemp"),
    Column("wp_vermogen_w", "sensor.kwh_meter_power_3"),
    Column("wp_stroom_kw_ebus", "sensor.heating_ebusd_hmu_currentconsumedpower"),
    Column("wp_warmte_kw", "sensor.heating_ebusd_hmu_currentyieldpower"),
    Column("vat_c", "sensor.heating_ebusd_basv2_hwcstoragetemp"),
    # tellers (absolute standen)
    Column("teller_wp_stroom_kwh", "sensor.kwh_meter_energy_import_3"),
    Column("teller_warmte_cv_dag_kwh", "sensor.heating_ebusd_hmu_yieldhcday"),
    Column("teller_warmte_ww_dag_kwh", "sensor.heating_ebusd_hmu_yieldhwcday"),
    Column("teller_stroom_verwarming_kwh", "sensor.warmtepomp_energie_verwarming"),
    Column("teller_stroom_warm_water_kwh", "sensor.warmtepomp_energie_warm_water"),
    Column("teller_compressor_starts", "sensor.heating_ebusd_hmu_runstatscompressorstarts"),
    Column("teller_compressor_uren", "sensor.heating_ebusd_hmu_runstatscompressorhours"),
    # context
    Column("prijs_eur_kwh", "sensor.zonneplan_current_quarter_hourly_electricity_tariff"),
    Column("warmteplan", "sensor.hems_batterij_warmteplan", None, numeric=False),
    # houtkachel (sensor op de muur naast de rookgaspijp, sinds 21-11-2026; was cv-hok)
    Column("kachel_c", "sensor.kachel_temperatuur"),
)

HEADER: tuple[str, ...] = ("tijd", *(c.name for c in COLUMNS))

StateGetter = Callable[[str], Any]  # entity_id -> State | None


def _value(state: Any, col: Column) -> str:
    if state is None:
        return ""
    raw = state.attributes.get(col.attribute) if col.attribute else state.state
    if raw is None:
        return ""
    if not col.numeric:
        return str(raw)
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return ""  # unavailable / unknown
    return f"{number:.3f}".rstrip("0").rstrip(".") if number != int(number) else str(int(number))


def build_row(get_state: StateGetter, now: datetime) -> list[str]:
    """Eén CSV-regel; ontbrekende of onbekende waarden worden een leeg veld."""
    return [now.isoformat(timespec="seconds"), *(_value(get_state(c.entity_id), c) for c in COLUMNS)]


def _read_header(path: Path) -> list[str] | None:
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            return next(csv.reader(fh), None)
    except FileNotFoundError:
        return None


def target_file(folder: Path, when: datetime) -> Path:
    """Maandbestand; bij een gewijzigde kolomindeling een nieuw volgnummer."""
    base = f"{PREFIX}_{when:%Y-%m}"
    n = 1
    while True:
        path = folder / (f"{base}.csv" if n == 1 else f"{base}_{n}.csv")
        header = _read_header(path)
        if header is None or header == list(HEADER):
            return path
        n += 1


def append_row(folder: Path, when: datetime, row: list[str]) -> Path:
    """Blokkerend (executor): regel toevoegen, kop schrijven als het bestand nieuw is."""
    folder.mkdir(parents=True, exist_ok=True)
    path = target_file(folder, when)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(HEADER)
        writer.writerow(row)
    return path


def read_rows(
    folder: Path,
    start: datetime,
    end: datetime,
    columns: Iterable[str] | None = None,
    limit: int = MAX_EXPORT_ROWS,
) -> dict[str, Any]:
    """Blokkerend (executor): regels tussen start en end, oudste eerst, als lijst van lijsten."""
    wanted = ["tijd", *[c for c in (columns or HEADER[1:]) if c != "tijd"]]
    rows: list[list[str]] = []
    truncated = False
    files = sorted(folder.glob(f"{PREFIX}_*.csv")) if folder.exists() else []
    for path in files:
        with path.open(newline="", encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                try:
                    t = datetime.fromisoformat(rec["tijd"])
                except (KeyError, ValueError):
                    continue
                if not start <= t < end:
                    continue
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append([rec.get(c, "") or "" for c in wanted])
    rows.sort(key=lambda r: r[0])
    return {
        "kolommen": wanted,
        "rijen": rows,
        "aantal": len(rows),
        "afgekapt": truncated,
        "bestanden": [p.name for p in files],
    }


def folder(hass: Any) -> Path:
    return Path(hass.config.path(FOLDER))


async def async_log_measurement(hass: Any, now: datetime) -> None:
    """Elk kwartier aangeroepen: één regel wegschrijven (alleen lezen van states)."""
    from homeassistant.util import dt as dt_util

    local = dt_util.as_local(now).replace(second=0, microsecond=0)
    row = build_row(hass.states.get, local)
    try:
        await hass.async_add_executor_job(append_row, folder(hass), local, row)
    except OSError as err:
        _LOGGER.warning("Meetlogger kon niet schrijven: %s", err)
