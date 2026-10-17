"""Media player entities: one per matrix output, with source selection."""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import OUTPUTS
from .coordinator import BlackbirdConfigEntry, BlackbirdCoordinator
from .entity import BlackbirdOutputEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlackbirdConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up media players for each output."""
    coordinator = entry.runtime_data
    async_add_entities(BlackbirdOutputPlayer(coordinator, output) for output in OUTPUTS)


class BlackbirdOutputPlayer(BlackbirdOutputEntity, MediaPlayerEntity):
    """A matrix output whose source is one of the matrix inputs."""

    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_supported_features = MediaPlayerEntityFeature.SELECT_SOURCE

    def __init__(self, coordinator: BlackbirdCoordinator, output: int) -> None:
        """Initialise the player."""
        super().__init__(coordinator, output, "media_player")
        self._attr_name = coordinator.output_name(output)
        self._attr_source_list = coordinator.input_names

    @property
    def available(self) -> bool:
        """Stay available while the matrix is off, to show it as off."""
        if (
            self.coordinator.last_update_success
            and self.coordinator.data.power is False
        ):
            return True
        return super().available

    @property
    def state(self) -> MediaPlayerState:
        """Off while the matrix is powered off, otherwise on."""
        if self.coordinator.data.power is False:
            return MediaPlayerState.OFF
        return MediaPlayerState.ON

    @property
    def source(self) -> str | None:
        """Return the input routed to this output."""
        return self.current_input_name

    async def async_select_source(self, source: str) -> None:
        """Route an input to this output."""
        await self.coordinator.async_set_output(
            self._output, self.coordinator.input_number(source)
        )
