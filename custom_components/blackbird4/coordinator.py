"""Polling coordinator for the Blackbird matrix."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BlackbirdError, BlackbirdMatrix, ConnectionState, MatrixState
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

CONNECTION_SCAN_INTERVAL = timedelta(seconds=60)

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
        self.connections: ConnectionCoordinator | None = (
            ConnectionCoordinator(hass, entry, client)
            if client.supports_connections
            else None
        )

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

    def _ensure_powered(self) -> None:
        if self.data is not None and self.data.power is False:
            raise HomeAssistantError("The matrix is powered off")

    async def async_set_power(self, on: bool) -> None:
        """Turn the matrix on or off."""
        try:
            await self.client.async_set_power(on)
        except BlackbirdError as err:
            raise HomeAssistantError(
                f"Failed to turn the matrix {'on' if on else 'off'}: {err}"
            ) from err
        if self.data is not None:
            # Routes are unknown while off; the next poll fills them back in.
            self.async_set_updated_data(
                replace(self.data, power=on, outputs=self.data.outputs if on else {})
            )

    async def async_set_output(self, output: int, input_: int) -> None:
        """Route an input to an output."""
        self._ensure_powered()
        try:
            await self.client.async_set_output(output, input_)
        except BlackbirdError as err:
            raise HomeAssistantError(
                f"Failed to switch output {output}: {err}"
            ) from err
        await self._async_apply_optimistic({output: input_})

    async def async_set_all(self, input_: int) -> None:
        """Route an input to every output."""
        self._ensure_powered()
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


class ConnectionCoordinator(DataUpdateCoordinator[ConnectionState]):
    """Poll which ports have something plugged in, and the beep setting.

    Kept separate and slow: each port costs a round trip, and cables change
    far less often than routes. Only ports whose sensors are enabled are
    asked, and nothing is polled while every sensor is disabled.
    """

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
            name=f"{DOMAIN} connections",
            update_interval=CONNECTION_SCAN_INTERVAL,
            # Sensors register as they're added; gather them into one poll.
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=1, immediate=False
            ),
        )
        self.client = client
        self._wanted: set[tuple[str, int]] = set()

    @callback
    def async_want(self, kind: str, port: int) -> CALLBACK_TYPE:
        """Poll this port until the returned callback is called."""
        self._wanted.add((kind, port))

        @callback
        def _unwant() -> None:
            self._wanted.discard((kind, port))

        return _unwant

    async def async_set_beep(self, on: bool) -> None:
        """Turn the front-panel beep on or off."""
        try:
            await self.client.async_set_beep(on)
        except BlackbirdError as err:
            raise HomeAssistantError(f"Failed to set the beep: {err}") from err
        if self.data is not None:
            self.async_set_updated_data(replace(self.data, beep=on))

    async def _async_update_data(self) -> ConnectionState:
        if not self._wanted:
            return self.data or ConnectionState()
        try:
            return await self.client.async_get_connections(sorted(self._wanted))
        except BlackbirdError as err:
            raise UpdateFailed(f"Error reading connection status: {err}") from err
