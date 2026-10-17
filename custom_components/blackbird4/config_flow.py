"""Config flow for the Monoprice Blackbird 4x4 integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
import voluptuous as vol

from .api import (
    PROTOCOL_HTTP,
    PROTOCOL_JSON,
    PROTOCOL_TCP,
    PROTOCOL_TCP_BANG,
    PROTOCOL_WEB_HEX,
    PROTOCOL_WEB_TERMINAL,
    BlackbirdConnectionError,
    BlackbirdError,
    BlackbirdMatrix,
    async_probe,
)
from .const import (
    CONF_PROTOCOL,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INPUTS,
    OUTPUTS,
    TCP_BANG_PORTS,
    TCP_FALLBACK_PORTS,
    input_key,
    output_key,
)

_LOGGER = logging.getLogger(__name__)


class CannotConnect(Exception):
    """Nothing answered at the address."""


class UnknownProtocol(Exception):
    """Something answered but didn't speak a protocol we know."""


async def async_detect(host: str, port: int) -> dict[str, Any]:
    """Find a working protocol for the matrix and return entry data."""
    answered = False
    candidates = [
        (PROTOCOL_HTTP, port),
        (PROTOCOL_WEB_HEX, port),
        (PROTOCOL_WEB_TERMINAL, port),
        (PROTOCOL_JSON, port),
    ]
    tcp_ports = list(TCP_FALLBACK_PORTS)
    if port != DEFAULT_PORT:
        # A non-default port may be the raw TCP control port.
        tcp_ports.insert(0, port)
    candidates += [(PROTOCOL_TCP, tcp_port) for tcp_port in dict.fromkeys(tcp_ports)]
    candidates += [(PROTOCOL_TCP_BANG, tcp_port) for tcp_port in TCP_BANG_PORTS]
    for protocol, candidate_port in candidates:
        client = BlackbirdMatrix(host, candidate_port, protocol, timeout=3)
        try:
            await client.async_get_state()
        except BlackbirdConnectionError:
            continue
        except BlackbirdError as err:
            answered = True
            _LOGGER.debug(
                "%s on port %s replied but: %s", protocol, candidate_port, err
            )
            continue
        return {CONF_HOST: host, CONF_PORT: candidate_port, CONF_PROTOCOL: protocol}
    raise UnknownProtocol if answered else CannotConnect


def _user_schema(host: str = "", port: int = DEFAULT_PORT) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=host): str,
            vol.Required(CONF_PORT, default=port): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
        }
    )


def _names_schema(options: Mapping[str, Any]) -> dict[Any, Any]:
    """Return optional name fields for every input and output."""
    schema: dict[Any, Any] = {}
    for key in [input_key(n) for n in INPUTS] + [output_key(n) for n in OUTPUTS]:
        schema[vol.Optional(key, description={"suggested_value": options.get(key)})] = (
            str
        )
    return schema


def _non_blank(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Drop blank names so the defaults ("Input 1", ...) apply."""
    return {
        key: value.strip() if isinstance(value, str) else value
        for key, value in user_input.items()
        if not (isinstance(value, str) and not value.strip())
    }


class BlackbirdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Blackbird matrix."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._details = ""
        self._data: dict[str, Any] = {}

    async def _async_validate(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> dict[str, Any] | None:
        try:
            return await async_detect(user_input[CONF_HOST], user_input[CONF_PORT])
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except UnknownProtocol:
            errors["base"] = "unknown_protocol"
            await self._async_probe(user_input[CONF_HOST], user_input[CONF_PORT])
        except Exception:
            _LOGGER.exception("Unexpected error detecting Blackbird matrix")
            errors["base"] = "unknown"
        return None

    async def _async_probe(self, host: str, port: int) -> None:
        """Show what the device serves so unsupported firmware can be added."""
        try:
            report = await async_probe(host, port)
        except Exception:
            _LOGGER.exception("Probe of %s:%s failed", host, port)
            return
        _LOGGER.warning("Blackbird probe of %s:%s:\n%s", host, port, report)
        self._details = (
            "\n\n**What the device replied** (copy this into a GitHub issue):"
            f"\n```\n{report[:6000]}\n```"
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the matrix's address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            # Don't let an earlier, abandoned attempt for this address block
            # a retry; the entry's unique ID still prevents duplicates.
            await self.async_set_unique_id(
                user_input[CONF_HOST], raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            if data := await self._async_validate(user_input, errors):
                self._data = data
                return await self.async_step_names()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_user_schema(), user_input),
            errors=errors,
            description_placeholders={"details": self._details},
        )

    async def async_step_names(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally name the inputs and outputs; blank keeps the defaults."""
        if user_input is not None:
            return self.async_create_entry(
                title="Blackbird 4x4", data=self._data, options=_non_blank(user_input)
            )
        return self.async_show_form(
            step_id="names", data_schema=vol.Schema(_names_schema({}))
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the matrix's address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            if user_input[CONF_HOST] != entry.data[CONF_HOST]:
                self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            if data := await self._async_validate(user_input, errors):
                return self.async_update_reload_and_abort(
                    entry, unique_id=data[CONF_HOST], data_updates=data
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(entry.data[CONF_HOST], entry.data[CONF_PORT]),
            errors=errors,
            description_placeholders={"details": self._details},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return BlackbirdOptionsFlow()


class BlackbirdOptionsFlow(OptionsFlow):
    """Names for inputs/outputs and the polling interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=_non_blank(user_input))
        options = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=2, max=3600)),
            **_names_schema(options),
        }
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
