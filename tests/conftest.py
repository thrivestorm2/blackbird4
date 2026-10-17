"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blackbird4.api import MatrixState
from custom_components.blackbird4.const import CONF_PROTOCOL, DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components in every test."""


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    """Patch the client used by the integration."""
    with patch(
        "custom_components.blackbird4.BlackbirdMatrix", autospec=True
    ) as client_cls:
        client = client_cls.return_value
        client.host = "192.168.1.39"
        client.protocol = "http"
        client.async_get_state.return_value = MatrixState(
            outputs={1: 1, 2: 2, 3: 3, 4: 4}, raw={"Outputbuttom": [1, 2, 3, 4]}
        )
        client.async_send_raw.return_value = "OK"
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured matrix."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Blackbird 4x4",
        unique_id="192.168.1.39",
        data={"host": "192.168.1.39", "port": 80, CONF_PROTOCOL: "http"},
    )
