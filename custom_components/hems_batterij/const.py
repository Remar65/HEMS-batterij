"""Constanten voor HEMS Batterij."""

from __future__ import annotations

DOMAIN = "hems_batterij"
NAME = "HEMS Batterij"

# Standaard kijkt de integratie alleen mee. Aansturen gebeurt pas als in de opties
# batterijen zijn aangewezen ("controlled_batteries") én de schakelaar "HEMS stuurt"
# aan staat. Alleen actuator.py roept services aan (zie tests/test_no_writes.py).

# Configuratie (config flow)
CONF_P1_POWER = "p1_power"
CONF_PV_POWER = "pv_power"
CONF_PV_INVERTED = "pv_inverted"
CONF_PRICE_SENSOR = "price_sensor"
CONF_SOLCAST_TODAY = "solcast_today"
CONF_SOLCAST_TOMORROW = "solcast_tomorrow"
CONF_BATTERIES = "batteries"

# Opties
CONF_WEAR_ENTITY = "wear_entity"
CONF_WEAR_CT = "wear_ct"
CONF_DEFAULT_EFFICIENCY = "default_efficiency"
CONF_NETTING_END = "netting_end"
CONF_NETTING_ACTIVE = "netting_full_value"
CONF_DEFAULT_LOAD_W = "default_load_w"
CONF_SOLCAST_FIELD = "solcast_field"
CONF_PV_SOURCE = "pv_source"
CONF_HELIOS_ENTITIES = "helios_entities"
CONF_CONTROLLED = "controlled_batteries"

PV_SOURCE_HELIOS = "helios"
PV_SOURCE_SOLCAST = "solcast"
CONF_SOLAR_FIRST_CT = "solar_first_ct"
CONF_TRADE_MARGIN_CT = "trade_margin_ct"
CONF_INVESTMENT_EUR = "investment_eur"
CONF_FEED_IN_COST_CT = "feed_in_cost_ct"
CONF_PRICE_FORECAST = "price_forecast"
CONF_PRICE_FORECAST_URL = "price_forecast_url"
CONF_PRICE_FORECAST_JEROEN = "price_forecast_jeroen"
CONF_HEAT_FORECAST = "heat_forecast"
CONF_HEAT_POWER = "heat_power"
CONF_DHW_POWER = "dhw_power"
CONF_DHW_RESERVE = "dhw_reserve"

DEFAULTS = {
    CONF_P1_POWER: "sensor.p1_meter_power",
    CONF_PV_POWER: ["sensor.kwh_meter_power", "sensor.kwh_meter_power_2"],
    CONF_PV_INVERTED: True,
    CONF_PRICE_SENSOR: "sensor.zonneplan_current_quarter_hourly_electricity_tariff",
    CONF_SOLCAST_TODAY: "sensor.solcast_pv_forecast_voorspelling_vandaag",
    CONF_SOLCAST_TOMORROW: "sensor.solcast_pv_forecast_voorspelling_morgen",
    CONF_BATTERIES: "marstek_m1, marstek_m2",
    CONF_WEAR_ENTITY: "input_number.batterij_slijtagekosten",
    CONF_WEAR_CT: 4.7,
    CONF_DEFAULT_EFFICIENCY: 0.806,
    CONF_NETTING_END: "2027-01-01",
    CONF_NETTING_ACTIVE: True,
    CONF_DEFAULT_LOAD_W: 400,
    CONF_SOLCAST_FIELD: "pv_estimate",
    # Zonverwachting: Helios (per panelenveld, zelflerend) zat 6-13 okt 2026 elke dag
    # dichter bij de werkelijke opbrengst dan Solcast. Solcast blijft de terugval.
    CONF_PV_SOURCE: PV_SOURCE_HELIOS,
    CONF_HELIOS_ENTITIES: [
        "sensor.helios_apsystems_aiko_power_now",
        "sensor.helios_enphase_jinko_power_now",
    ],
    # Rangorde (Marco, 3 okt 2026): zon zelf gebruiken > batterij voor eigen huis > handel.
    CONF_SOLAR_FIRST_CT: 10.0,
    CONF_TRADE_MARGIN_CT: 3.0,
    # Aanschaf M1 (€1450) + M2 (€1175), voor de terugverdientijd.
    CONF_INVESTMENT_EUR: 2625.0,
    # Terugleverkosten per kWh na de saldering (nu nog onbekend, dus 0).
    CONF_FEED_IN_COST_CT: 0.0,
    # Prijsvoorspelling voor de dagen na de bekende Zonneplan-prijzen (Marco, 24 okt 2026:
    # "jeroen.nl + leren"). jeroen.nl eerst; lukt dat niet, dan EpexPredictor (de URL).
    CONF_PRICE_FORECAST: True,
    CONF_PRICE_FORECAST_JEROEN: True,
    # Stroom voor verwarming apart voorspellen uit de weersverwachting (Marco, 24 okt 2026).
    # Het vermogen is de template-sensor "WP-vermogen terwijl circuit 0 HEATING is".
    CONF_HEAT_FORECAST: True,
    CONF_HEAT_POWER: "sensor.warmtepomp_vermogen_verwarming",
    CONF_DHW_POWER: "sensor.warmtepomp_vermogen_warm_water",
    CONF_DHW_RESERVE: True,
    CONF_PRICE_FORECAST_URL: "https://epexpredictor.batzill.com/prices_short?region=NL&unit=EUR_PER_KWH&hours=168",
    # Batterijen die HEMS zelf aanstuurt (leeg = alleen meekijken). Marco, 17 okt 2026:
    # eerst alleen de M2; de M1 blijft bij HBC.
    CONF_CONTROLLED: [],
}

