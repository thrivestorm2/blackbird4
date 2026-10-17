"""Binary sensors: whether each input has a source and each output a display."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import CONNECTION_INPUT, CONNECTION_OUTPUT
from .const import INPUTS, OUTPUTS
from .coordinator import (
    BlackbirdConfigEntry,
    BlackbirdCoordinator,
    ConnectionCoordinator,
)
from .entity import device_info

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlackbirdConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up connection sensors when the matrix can report them."""
    coordinator = entry.runtime_data
    if coordinator.connections is None:
        return
    async_add_entities(
        [
            *(
                BlackbirdConnectionSensor(coordinator, CONNECTION_INPUT, port)
                for port in INPUTS
            ),
            *(
                BlackbirdConnectionSensor(coordinator, CONNECTION_OUTPUT, port)
                for port in OUTPUTS
            ),
        ]
    )


class BlackbirdConnectionSensor(
    CoordinatorEntity[ConnectionCoordinator], BinarySensorEntity
):
    """Whether a port has a cable with a live device on the other end."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PLUG
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BlackbirdCoordinator, kind: str, port: int) -> None:
        """Initialise the sensor."""
        assert coordinator.connections is not None
        super().__init__(coordinator.connections)
        self._kind = kind
        self._port = port
        entry = coordinator.config_entry
        name = (
            coordinator.input_name(port)
            if kind == CONNECTION_INPUT
            else coordinator.output_name(port)
        )
        self._attr_name = f"{name} connected"
        self._attr_unique_id = f"{entry.entry_id}_{kind}_{port}_connected"
        self._attr_device_info = device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Start polling this port; disabled sensors are never added."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_want(self._kind, self._port))
        await self.coordinator.async_request_refresh()

    @property
    def _ports(self) -> dict[int, bool]:
        data = self.coordinator.data
        if data is None:
            return {}
        return data.inputs if self._kind == CONNECTION_INPUT else data.outputs

    @property
    def available(self) -> bool:
        """Available while the matrix reports this port."""
        return super().available and self._port in self._ports

    @property
    def is_on(self) -> bool | None:
        """Return True if something is connected."""
        return self._ports.get(self._port)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw status byte so its meaning can be checked."""
        return {
            "status_byte": self.coordinator.data.raw.get(f"{self._kind}_{self._port}")
        }
