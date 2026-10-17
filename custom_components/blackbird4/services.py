"""Services for the Blackbird matrix."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .api import BlackbirdError
from .const import (
    ATTR_COMMAND,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_INPUT,
    DOMAIN,
    NUM_INPUTS,
    SERVICE_SEND_COMMAND,
    SERVICE_SET_ALL_OUTPUTS,
)
from .coordinator import BlackbirdCoordinator

SET_ALL_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_INPUT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=NUM_INPUTS)
        ),
    }
)
SEND_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_COMMAND): cv.string,
    }
)


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> BlackbirdCoordinator:
    """Return the coordinator for the call's entry (or the only loaded one)."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(
                f"No loaded Blackbird matrix with id {entry_id}"
            )
    elif len(entries) != 1:
        raise ServiceValidationError(
            "Specify config_entry_id: there is not exactly one Blackbird matrix loaded"
        )
    return entries[0].runtime_data


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services."""

    async def set_all_outputs(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_set_all(call.data[ATTR_INPUT])

    async def send_command(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        try:
            reply = await coordinator.client.async_send_raw(call.data[ATTR_COMMAND])
        except BlackbirdError as err:
            raise HomeAssistantError(f"Command failed: {err}") from err
        await coordinator.async_request_refresh()
        return {"reply": reply}

    hass.services.async_register(
        DOMAIN, SERVICE_SET_ALL_OUTPUTS, set_all_outputs, schema=SET_ALL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        send_command,
        schema=SEND_COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
