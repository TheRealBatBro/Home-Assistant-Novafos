"""The Novafos integration: water (and district heating) data from KMD Easy-Energy."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import NovafosAuthError, clean_token, token_expiry
from .const import CONF_ACCESS_TOKEN, DOMAIN
from .coordinator import NovafosConfigEntry, NovafosCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_UPDATE_TOKEN = "update_token"
UPDATE_TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): cv.string,
        # Sent by kpoppel's Chrome extension; ignored (the token carries its own expiry).
        vol.Optional("access_token_date_updated"): cv.string,
        vol.Optional("config_entry_id"): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def update_token(call: ServiceCall) -> None:
        try:
            token = clean_token(call.data[CONF_ACCESS_TOKEN])
            token_expiry(token)
        except NovafosAuthError as err:
            raise ServiceValidationError(f"Invalid access token: {err}") from err
        entries = [
            e
            for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if call.data.get("config_entry_id") in (None, e.entry_id)
        ]
        if not entries:
            raise ServiceValidationError("No loaded Novafos entry to update")
        for entry in entries:
            # Stored in options so it survives a restart; the update listener refreshes.
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_ACCESS_TOKEN: token}
            )

    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_TOKEN, update_token, schema=UPDATE_TOKEN_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: NovafosConfigEntry) -> bool:
    coordinator = NovafosCoordinator(hass, entry)
    await coordinator.async_load_statistics()
    # Never fails on an expired token: the sensors keep their restored values.
    await coordinator.async_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: NovafosConfigEntry) -> None:
    coordinator = entry.runtime_data
    if coordinator.set_token(entry.options.get(CONF_ACCESS_TOKEN, "")):
        await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: NovafosConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

