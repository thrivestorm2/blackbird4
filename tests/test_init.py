"""Tests for setup, entities, services and diagnostics."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.blackbird4.api import BlackbirdConnectionError
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
    diag = await async_get_config_entry_diagnostics(hass, loaded)
    assert diag["routes"] == {1: 1, 2: 2, 3: 3, 4: 4}
    assert diag["raw_status"] == {"Outputbuttom": [1, 2, 3, 4]}
