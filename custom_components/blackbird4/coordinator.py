"""Polling coordinator for the Blackbird matrix."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BlackbirdError, BlackbirdMatrix, MatrixState
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INPUTS,
    OUTPUTS,
    input_key,
    output_key,
)

_LOGGER = logging.getLogger(__name__)

type BlackbirdConfigEntry = ConfigEntry[BlackbirdCoordinator]


class BlackbirdCoordinator(DataUpdateCoordinator[MatrixState]):
    """Fetch routing from the matrix and send switch commands."""

    config_entry: BlackbirdConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BlackbirdConfigEntry,
        client: BlackbirdMatrix,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client

    async def _async_update_data(self) -> MatrixState:
        try:
            return await self.client.async_get_state()
        except BlackbirdError as err:
            raise UpdateFailed(f"Error talking to Blackbird matrix: {err}") from err

    def input_name(self, number: int) -> str:
        """Return the display name of an input."""
        return self.config_entry.options.get(input_key(number)) or f"Input {number}"

    def output_name(self, number: int) -> str:
        """Return the display name of an output."""
        return self.config_entry.options.get(output_key(number)) or f"Output {number}"

    @property
    def input_names(self) -> list[str]:
        """Return all input display names, in input order."""
        return [self.input_name(number) for number in INPUTS]

    def input_number(self, name: str) -> int:
        """Return the input number for a display name."""
        for number in INPUTS:
            if self.input_name(number) == name:
                return number
        raise HomeAssistantError(f"Unknown input: {name}")

    async def async_set_output(self, output: int, input_: int) -> None:
        """Route an input to an output."""
        try:
            await self.client.async_set_output(output, input_)
        except BlackbirdError as err:
            raise HomeAssistantError(
                f"Failed to switch output {output}: {err}"
            ) from err
        await self._async_apply_optimistic({output: input_})

    async def async_set_all(self, input_: int) -> None:
        """Route an input to every output."""
        try:
            await self.client.async_set_all(input_, OUTPUTS)
        except BlackbirdError as err:
            raise HomeAssistantError(f"Failed to switch outputs: {err}") from err
        await self._async_apply_optimistic(dict.fromkeys(OUTPUTS, input_))

    async def _async_apply_optimistic(self, routes: dict[int, int]) -> None:
        # Show the new route right away. Polling immediately can read the old
        # route back while the matrix is still switching, so leave confirmation
        # to the next scheduled poll (async_set_updated_data reschedules it).
        if self.data is not None:
            self.async_set_updated_data(
                replace(self.data, outputs={**self.data.outputs, **routes})
            )
