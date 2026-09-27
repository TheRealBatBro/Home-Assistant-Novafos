"""Config flow for Novafos."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .api import NovafosAuthError, NovafosClient, NovafosError, clean_token
from .const import CONF_ACCESS_TOKEN, CONF_PRICE, CONF_PRICES, DEFAULT_NAME, DOMAIN, price_for_year

_LOGGER = logging.getLogger(__name__)

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(multiline=True))


async def _validate(hass: HomeAssistant, raw: str) -> tuple[NovafosClient, list, dict]:
    """Returns (client, meters, errors)."""
    client = NovafosClient(async_get_clientsession(hass))
    try:
        client.token = clean_token(raw)
        if client.token_expires is None:
            return client, [], {"base": "invalid_token"}
        if not client.token_valid():
            return client, [], {"base": "token_expired"}
        await client.async_login()
        meters = await client.async_get_meters()
    except NovafosAuthError:
        return client, [], {"base": "invalid_auth"}
    except NovafosError as err:
        _LOGGER.warning("Could not reach Novafos: %s", err)
        return client, [], {"base": "cannot_connect"}
    if not meters:
        return client, [], {"base": "no_meters"}
    return client, meters, {}


class NovafosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up with a name and a current access token."""

    VERSION = 1
    MINOR_VERSION = 0

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client, meters, errors = await _validate(self.hass, user_input[CONF_ACCESS_TOKEN])
            if not errors:
                await self.async_set_unique_id(client.customer_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{user_input[CONF_NAME]} ({client.address or client.customer_number})",
                    data={CONF_NAME: user_input[CONF_NAME], "meters": [asdict(m) for m in meters]},
                    options={CONF_ACCESS_TOKEN: client.token},
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_ACCESS_TOKEN): TOKEN_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return NovafosOptionsFlow()


class NovafosOptionsFlow(OptionsFlow):
    """Paste a new access token and/or set this year's price per m³."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        options = dict(self.config_entry.options)
        year = str(dt_util.now().year)
        if user_input is not None:
            if raw := (user_input.get(CONF_ACCESS_TOKEN) or "").strip():
                client, _, errors = await _validate(self.hass, raw)
                unique_id = self.config_entry.unique_id
                if not errors and unique_id and unique_id.isdigit() and unique_id != client.customer_id:
                    errors = {"base": "wrong_customer"}
                if not errors:
                    options[CONF_ACCESS_TOKEN] = client.token
            if not errors:
                prices = dict(options.get(CONF_PRICES) or {})
                if (price := user_input.get(CONF_PRICE)) is not None:
                    prices[year] = round(float(price), 4)
                else:
                    prices.pop(year, None)
                options[CONF_PRICES] = prices
                return self.async_create_entry(data=options)
        current = price_for_year(options.get(CONF_PRICES) or {}, int(year))
        schema = vol.Schema(
            {
                vol.Optional(CONF_ACCESS_TOKEN): TOKEN_SELECTOR,
                vol.Optional(
                    CONF_PRICE, description={"suggested_value": current}
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=1000, step=0.01, mode=NumberSelectorMode.BOX,
                        unit_of_measurement=f"{self.hass.config.currency}/m³",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"year": year},
        )
