"""HEMS stuurt: actuator, samenwerking met HBC op één P1-meter, schaduw-HBC en de schakelaar."""

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.hems_batterij.actuator import Command, command_for, writes_needed
from custom_components.hems_batterij.const import DEFAULTS, DOMAIN
from custom_components.hems_batterij.controller import ControllerSettings, ControlTarget, control_step
from custom_components.hems_batterij.coordinator import _prices_from_store
from custom_components.hems_batterij.planner import (
    MODE_GRID_CHARGE,
    MODE_SELF_CONSUMPTION,
    MODE_SELL,
    Plan,
    PlanStep,
)
from custom_components.hems_batterij.shadow_hbc import HbcTargets, hbc_counteracts, hbc_total_w

from .test_controller import bat
from .test_init import _set_states

SELF = ControlTarget(MODE_SELF_CONSUMPTION, max_charge_w=5000, max_discharge_w=5000)
SETTINGS = ControllerSettings(smoothing=0.0)


# ------------------------------------------------------------------ actuator
def test_command_for_directions_and_limits() -> None:
    assert command_for(20, 2500) == Command("stop", 0)
    assert command_for(-1800.4, 2500) == Command("charge", 1800)
    assert command_for(3000, 2500) == Command("discharge", 2500)


def test_writes_only_what_differs() -> None:
    wanted = Command("charge", 1800)
    assert writes_needed(wanted, "stop", 0, 0) == [("charge_w", 1800), ("mode", "charge")]
    assert writes_needed(wanted, "charge", 1790, 0) == []
    assert writes_needed(wanted, "charge", 1500, 0) == [("charge_w", 1800)]
    assert writes_needed(Command("stop", 0), "discharge", 0, 800) == [("mode", "stop")]


# --------------------------------------------------- twee regelaars, één meter
def test_takes_half_the_error_when_other_battery_regulates() -> None:
    alone = control_step(SELF, 600, [bat("m2", 50)], None, SETTINGS)
    shared = control_step(SELF, 600, [bat("m2", 50)], None, SETTINGS, other_battery_w=400)
    assert alone.total_w == 600
    assert shared.total_w == 300


def test_never_works_against_other_battery() -> None:
    # HBC laadt M1 met 1500 W, het net levert nog 200 W: HEMS gaat niet ontladen om dat te dekken.
    result = control_step(SELF, 200, [bat("m2", 50)], None, SETTINGS, other_battery_w=-1500)
    assert result.total_w == 0
    # Klein verschil met een stilstaande andere batterij: gewoon zelf regelen.
    assert control_step(SELF, 200, [bat("m2", 50)], None, SETTINGS, other_battery_w=10).total_w == 200


# ---------------------------------------------------------------- schaduw-HBC
def test_shadow_hbc_follows_sub_strategy() -> None:
    b = [bat("m2", 50)]
    targets = HbcTargets(charge_target_soc=90, sell_target_soc=80)
    assert hbc_total_w("Self-consumption", 600, b, targets) == 600
    assert hbc_total_w("Self-consumption", -3000, b, targets) == -2500
    assert hbc_total_w("Charge", 300, b, targets) == -2500
    assert hbc_total_w("Charge", 300, [bat("m2", 95)], targets) == 0
    assert hbc_total_w("Sell", -400, [bat("m2", 90)], targets) == 2500
    assert hbc_total_w("Sell", -400, b, targets) == 0  # onder verkoopdoel: alleen nul-import
    assert hbc_total_w("Charge PV", 500, b, targets) == 0
    assert hbc_total_w("Charge PV", -500, b, targets) == -500
    assert hbc_total_w("Standby / peak shave", 800, b, targets) == 0
    assert hbc_total_w(None, 400, b, targets) == 400