# Entiteiten per Marstek (ESPHome LilyGo), afgeleid van het voorvoegsel, bijv. "marstek_m1".
BATTERY_ENTITY_PATTERNS = {
    "soc": "sensor.{p}_battery_state_of_charge",
    "ac_power": "sensor.{p}_ac_power",  # - = laden, + = ontladen
    "capacity": "sensor.{p}_battery_total_energy",
    "efficiency": "sensor.{p}_lifetime_round_trip_efficiency",
    "max_charge": "number.{p}_max_charge_power",
    "max_discharge": "number.{p}_max_discharge_power",
    "soc_min": "number.{p}_discharging_cutoff_capacity",
    "soc_max": "number.{p}_charging_cutoff_capacity",
    "inverter_state": "sensor.{p}_inverter_state",
    # Staat deze op "disable", dan negeert de batterij elke aansturing via Modbus.
    "rs485_mode": "select.{p}_rs485_control_mode",
}
RS485_DISABLED = "disable"

# Aansturing via Modbus (ESPHome LilyGo): richting + vermogen.
CONTROL_ENTITY_PATTERNS = {
    "mode": "select.{p}_forcible_charge_discharge",  # stop / charge / discharge
    "charge_w": "number.{p}_forcible_charge_power",
    "discharge_w": "number.{p}_forcible_discharge_power",
}
CONTROL_STOP = "stop"
CONTROL_CHARGE = "charge"
CONTROL_DISCHARGE = "discharge"
CONTROL_MIN_W = 50.0  # kleiner vermogen: batterij stilzetten
CONTROL_WRITE_DELTA_W = 50.0  # vermogen pas opnieuw sturen bij zoveel verschil
CONTROL_RESEND_SECONDS = 15.0  # zelfde waarde niet vaker opnieuw sturen dan dit

# HBC: de actieve deelstrategie en de grenzen die HBC per batterij gebruikt. Daarmee
# speelt de schaduw-HBC na wat HBC met een door HEMS gestuurde batterij had gedaan.
HBC_SUB_STRATEGY_ENTITY = "input_text.house_battery_strategy_active_sub_strategy"
HBC_CHARGE_TARGET_SOC_ENTITY = "input_number.house_battery_strategy_charge_target_soc"
HBC_SELL_TARGET_SOC_ENTITY = "input_number.house_battery_strategy_sell_target_soc"
HBC_CUTOFF_PATTERNS = {
    "soc_min": "input_number.{p}_discharging_cutoff_capacity",
    "soc_max": "input_number.{p}_charging_cutoff_capacity",
}

# ESPHome stuurt alleen wijzigingen door: een stilstaande batterij meldt geen nieuw
# AC-vermogen. Daarom telt een batterij als actueel zolang één van deze sensoren recent
# een waarde gaf (netspanning en -frequentie veranderen vrijwel continu).
BATTERY_HEARTBEAT_PATTERNS = (
    "sensor.{p}_ac_power",
    "sensor.{p}_ac_voltage",
    "sensor.{p}_ac_frequency",
    "sensor.{p}_battery_voltage",
)

# Binary sensors die betekenen dat een batterij niet gebruikt moet worden.
BATTERY_ALARM_SUFFIXES = {
    "bat_communication_failure": "communicatiefout batterij",
    "bms_protect": "BMS-beveiliging actief",
    "bat_overcurrent": "overstroom",
    "bat_overvoltage": "overspanning",
    "bat_undervoltage": "onderspanning",
    "overtemperature_limit": "te warm",
    "low_temperature_limit": "te koud",
    "grid_overvoltage": "netspanning te hoog",
    "grid_undervoltage": "netspanning te laag",
}

FAST_LOOP_SECONDS = 5
STALE_P1_SECONDS = 30
STALE_BATTERY_SECONDS = 120
PLAN_STEP_KWH = 0.1
PRICE_FORECAST_MAX_AGE_HOURS = 36  # oudere voorspelling niet meer gebruiken
PRICE_FORECAST_TIMEOUT_SECONDS = 20
# Publieke MCP-server van jeroen.nl: gratis voor eigen domotica, max 60 aanroepen per uur
# per IP-adres (https://jeroen.nl/api-voorwaarden). Wij vragen één keer per uur.
JEROEN_MCP_URL = "https://jeroen.nl/mcp"
# Uurtemperatuur (gisteren t/m 7 dagen vooruit) voor de locatie van Home Assistant.
# Gratis voor niet-commercieel gebruik; wij vragen één keer per uur.
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat:.3f}&longitude={lon:.3f}"
    "&hourly=temperature_2m&past_days=2&forecast_days=8&timezone=GMT&timeformat=unixtime"
)
DECISION_LOG_SIZE = 50

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.load_profile"
