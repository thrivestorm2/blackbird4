"""Select entities: one dropdown per matrix output choosing its input."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
    """Set up a source select for each output."""
    coordinator = entry.runtime_data
    async_add_entities(BlackbirdOutputSelect(coordinator, output) for output in OUTPUTS)


class BlackbirdOutputSelect(BlackbirdOutputEntity, SelectEntity):
    """Choose which input feeds an output."""

    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, coordinator: BlackbirdCoordinator, output: int) -> None:
        """Initialise the select."""
        super().__init__(coordinator, output, "select")
        self._attr_name = f"{coordinator.output_name(output)} source"
        self._attr_options = coordinator.input_names

    @property
    def current_option(self) -> str | None:
        """Return the input routed to this output."""
        return self.current_input_name

    async def async_select_option(self, option: str) -> None:
        """Route an input to this output."""
        await self.coordinator.async_set_output(
            self._output, self.coordinator.input_number(option)
        )
