"""Tests for setup, entities, services and diagnostics."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.blackbird4.api import (
    BlackbirdConnectionError,
    ConnectionState,
)
from custom_components.blackbird4.const import DOMAIN
from custom_components.blackbird4.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


async def test_entities_created(hass: HomeAssistant, loaded) -> None:
    for output in range(1, 5):
        player = hass.states.get(f"media_player.blackbird_4x4_output_{output}")
        assert player.state == STATE_ON
        assert player.attributes["source"] == f"Input {output}"
        select = hass.states.get(f"select.blackbird_4x4_output_{output}_source")
        assert select.state == f"Input {output}"
        assert select.attributes["options"] == [
            "Input 1",
            "Input 2",
            "Input 3",
            "Input 4",
        ]


async def test_select_source(hass: HomeAssistant, loaded, mock_client) -> None:
    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": "media_player.blackbird_4x4_output_1", "source": "Input 3"},
        blocking=True,
    )
    mock_client.async_set_output.assert_awaited_with(1, 3)
    await hass.async_block_till_done()
    assert (
        hass.states.get("media_player.blackbird_4x4_output_1").attributes["source"]
        == "Input 3"
    )


async def test_select_option(hass: HomeAssistant, loaded, mock_client) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.blackbird_4x4_output_2_source", "option": "Input 4"},
        blocking=True,
    )
    mock_client.async_set_output.assert_awaited_with(2, 4)
    # Optimistic until the device's next poll says otherwise.
    assert hass.states.get("select.blackbird_4x4_output_2_source").state == "Input 4"


async def test_unavailable_on_error(
    hass: HomeAssistant, loaded, mock_client, freezer: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_state.side_effect = BlackbirdConnectionError("down")
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert (
        hass.states.get("media_player.blackbird_4x4_output_1").state
        == STATE_UNAVAILABLE
    )
    assert (
        hass.states.get("select.blackbird_4x4_output_1_source").state
        == STATE_UNAVAILABLE
    )


async def test_setup_retry_when_offline(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client
) -> None:
    mock_client.async_get_state.side_effect = BlackbirdConnectionError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_services(hass: HomeAssistant, loaded, mock_client) -> None:
    await hass.services.async_call(
        DOMAIN, "set_all_outputs", {"input": 2}, blocking=True
    )
    mock_client.async_set_all.assert_awaited_once()
    assert mock_client.async_set_all.await_args.args[0] == 2

    response = await hass.services.async_call(
        DOMAIN,
        "send_command",
        {"command": "PowerOFF."},
        blocking=True,
        return_response=True,
    )
    mock_client.async_send_raw.assert_awaited_with("PowerOFF.")
    assert response == {"reply": "OK"}


async def test_unload(hass: HomeAssistant, loaded) -> None:
    assert await hass.config_entries.async_unload(loaded.entry_id)
    assert loaded.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics(hass: HomeAssistant, loaded) -> None:
    with patch(
        "custom_components.blackbird4.diagnostics.async_web_ui_command_code",
        return_value=["[/js/x.js] power"],
    ):
        diag = await async_get_config_entry_diagnostics(hass, loaded)
    assert diag["web_ui_command_code"] == ["[/js/x.js] power"]
    assert diag["connections"] is None
    assert diag["routes"] == {1: 1, 2: 2, 3: 3, 4: 4}
    assert diag["raw_status"] == {"Outputbuttom": [1, 2, 3, 4]}


async def test_no_connection_sensors_without_support(
    hass: HomeAssistant, loaded
) -> None:
    assert not hass.states.async_entity_ids("binary_sensor")


async def _setup_with_connections(hass, config_entry, mock_client, freezer):
    mock_client.supports_connections = True
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    # Sensors' first poll is gathered into one after a short delay.
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_connection_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_client.async_get_connections.return_value = ConnectionState(
        inputs={1: True, 2: True, 3: True, 4: False},
        outputs={1: False, 2: True, 3: True},
        raw={"output_1": "ff", "output_2": "00"},
    )
    await _setup_with_connections(hass, config_entry, mock_client, freezer)
    # One poll for all eight sensors.
    mock_client.async_get_connections.assert_awaited_once()
    assert len(mock_client.async_get_connections.await_args.args[0]) == 8

    assert (
        hass.states.get("binary_sensor.blackbird_4x4_input_1_connected").state == "on"
    )
    assert (
        hass.states.get("binary_sensor.blackbird_4x4_input_4_connected").state == "off"
    )
    output_1 = hass.states.get("binary_sensor.blackbird_4x4_output_1_connected")
    assert output_1.state == "off"
    assert output_1.attributes["status_byte"] == "ff"
    # Not reported by the matrix.
    assert (
        hass.states.get("binary_sensor.blackbird_4x4_output_4_connected").state
        == STATE_UNAVAILABLE
    )


async def test_connection_status_failure_does_not_block_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_client.async_get_connections.side_effect = BlackbirdConnectionError("x")
    await _setup_with_connections(hass, config_entry, mock_client, freezer)
    assert hass.states.get("media_player.blackbird_4x4_output_1").state == STATE_ON
    assert (
        hass.states.get("binary_sensor.blackbird_4x4_input_1_connected").state
        == STATE_UNAVAILABLE
    )


def _disable(
    hass: HomeAssistant, entry: MockConfigEntry, unique_ids: list[str]
) -> None:
    registry = er.async_get(hass)
    for unique_id in unique_ids:
        registry.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            f"{entry.entry_id}_{unique_id}_connected",
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )


async def test_disabled_sensors_are_not_polled(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client,
    freezer: FrozenDateTimeFactory,
) -> None:
    config_entry.add_to_hass(hass)
    _disable(
        hass,
        config_entry,
        ["input_2", "input_3", "input_4", "output_1", "output_2", "output_3"],
    )
    await _setup_with_connections(hass, config_entry, mock_client, freezer)
    polled = mock_client.async_get_connections.await_args.args[0]
    assert sorted(polled) == [("input", 1), ("output", 4)]


async def test_nothing_polled_when_all_sensors_disabled(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client,
    freezer: FrozenDateTimeFactory,
) -> None:
    config_entry.add_to_hass(hass)
    _disable(
        hass,
        config_entry,
        [f"{kind}_{port}" for kind in ("input", "output") for port in range(1, 5)],
    )
    await _setup_with_connections(hass, config_entry, mock_client, freezer)
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    mock_client.async_get_connections.assert_not_awaited()
    # Routing keeps working.
    assert hass.states.get("media_player.blackbird_4x4_output_1").state == STATE_ON


async def test_upgrade_keeps_entity_ids(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client
) -> None:
    """An entry from an older version keeps its entities, including renamed ones."""
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # Entities as an older version registered them, one renamed by the user.
    registry.async_get_or_create(
        "media_player",
        DOMAIN,
        f"{config_entry.entry_id}_output_1_media_player",
        config_entry=config_entry,
        suggested_object_id="living_room_tv",
    )
    registry.async_get_or_create(
        "select",
        DOMAIN,
        f"{config_entry.entry_id}_output_1_select",
        config_entry=config_entry,
        suggested_object_id="blackbird_4x4_output_1_source",
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("media_player.living_room_tv") is not None
    assert hass.states.get("select.blackbird_4x4_output_1_source") is not None
    assert hass.states.get("media_player.blackbird_4x4_output_1") is None
    assert config_entry.options == {}
