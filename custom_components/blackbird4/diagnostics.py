"""Diagnostics for the Blackbird matrix."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .api import async_web_ui_command_code
from .coordinator import BlackbirdConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BlackbirdConfigEntry
) -> dict[str, Any]:
    """Return diagnostics, including the raw status reply, for bug reports."""
    coordinator = entry.runtime_data
    data = coordinator.data
    connections = coordinator.connections.data if coordinator.connections else None
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "protocol": coordinator.client.protocol,
        "last_update_success": coordinator.last_update_success,
        "routes": data.outputs if data else None,
        "raw_status": data.raw if data else None,
        "connections": (
            {
                "inputs": connections.inputs,
                "outputs": connections.outputs,
                "status_bytes": connections.raw,
            }
            if connections
            else None
        ),
        # The web UI's own command code, to add functions such as power.
        "web_ui_command_code": await async_web_ui_command_code(
            coordinator.client.host, coordinator.client.port
        ),
    }
