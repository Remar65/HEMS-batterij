from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hems_batterij.const import DEFAULTS, DOMAIN

from .test_init import _set_states


async def test_flow_creates_entry(hass: HomeAssistant) -> None:
    _set_states(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    user_input = {
        k: DEFAULTS[k]
        for k in (
            "p1_power",
            "pv_power",
            "pv_inverted",
            "price_sensor",
            "solcast_today",
            "solcast_tomorrow",
            "batteries",
        )
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_flow_rejects_unknown_battery(hass: HomeAssistant) -> None:
    _set_states(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    user_input = {
        k: DEFAULTS[k]
        for k in (
            "p1_power",
            "pv_power",
            "pv_inverted",
            "price_sensor",
            "solcast_today",
            "solcast_tomorrow",
        )
    }
    user_input["batteries"] = "marstek_m9"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"batteries": "battery_not_found"}
