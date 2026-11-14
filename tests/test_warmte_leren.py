"""Zelflerend warmtemodel: leren uit synthetische metingen met een bekende uitkomst."""

from datetime import UTC, datetime, timedelta
import random

from custom_components.hems_batterij.warmte_leren import (
    TANK_ROOM_TEMP,
    HeatLearner,
    HourAccumulator,
    HourSample,
    Readings,
    fit_cop,
    fit_room,
    fit_tank,
    voorstellen,
)

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def test_accumulator_splits_heating_and_hot_water_per_hour() -> None:
    acc = HourAccumulator()
    done = None
    for i in range(721):  # 1 uur à 5 s, plus de eerste meting van het volgende uur
        now = T0 + timedelta(seconds=5 * i)
        hwc = i >= 360  # tweede half uur warm water
        r = Readings(
            t_out=5.0,
            room=20.0 + i / 720,
            vrc_room=21.5 if i == 100 else 20.0,
            kitchen=20.0,
            heat_w=0.0 if hwc else 800.0,
            dhw_w=1200.0 if hwc else 0.0,
            yield_kw=3.0,
            status="hwc_compressor_active" if hwc else "heat_compressor_active",
            pv_w=0.0,
            tank=45.0,
        )
        done = acc.add(now, 5.0 if i else 0.0, r) or done
    assert done is not None
    assert done.start == T0
    assert abs(done.heat_elec_kwh - 0.4) < 0.01
    assert abs(done.dhw_elec_kwh - 0.6) < 0.01
    assert abs(done.heat_yield_kwh - 1.5) < 0.01
    assert abs(done.dhw_yield_kwh - 1.5) < 0.01
    assert done.dhw_active and done.stove
    assert done.room_start == 20.0 and done.room_end > 20.9
    assert done.t_out == 5.0


def test_short_hour_is_dropped() -> None:
    acc = HourAccumulator()
    acc.add(T0 + timedelta(minutes=50), 0.0, Readings(room=20.0))
    acc.add(T0 + timedelta(minutes=55), 300.0, Readings(room=20.0))
    assert acc.add(T0 + timedelta(hours=1), 5.0, Readings(room=20.0)) is None


def _cop_samples(a: float, b: float) -> list[HourSample]:
    samples = []
    for i in range(60):
        t = (i % 11) * 1.0  # 0..10 °C
        elec = 0.6
        samples.append(
            HourSample(
                start=T0 + timedelta(hours=i),
                seconds=3600,
                t_out=t,
                heat_elec_kwh=elec,
                heat_yield_kwh=elec * (a + b * t),
            )
        )
    return samples


def test_cop_line_is_learned() -> None:
    fit = fit_cop(_cop_samples(3.0, 0.12), "verwarming")
    assert fit is not None
    assert abs(fit.a - 3.0) < 0.01 and abs(fit.b - 0.12) < 0.005
    assert fit_cop(_cop_samples(3.0, 0.12), "warm_water") is None


def test_cop_needs_enough_data() -> None:
    assert fit_cop(_cop_samples(3.0, 0.1)[:5], "verwarming") is None


def _room_samples(c=(0.15, 0.10, 0.05), loss=0.02, hours=24 * 8, stove_every=0) -> list[HourSample]:
    rng = random.Random(1)
    samples = []
    t_in = 20.0
    q = [0.0, 0.0, 0.0]
    for i in range(hours):
        t_out = 5.0 + 5.0 * ((i % 24) / 24)
        q = [rng.choice([0.0, 0.0, 2.5, 3.0]), q[0], q[1]]
        pv = 1.0 if 11 <= i % 24 < 15 else 0.0
        stove = bool(stove_every and i % stove_every == 0)
        rise = sum(ci * qi for ci, qi in zip(c, q, strict=True)) + 0.1 * pv - loss * (t_in - t_out) + 0.02
        if stove:
            rise += 1.0  # kachel: onverklaarbare sprong, mag niet meetellen
        samples.append(
            HourSample(
                start=T0 + timedelta(hours=i),
                seconds=3600,
                t_out=t_out,
                room_start=t_in,
                room_end=t_in + rise,
                heat_yield_kwh=q[0],
                pv_kwh=pv,
                stove=stove,
            )
        )
        t_in += rise
    return samples


