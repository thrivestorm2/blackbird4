"""The Monoprice Blackbird 4x4 HDMI matrix integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import PROTOCOL_HTTP, BlackbirdMatrix
from .const import CONF_PROTOCOL, DOMAIN
from .coordinator import BlackbirdConfigEntry, BlackbirdCoordinator
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BlackbirdConfigEntry) -> bool:
    """Set up a Blackbird matrix from a config entry."""
    client = BlackbirdMatrix(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data.get(CONF_PROTOCOL, PROTOCOL_HTTP),
    )
    coordinator = BlackbirdCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    if coordinator.connections is not None:
        # Connection status is a nice-to-have: don't block setup on it.
        await coordinator.connections.async_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BlackbirdConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(
    hass: HomeAssistant, entry: BlackbirdConfigEntry
) -> None:
    """Reload so new names and the scan interval take effect."""
    await hass.config_entries.async_reload(entry.entry_id)
