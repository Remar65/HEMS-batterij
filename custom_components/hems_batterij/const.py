"""Constanten voor HEMS Batterij."""

from __future__ import annotations

DOMAIN = "hems_batterij"
NAME = "HEMS Batterij"

# Deze versie kijkt alleen mee. Er bestaat in deze integratie geen code die
# batterij-entiteiten aanstuurt; deze vlag staat er om dat ook in de UI te tonen.
SHADOW_MODE = True

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
CONF_SOLAR_FIRST_CT = "solar_first_ct"
CONF_TRADE_MARGIN_CT = "trade_margin_ct"
CONF_INVESTMENT_EUR = "investment_eur"
CONF_FEED_IN_COST_CT = "feed_in_cost_ct"

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
    # Rangorde (Marco, 3 okt 2026): zon zelf gebruiken > batterij voor eigen huis > handel.
    CONF_SOLAR_FIRST_CT: 10.0,
    CONF_TRADE_MARGIN_CT: 3.0,
    # Aanschaf M1 (€1450) + M2 (€1175), voor de terugverdientijd.
    CONF_INVESTMENT_EUR: 2625.0,
    # Terugleverkosten per kWh na de saldering (nu nog onbekend, dus 0).
    CONF_FEED_IN_COST_CT: 0.0,
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
DECISION_LOG_SIZE = 50

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.load_profile"