def test_room_model_finds_loss_and_afterglow() -> None:
    fit = fit_room(_room_samples(stove_every=17))
    assert fit is not None
    # 1 / (0,15 + 0,10 + 0,05) = 3,33 kWh per graad; verlies 0,02 x 3,33 = 67 W/K
    assert abs(fit.kwh_per_degree - 3.33) < 0.1
    assert abs(fit.loss_w_per_k - 66.7) < 3
    assert abs(fit.afterglow_share - 0.5) < 0.03
    assert fit.r2 > 0.95
    # Na een uur laden met 3 kWh: 0,10 x 3 + 2 x 0,05 x 3 = 0,6 graad naloop.
    assert abs(fit.afterglow_degrees(3.0) - 0.6) < 0.05


def test_room_model_needs_enough_hours() -> None:
    assert fit_room(_room_samples(hours=50)) is None


def _tank_samples(rate=0.01, days=3) -> list[HourSample]:
    samples = []
    tank = 50.0
    for i in range(24 * days):
        start = T0 + timedelta(hours=i)
        s = HourSample(start=start, seconds=3600, tank_start=tank)
        if start.hour == 7:  # douchen
            s.tank_min = tank - 8
            tank -= 8
        elif start.hour == 2:  # laden
            s.dhw_active = True
            s.dhw_elec_kwh = 2.0
            tank_new = 50.0
            s.tank_min = tank
            tank = tank_new
        else:
            tank -= rate * (tank - TANK_ROOM_TEMP)
            s.tank_min = tank
        s.tank_end = tank
        samples.append(s)
    return samples


def test_tank_loss_and_taps() -> None:
    fit = fit_tank(_tank_samples())
    assert fit.loss_rate is not None
    assert abs(fit.loss_at(50) - 0.29) < 0.02
    assert fit.taps_per_hour[7] == 1.0
    assert sum(fit.taps_per_hour) == 1.0


def test_proposals_compare_with_current_settings() -> None:
    cop = fit_cop(_cop_samples(2.6, 0.05), "verwarming")
    room = fit_room(_room_samples())
    tank = fit_tank(_tank_samples(rate=0.02))
    result = voorstellen(cop, None, room, tank, 3.0, 10, {"voorsprong": 0.3})
    ids = {p["id"] for p in result}
    assert ids == {"cop_vloerbuffer", "vat_afkoeling", "voorsprong_naverwarming"}
    lead = next(p for p in result if p["id"] == "voorsprong_naverwarming")
    assert lead["voorstel"] == "0,6 °C"
    # Komt het geleerde overeen met wat er staat, dan geen voorstel.
    assert voorstellen(None, None, room, fit_tank([]), 3.0, 10, {"voorsprong": 0.6}) == []


def test_learner_round_trip_and_summary() -> None:
    learner = HeatLearner()
    learner.samples = _room_samples(hours=30)
    restored = HeatLearner.from_dict(learner.to_dict())
    assert len(restored.samples) == 30
    assert restored.samples[5].room_end == round(learner.samples[5].room_end, 2)
    info = restored.summary({"voorsprong": 0.5})
    assert info["uren_gemeten"] == 30
    assert info["huis"] == "nog aan het leren"
    assert info["voorstellen"] == []


def test_learner_keeps_only_recent_hours() -> None:
    learner = HeatLearner()
    learner.samples = [HourSample(start=T0 - timedelta(days=60), seconds=3600)]
    for i in range(721):
        learner.add(T0 + timedelta(seconds=5 * i), 5.0 if i else 0.0, Readings(room=20.0))
    assert [s.start for s in learner.samples] == [T0]
