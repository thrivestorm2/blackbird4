"""Diagnostics for the Blackbird matrix."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import BlackbirdConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BlackbirdConfigEntry
) -> dict[str, Any]:
    """Return diagnostics, including the raw status reply, for bug reports."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "protocol": coordinator.client.protocol,
        "last_update_success": coordinator.last_update_success,
        "routes": data.outputs if data else None,
        "raw_status": data.raw if data else None,
    }
