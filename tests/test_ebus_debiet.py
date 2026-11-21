"""Debiet verwarmingswater uit ebusd-berichten."""

from custom_components.hems_batterij.ebus_debiet import parse_flow
from custom_components.hems_batterij.meetlogger import HEADER


def test_parse_flow_json_and_plain():
    assert parse_flow('{"value": {"value": 890}}') == 890.0
    assert parse_flow(b'{"0": {"name": "", "value": 875}}') == 875.0
    assert parse_flow("880") == 880.0


def test_parse_flow_rejects_garbage():
    assert parse_flow("") is None
    assert parse_flow("abc") is None
    assert parse_flow('{"value": null}') is None
    assert parse_flow('{"a": 1, "b": 2}') is None
    assert parse_flow("-5") is None
    assert parse_flow("99999") is None


def test_meetlogger_logs_flow_and_pump_at_the_end():
    assert HEADER[-2:] == ("debiet_lh", "pomp_pct")
