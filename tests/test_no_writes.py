"""Borging: alleen actuator.py (batterijen) en vaillant_actuator.py (ruimte-invloed) sturen.

Deze test faalt zodra er buiten actuator.py code bijkomt die services aanroept of
states zet. Of er echt gestuurd wordt, hangt af van de opties en de schakelaar
"HEMS stuurt" (zie test_init.py).
"""

from pathlib import Path
import re

PACKAGE = Path(__file__).parent.parent / "custom_components" / "hems_batterij"
ALLOWED = {"actuator.py", "vaillant_actuator.py"}
FORBIDDEN = [
    r"async_call\(",
    r"call_service",
    r"services\.call",
    r"states\.async_set",
    r"states\.set\(",
]


def test_no_service_calls_outside_actuator():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path.name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            if re.search(pattern, text):
                offenders.append(f"{path.name}: {pattern}")
    assert offenders == []


def test_actuator_only_touches_marstek_control_entities():
    text = (PACKAGE / "actuator.py").read_text(encoding="utf-8")
    # Alleen select.select_option en number.set_value, niets anders.
    assert set(re.findall(r'"(select|number)", "(\w+)"', text)) == {
        ("select", "select_option"),
        ("number", "set_value"),
    }
    assert "states.async_set" not in text


def test_vaillant_actuator_only_publishes_room_influence():
    text = (PACKAGE / "vaillant_actuator.py").read_text(encoding="utf-8")
    # Alleen mqtt.publish, en alleen naar het bericht van de kamerbijschakeling.
    assert set(re.findall(r'async_call\(\s*"(\w+)", "(\w+)"', text)) == {("mqtt", "publish")}
    assert set(re.findall(r'f"\{TOPIC\}/(\w+)"', text)) == {"get", "set"}
    assert 'TOPIC = "ebusd/basv2/Hc1RoomTempModulation"' in text
    assert "states.async_set" not in text


def test_room_influence_auto_is_off_by_default():
    from custom_components.hems_batterij.ruimte_invloed_ha import RoomInfluence

    assert RoomInfluence(None, None).auto_enabled is False  # type: ignore[arg-type]


def test_control_is_off_by_default():
    from custom_components.hems_batterij.const import CONF_CONTROLLED, DEFAULTS

    assert DEFAULTS[CONF_CONTROLLED] == []
