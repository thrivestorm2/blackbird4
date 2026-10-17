"""Constants for the Monoprice Blackbird 4x4 integration."""

from typing import Final

DOMAIN: Final = "blackbird4"
MANUFACTURER: Final = "Monoprice"
MODEL: Final = "Blackbird 4K 4x4 HDMI Matrix"

CONF_PROTOCOL: Final = "protocol"
CONF_SCAN_INTERVAL: Final = "scan_interval"

DEFAULT_PORT: Final = 80
DEFAULT_SCAN_INTERVAL: Final = 10
# Ports tried for the raw TCP fallback when the web UI endpoints don't answer.
TCP_FALLBACK_PORTS: Final = (23, 4001, 8000, 5000)
# Ports tried with the bang command set; the 39668 manual lists 8000.
TCP_BANG_PORTS: Final = (8000, 23, 4001, 5000)

NUM_INPUTS: Final = 4
NUM_OUTPUTS: Final = 4
INPUTS: Final = range(1, NUM_INPUTS + 1)
OUTPUTS: Final = range(1, NUM_OUTPUTS + 1)

SERVICE_SET_ALL_OUTPUTS: Final = "set_all_outputs"
SERVICE_SEND_COMMAND: Final = "send_command"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_INPUT: Final = "input"
ATTR_COMMAND: Final = "command"


def input_key(number: int) -> str:
    """Return the options key holding an input's display name."""
    return f"input_{number}"


def output_key(number: int) -> str:
    """Return the options key holding an output's display name."""
    return f"output_{number}"
