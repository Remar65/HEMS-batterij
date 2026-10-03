from custom_components.hems_batterij.controller import (
    BatteryState,
    ControllerSettings,
    ControlTarget,
    allocate,
    control_step,
)
from custom_components.hems_batterij.planner import (
    MODE_GRID_CHARGE,
    MODE_HOLD,
    MODE_SAFETY,
    MODE_SELF_CONSUMPTION,
)

SETTINGS = ControllerSettings(smoothing=0.0)


def bat(name, soc, power=0.0, eff=0.8, **kw):
    return BatteryState(
        name=name,
        soc_pct=soc,
        capacity_kwh=5.12,
        ac_power_w=power,
        max_charge_w=2500,
        max_discharge_w=2500,
        soc_min_pct=12,
        soc_max_pct=100,
        efficiency=eff,
        **kw,
    )


SELF = ControlTarget(MODE_SELF_CONSUMPTION, max_charge_w=5000, max_discharge_w=5000)


def test_self_consumption_brings_p1_to_zero():
    # Huis neemt 600 W af uit het net, batterijen staan stil: 600 W ontladen.
    result = control_step(SELF, 600, [bat("m1", 50), bat("m2", 50)], None, SETTINGS)
    assert result.total_w == 600
    # Klein vermogen: maar één batterij, bij gelijke stand die met het beste rendement.
    assert sorted(result.per_battery_w.values()) == [0, 600]


def test_small_power_goes_to_fullest_battery():
    split = allocate(500, [bat("m1", 80), bat("m2", 40)], SETTINGS)
    assert split == {"m1": 500, "m2": 0}
    split = allocate(-500, [bat("m1", 80), bat("m2", 40)], SETTINGS)
    assert split == {"m1": 0, "m2": -500}


def test_large_power_split_and_capped():
    split = allocate(4000, [bat("m1", 90), bat("m2", 50)], SETTINGS)
    assert sum(split.values()) == 4000
    assert split["m1"] > split["m2"]
    assert max(split.values()) <= 2500
    split = allocate(6000, [bat("m1", 90), bat("m2", 50)], SETTINGS)
    assert split == {"m1": 2500, "m2": 2500}


def test_empty_battery_not_discharged():
    split = allocate(1000, [bat("m1", 12), bat("m2", 60)], SETTINGS)
    assert split["m1"] == 0 and split["m2"] == 1000


def test_hold_blocks_discharge_but_allows_pv_charge():
    hold = ControlTarget(MODE_HOLD, max_charge_w=5000, max_discharge_w=0)
    assert control_step(hold, 800, [bat("m1", 70), bat("m2", 70)], None, SETTINGS).total_w == 0
    assert control_step(hold, -900, [bat("m1", 70), bat("m2", 70)], None, SETTINGS).total_w == -900


def test_grid_charge_fixed_power():
    target = ControlTarget(MODE_GRID_CHARGE, fixed_w=-3000, max_charge_w=3000)
    result = control_step(target, 200, [bat("m1", 30), bat("m2", 40)], None, SETTINGS)
    assert result.total_w == -3000
    assert result.per_battery_w["m1"] < result.per_battery_w["m2"] < 0  # leegste krijgt meer


def test_safety_when_p1_missing():
    result = control_step(SELF, None, [bat("m1", 50), bat("m2", 50)], None, SETTINGS)
    assert result.mode == MODE_SAFETY
    assert result.total_w == 0
    assert any("P1" in r for r in result.safety_reasons)


def test_alarm_excludes_battery():
    batteries = [bat("m1", 50, alarms=["BMS-beveiliging actief"]), bat("m2", 50)]
    result = control_step(SELF, 3000, batteries, None, SETTINGS)
    assert result.per_battery_w["m1"] == 0
    assert result.per_battery_w["m2"] == 2500
    assert result.safety_reasons


def test_stale_battery_data_all_batteries_means_safety():
    batteries = [bat("m1", 50, available=False), bat("m2", 50, available=False)]
    assert control_step(SELF, 500, batteries, None, SETTINGS).mode == MODE_SAFETY


def test_no_plan_falls_back_to_self_consumption():
    result = control_step(None, 400, [bat("m1", 50), bat("m2", 50)], None, SETTINGS)
    assert result.mode == MODE_SELF_CONSUMPTION
    assert result.total_w == 400


def test_tracks_existing_battery_power():
    # Batterijen leveren al 1000 W, P1 is nog 200 W afname: totaal naar 1200 W.
    result = control_step(SELF, 200, [bat("m1", 50, 500), bat("m2", 50, 500)], None, SETTINGS)
    assert result.total_w == 1200
