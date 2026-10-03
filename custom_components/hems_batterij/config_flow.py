"""Instellen via de UI."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_BATTERIES,
    CONF_DEFAULT_EFFICIENCY,
    CONF_DEFAULT_LOAD_W,
    CONF_FEED_IN_COST_CT,
    CONF_INVESTMENT_EUR,
    CONF_NETTING_ACTIVE,
    CONF_NETTING_END,
    CONF_P1_POWER,
    CONF_PRICE_SENSOR,
    CONF_PV_INVERTED,
    CONF_PV_POWER,
    CONF_SOLAR_FIRST_CT,
    CONF_SOLCAST_FIELD,
    CONF_SOLCAST_TODAY,
    CONF_SOLCAST_TOMORROW,
    CONF_TRADE_MARGIN_CT,
    CONF_WEAR_CT,
    CONF_WEAR_ENTITY,
    DEFAULTS,
    DOMAIN,
    NAME,
)

_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_P1_POWER, default=defaults[CONF_P1_POWER]): _SENSOR,
            vol.Required(CONF_PV_POWER, default=defaults[CONF_PV_POWER]): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=True)
            ),
            vol.Required(CONF_PV_INVERTED, default=defaults[CONF_PV_INVERTED]): bool,
            vol.Required(CONF_PRICE_SENSOR, default=defaults[CONF_PRICE_SENSOR]): _SENSOR,
            vol.Required(CONF_SOLCAST_TODAY, default=defaults[CONF_SOLCAST_TODAY]): _SENSOR,
            vol.Required(CONF_SOLCAST_TOMORROW, default=defaults[CONF_SOLCAST_TOMORROW]): _SENSOR,
            vol.Required(CONF_BATTERIES, default=defaults[CONF_BATTERIES]): str,
        }
    )


class HemsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            prefixes = [p.strip() for p in user_input[CONF_BATTERIES].split(",") if p.strip()]
            missing = [
                p for p in prefixes if self.hass.states.get(f"sensor.{p}_battery_state_of_charge") is None
            ]
            if not prefixes:
                errors[CONF_BATTERIES] = "no_batteries"
            elif missing:
                errors[CONF_BATTERIES] = "battery_not_found"
            else:
                return self.async_create_entry(title=NAME, data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema({**DEFAULTS, **(user_input or {})}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HemsOptionsFlow()


class HemsOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**DEFAULTS, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Optional(CONF_WEAR_ENTITY, default=current[CONF_WEAR_ENTITY]): str,
                vol.Required(CONF_WEAR_CT, default=current[CONF_WEAR_CT]): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=50)
                ),
                vol.Required(CONF_DEFAULT_EFFICIENCY, default=current[CONF_DEFAULT_EFFICIENCY]): vol.All(
                    vol.Coerce(float), vol.Range(min=0.5, max=1.0)
                ),
                vol.Required(CONF_NETTING_ACTIVE, default=current[CONF_NETTING_ACTIVE]): bool,
                vol.Required(CONF_NETTING_END, default=current[CONF_NETTING_END]): str,
                vol.Required(CONF_FEED_IN_COST_CT, default=current[CONF_FEED_IN_COST_CT]): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=50)
                ),
                vol.Required(CONF_DEFAULT_LOAD_W, default=current[CONF_DEFAULT_LOAD_W]): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=10000)
                ),
                vol.Required(CONF_SOLAR_FIRST_CT, default=current[CONF_SOLAR_FIRST_CT]): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=50)
                ),
                vol.Required(CONF_TRADE_MARGIN_CT, default=current[CONF_TRADE_MARGIN_CT]): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=50)
                ),
                vol.Required(CONF_INVESTMENT_EUR, default=current[CONF_INVESTMENT_EUR]): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=100000)
                ),
                vol.Required(CONF_SOLCAST_FIELD, default=current[CONF_SOLCAST_FIELD]): vol.In(
                    ["pv_estimate", "pv_estimate10", "pv_estimate90"]
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
