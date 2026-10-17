"""Tests for the config and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blackbird4.api import (
    BlackbirdConnectionError,
    BlackbirdProtocolError,
    MatrixState,
)
from custom_components.blackbird4.const import DOMAIN

DETECT_CLIENT = "custom_components.blackbird4.config_flow.BlackbirdMatrix"


def _client_cls(behaviour):
    """Return a fake client class whose get_state depends on (protocol, port)."""

    def factory(host, port, protocol, timeout):
        client = AsyncMock()
        result = behaviour(protocol, port)
        if isinstance(result, Exception):
            client.async_get_state.side_effect = result
        else:
            client.async_get_state.return_value = result
        return client

    return factory


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_http(hass: HomeAssistant, mock_client) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    with patch(DETECT_CLIENT, _client_cls(lambda protocol, port: MatrixState({1: 1}))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": " 192.168.1.39 ", "port": 80}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "192.168.1.39", "port": 80, "protocol": "http"}
    assert result["result"].unique_id == "192.168.1.39"


async def test_user_flow_falls_back_to_tcp(hass: HomeAssistant, mock_client) -> None:
    def behaviour(protocol, port):
        if protocol == "tcp" and port == 4001:
            return MatrixState({1: 1})
        return BlackbirdProtocolError("nope")

    result = await _start(hass)
    with patch(DETECT_CLIENT, _client_cls(behaviour)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.39", "port": 80}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "192.168.1.39", "port": 4001, "protocol": "tcp"}


async def test_user_flow_errors(hass: HomeAssistant) -> None:
    result = await _start(hass)
    with patch(
        DETECT_CLIENT,
        _client_cls(lambda protocol, port: BlackbirdConnectionError("down")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.39", "port": 80}
        )
    assert result["errors"] == {"base": "cannot_connect"}

    with (
        patch(
            DETECT_CLIENT,
            _client_cls(
                lambda protocol, port: (
                    BlackbirdProtocolError("?")
                    if protocol == "http"
                    else BlackbirdConnectionError("down")
                )
            ),
        ),
        patch(
            "custom_components.blackbird4.config_flow.async_probe",
            return_value="GET /: HTTP/1.1 200 OK",
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.39", "port": 80}
        )
    assert result["errors"] == {"base": "unknown_protocol"}
    assert "GET /: HTTP/1.1 200 OK" in result["description_placeholders"]["details"]


async def test_duplicate_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.39", "port": 80}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    with patch(DETECT_CLIENT, _client_cls(lambda protocol, port: MatrixState({1: 1}))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.40", "port": 80}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data["host"] == "192.168.1.40"
    assert config_entry.unique_id == "192.168.1.40"


async def test_options_flow(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client
) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_interval": 30,
            "input_1": "Apple TV",
            "output_1": "Living Room",
            "input_2": "",
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        "scan_interval": 30,
        "input_1": "Apple TV",
        "output_1": "Living Room",
    }
    # Reloaded with the new names (entity IDs are kept).
    state = hass.states.get("media_player.blackbird_4x4_output_1")
    assert state.attributes["friendly_name"] == "Blackbird 4x4 Living Room"
    assert state.attributes["source"] == "Apple TV"
    assert state.attributes["source_list"] == [
        "Apple TV",
        "Input 2",
        "Input 3",
        "Input 4",
    ]
