"""Switches: matrix power and the front-panel beep."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import STATUS_BEEP
from .coordinator import (
    BlackbirdConfigEntry,
    BlackbirdCoordinator,
    ConnectionCoordinator,
)
from .entity import device_info

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlackbirdConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up power and beep switches when the matrix supports them."""
    coordinator = entry.runtime_data
    if not coordinator.client.supports_power:
        return
    entities: list[SwitchEntity] = [BlackbirdPowerSwitch(coordinator)]
    if coordinator.connections is not None:
        entities.append(BlackbirdBeepSwitch(coordinator))
    async_add_entities(entities)


class BlackbirdPowerSwitch(CoordinatorEntity[BlackbirdCoordinator], SwitchEntity):
    """Power for the whole matrix."""

    _attr_has_entity_name = True
    _attr_name = "Power"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: BlackbirdCoordinator) -> None:
        """Initialise the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_power"
        self._attr_device_info = device_info(coordinator)

    @property
    def available(self) -> bool:
        """Available while the matrix reports its power state."""
        return super().available and self.coordinator.data.power is not None

    @property
    def is_on(self) -> bool | None:
        """Return True if the matrix is on."""
        return self.coordinator.data.power

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the matrix on."""
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the matrix off."""
        await self.coordinator.async_set_power(False)


class BlackbirdBeepSwitch(CoordinatorEntity[ConnectionCoordinator], SwitchEntity):
    """The front-panel beep. Polled only while enabled."""

    _attr_has_entity_name = True
    _attr_name = "Beep"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BlackbirdCoordinator) -> None:
        """Initialise the switch."""
        assert coordinator.connections is not None
        super().__init__(coordinator.connections)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_beep"
        self._attr_device_info = device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Start polling the beep setting; disabled switches are never added."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_want(*STATUS_BEEP))
        await self.coordinator.async_request_refresh()

    @property
    def available(self) -> bool:
        """Available while the matrix reports the beep setting."""
        data = self.coordinator.data
        return super().available and data is not None and data.beep is not None

    @property
    def is_on(self) -> bool | None:
        """Return True if the beep is on."""
        return self.coordinator.data.beep if self.coordinator.data else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the beep on."""
        await self.coordinator.async_set_beep(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the beep off."""
        await self.coordinator.async_set_beep(False)