# ------------------------------------------------------------ in Home Assistant
async def test_switch_controls_only_m2_and_stops_it_again(hass: HomeAssistant) -> None:
    _set_states(hass)
    for prefix in ("marstek_m1", "marstek_m2"):
        hass.states.async_set(f"select.{prefix}_forcible_charge_discharge", "stop")
        hass.states.async_set(f"number.{prefix}_forcible_charge_power", "0")
        hass.states.async_set(f"number.{prefix}_forcible_discharge_power", "0")
    hass.states.async_set("input_text.house_battery_strategy_active_sub_strategy", "Self-consumption")
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"batteries": "marstek_m1, marstek_m2"},
        options={"controlled_batteries": ["marstek_m2"]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    # Schakelaar staat standaard uit: alleen meekijken, niets gestuurd.
    switch_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_sturing")
    assert hass.states.get(switch_id).state == "off"
    await coordinator._async_fast_tick(None)
    assert selects == [] and numbers == []
    assert coordinator.data.control_active is False

    # Aan: P1 vraagt 350 W, dus M2 gaat ontladen; M1 wordt niet aangeraakt.
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=True)
    assert coordinator.data.control_active is True
    assert coordinator.data.controlled == ["marstek_m2"]
    assert coordinator.data.commands["marstek_m2"].mode == "discharge"
    touched = {c.data["entity_id"] for c in selects + numbers}
    assert touched == {
        "select.marstek_m2_forcible_charge_discharge",
        "number.marstek_m2_forcible_discharge_power",
    }
    assert coordinator.data.shadow_result is not None
    assert coordinator.data.shadow_result.mode.startswith("hbc")

    # Uit: M2 terug op stop, zodat HBC of de terugval-automatisering hem weer kan overnemen.
    hass.states.async_set("select.marstek_m2_forcible_charge_discharge", "discharge")
    selects.clear()
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)
    assert [c.data for c in selects] == [
        {"entity_id": "select.marstek_m2_forcible_charge_discharge", "option": "stop"}
    ]
    assert coordinator.data.control_active is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unusable_battery_is_stopped(hass: HomeAssistant) -> None:
    _set_states(hass)
    hass.states.async_set("select.marstek_m2_forcible_charge_discharge", "charge")
    hass.states.async_set("binary_sensor.marstek_m2_bms_protect", "on")
    selects = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"batteries": "marstek_m1, marstek_m2"},
        options={"controlled_batteries": ["marstek_m2"]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.async_set_control(True)
    assert [c.data["option"] for c in selects] == ["stop"]
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_hbc_counteracts_fixed_orders() -> None:
    m1 = [bat("m1", 50)]
    # HEMS laadt uit het net terwijl HBC zelfverbruik doet: HBC ontlaadt M1 ertegenin.
    assert hbc_counteracts(MODE_GRID_CHARGE, "Self-consumption", m1)
    assert hbc_counteracts(MODE_GRID_CHARGE, None, m1)
    # HBC laadt zelf ook, of M1 is leeg: geen probleem.
    assert not hbc_counteracts(MODE_GRID_CHARGE, "Charge", m1)
    assert not hbc_counteracts(MODE_GRID_CHARGE, "Self-consumption", [bat("m1", 10)])
    # Verkopen terwijl HBC overschot in M1 opslaat: tegenwerken; bij nul-import niet.
    assert hbc_counteracts(MODE_SELL, "Self-consumption", m1)
    assert hbc_counteracts(MODE_SELL, "Charge PV", m1)
    assert not hbc_counteracts(MODE_SELL, "Zero import", m1)
    assert not hbc_counteracts(MODE_SELL, "Self-consumption", [bat("m1", 100)])
    assert not hbc_counteracts(MODE_SELF_CONSUMPTION, "Self-consumption", m1)


async def test_no_grid_charge_while_hbc_covers_it_from_m1(hass: HomeAssistant) -> None:
    """19 okt 16:00: plan net_laden voor M2, HBC op zelfverbruik ontlaadde M1 ertegenin."""
    _set_states(hass)
    for prefix in ("marstek_m1", "marstek_m2"):
        hass.states.async_set(f"select.{prefix}_forcible_charge_discharge", "stop")
        hass.states.async_set(f"number.{prefix}_forcible_charge_power", "0")
        hass.states.async_set(f"number.{prefix}_forcible_discharge_power", "0")
    hass.states.async_set("input_text.house_battery_strategy_active_sub_strategy", "Self-consumption")
    async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"batteries": "marstek_m1, marstek_m2"},
        options={"controlled_batteries": ["marstek_m2"]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    await coordinator.async_set_control(True)

    step = PlanStep(
        start=dt_util.now() - timedelta(minutes=1),
        end=dt_util.now() + timedelta(minutes=14),
        buy=0.345,
        sell=0.345,
        pv_kwh=0.0,
        load_kwh=0.1,
        soc_start_kwh=1.0,
        soc_end_kwh=1.2,
        battery_ac_kwh=-0.23,
        grid_kwh=0.05,
        cost_eur=0.0,
        baseline_cost_eur=0.0,
        mode=MODE_GRID_CHARGE,
        fixed_w=-900.0,
        max_charge_w=900.0,
    )

    coordinator._plan = Plan(created=dt_util.now(), steps=[step])
    await coordinator._async_fast_tick(None)
    assert coordinator.data.commands["marstek_m2"].mode != "charge"
    assert coordinator.data.result.reason.startswith("niet uit het net laden")

    # HBC laadt M1 zelf ook goedkoop: dan mag HEMS M2 wel uit het net laden.
    hass.states.async_set("input_text.house_battery_strategy_active_sub_strategy", "Charge")
    await coordinator._async_fast_tick(None)
    assert coordinator.data.commands["marstek_m2"].mode == "charge"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_keeps_planning_on_last_known_prices_when_zonneplan_drops(hass: HomeAssistant) -> None:
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"batteries": "marstek_m1, marstek_m2"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator._plan is not None
    assert coordinator.data.price_source == "zonneplan"

    # Internet weg: Zonneplan wordt 'unavailable' zonder forecast, het plan blijft.
    hass.states.async_set(DEFAULTS["price_sensor"], "unavailable", {})
    await coordinator.async_replan("test")
    assert coordinator._plan is not None
    assert coordinator.data.price_source.startswith("laatst bekend")

    # Ook na een herstart tijdens de storing: de prijzen komen uit de opslag.
    payload = coordinator._storage_payload()
    assert _prices_from_store(payload["prices"]) == coordinator._last_prices
    assert _prices_from_store([["kapot"]]) == []
    assert await hass.config_entries.async_unload(entry.entry_id)
