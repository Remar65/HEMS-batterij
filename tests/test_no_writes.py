"""Borging: alleen actuator.py mag iets aansturen.

Deze test faalt zodra er buiten actuator.py code bijkomt die services aanroept of
states zet. Of er echt gestuurd wordt, hangt af van de opties en de schakelaar
"HEMS stuurt" (zie test_init.py).
"""

from pathlib import Path
import re

PACKAGE = Path(__file__).parent.parent / "custom_components" / "hems_batterij"
ALLOWED = {"actuator.py"}
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


def test_control_is_off_by_default():
    from custom_components.hems_batterij.const import CONF_CONTROLLED, DEFAULTS

    assert DEFAULTS[CONF_CONTROLLED] == []
