"""Base entity for the Blackbird matrix."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import BlackbirdCoordinator


def device_info(coordinator: BlackbirdCoordinator) -> DeviceInfo:
    """Return the matrix's device info, shared by every entity."""
    entry = coordinator.config_entry
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        name=entry.title,
        configuration_url=f"http://{coordinator.client.host}",
    )


class BlackbirdOutputEntity(CoordinatorEntity[BlackbirdCoordinator]):
    """An entity representing one output of the matrix."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: BlackbirdCoordinator, output: int, kind: str
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._output = output
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_output_{output}_{kind}"
        self._attr_device_info = device_info(coordinator)

    @property
    def available(self) -> bool:
        """Return True if the matrix reported a route for this output."""
        return (
            super().available
            and self.coordinator.data.input_for(self._output) is not None
        )

    @property
    def current_input_name(self) -> str | None:
        """Return the display name of the input routed to this output."""
        input_ = self.coordinator.data.input_for(self._output)
        if input_ is None:
            return None
        return self.coordinator.input_name(input_)
