"""Borging van de meekijkmodus: de integratie mag niets aansturen.

Deze test faalt zodra er code bijkomt die services aanroept of states zet.
Bij de latere stap naar echt aansturen moet deze test bewust worden aangepast.
"""

from pathlib import Path
import re

PACKAGE = Path(__file__).parent.parent / "custom_components" / "hems_batterij"
FORBIDDEN = [
    r"async_call\(",
    r"call_service",
    r"services\.call",
    r"states\.async_set",
    r"states\.set\(",
]


def test_no_service_calls_in_integration():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            if re.search(pattern, text):
                offenders.append(f"{path.name}: {pattern}")
    assert offenders == []


def test_shadow_mode_flag():
    from custom_components.hems_batterij.const import SHADOW_MODE

    assert SHADOW_MODE is True
