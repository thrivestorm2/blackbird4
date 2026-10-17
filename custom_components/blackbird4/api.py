"""Async client for the Monoprice Blackbird 4K 4x4 HDMI matrix.

The matrix's web UI talks to two CGI endpoints:

* ``/cgi-bin/MUH44TP_getsetparams.cgi`` returns the current routing as a
  JavaScript-ish object wrapped in parentheses.
* ``/cgi-bin/MMX32_Keyvalue.cgi`` accepts ``{CMD=<rs232 command>`` and passes
  the command through to the matrix's RS-232 command processor.

The embedded web server sends HTTP responses that are not standards
compliant, so aiohttp/httpx reject them. This module speaks just enough HTTP
over a raw socket to work around that.

Some firmware revisions expose the RS-232 command set on a raw TCP port
instead; ``TcpTransport`` covers that case with the same interface.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
from dataclasses import dataclass, field
import json
import re
from typing import Any

STATUS_PATH = "/cgi-bin/MUH44TP_getsetparams.cgi"
COMMAND_PATH = "/cgi-bin/MMX32_Keyvalue.cgi"
# The web UI posts this body to the status endpoint; any non-empty body works.
STATUS_BODY = "lcc"

PROTOCOL_HTTP = "http"
PROTOCOL_TCP = "tcp"

_TCP_STATUS_RE = re.compile(
    r"out(?:put)?\s*0*(\d+)(?:\s*:\s*|\D{0,20}?in(?:put)?\s*)0*(\d+)",
    re.IGNORECASE,
)


class BlackbirdError(Exception):
    """Base error for the Blackbird client."""


class BlackbirdConnectionError(BlackbirdError):
    """The matrix could not be reached."""


class BlackbirdProtocolError(BlackbirdError):
    """The matrix replied with something we could not understand."""


@dataclass
class MatrixState:
    """Routing state of the matrix: output number -> input number."""

    outputs: dict[int, int] = field(default_factory=dict)
    raw: Any = None

    def input_for(self, output: int) -> int | None:
        """Return the input routed to ``output``, if known."""
        return self.outputs.get(output)


def build_command(command: str) -> str:
    """Normalise an RS-232 command so it ends with the required period."""
    command = command.strip()
    return command if command.endswith(".") else f"{command}."


def route_command(output: int, input_: int) -> str:
    """Return the RS-232 command routing ``input_`` to ``output`` (0 = all)."""
    return f"OUT{output:02d}:{input_:02d}."


def parse_http_response(response: bytes) -> str:
    """Strip headers from a (possibly malformed) HTTP response and return the body.

    Raises BlackbirdProtocolError for a non-200 status line.
    """
    text = response.decode("utf-8", errors="replace")
    if not text.startswith("HTTP/"):
        # No status line at all: treat everything as body.
        return text
    status_line = text.split("\n", 1)[0]
    parts = status_line.split()
    if len(parts) >= 2 and parts[1].isdigit() and parts[1] != "200":
        raise BlackbirdProtocolError(f"HTTP status {parts[1]}")
    for separator in ("\r\n\r\n", "\n\n", "\r\r"):
        if separator in text:
            return text.split(separator, 1)[1]
    return ""


def parse_status_body(body: str) -> dict[str, Any]:
    """Parse the ``({'Outputbuttom': [...], ...})`` status blob."""
    body = body.strip().strip(";").strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1].strip()
    try:
        data = ast.literal_eval(body)
    except (ValueError, SyntaxError):
        try:
            data = json.loads(body.replace("'", '"'))
        except ValueError as err:
            raise BlackbirdProtocolError(f"Unparseable status: {body[:200]!r}") from err
    if not isinstance(data, dict):
        raise BlackbirdProtocolError(f"Unexpected status: {body[:200]!r}")
    return data


def state_from_status(data: dict[str, Any]) -> MatrixState:
    """Build a MatrixState from the parsed HTTP status blob."""
    routes = data.get("Outputbuttom")
    if not isinstance(routes, list):
        raise BlackbirdProtocolError("Status has no 'Outputbuttom' list")
    state = MatrixState(raw=data)
    for index, value in enumerate(routes):
        try:
            state.outputs[index + 1] = int(value)
        except (TypeError, ValueError):
            continue
    return state


def state_from_text(text: str) -> MatrixState:
    """Build a MatrixState from an RS-232 style status reply.

    Matches lines such as ``Output 01 Switch To In 02!`` or ``OUT01:02``.
    """
    state = MatrixState(raw=text)
    for output, input_ in _TCP_STATUS_RE.findall(text):
        state.outputs[int(output)] = int(input_)
    if not state.outputs:
        raise BlackbirdProtocolError(f"No routing found in reply: {text[:200]!r}")
    return state


class HttpTransport:
    """Talk to the matrix through its web UI's CGI endpoints."""

    protocol = PROTOCOL_HTTP

    def __init__(self, host: str, port: int, timeout: float) -> None:
        """Initialise the transport."""
        self.host = host
        self.port = port
        self.timeout = timeout

    async def _post(self, path: str, body: str) -> str:
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Accept: */*\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {len(body.encode())}\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"{body}"
        ).encode()
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_connection(self.host, self.port)
                try:
                    writer.write(request)
                    await writer.drain()
                    response = await reader.read()
                finally:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
        except (OSError, TimeoutError) as err:
            raise BlackbirdConnectionError(
                f"Cannot reach {self.host}:{self.port}: {err}"
            ) from err
        return parse_http_response(response)

    async def get_state(self) -> MatrixState:
        """Return the current routing."""
        return state_from_status(
            parse_status_body(await self._post(STATUS_PATH, STATUS_BODY))
        )

    async def send(self, command: str) -> str:
        """Send an RS-232 command through the web UI passthrough."""
        return await self._post(COMMAND_PATH, "{CMD=" + build_command(command))


class TcpTransport:
    """Talk to the matrix's RS-232 command set over a raw TCP socket."""

    protocol = PROTOCOL_TCP

    def __init__(self, host: str, port: int, timeout: float) -> None:
        """Initialise the transport."""
        self.host = host
        self.port = port
        self.timeout = timeout

    async def _exchange(self, command: str) -> str:
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_connection(self.host, self.port)
        except (OSError, TimeoutError) as err:
            raise BlackbirdConnectionError(
                f"Cannot reach {self.host}:{self.port}: {err}"
            ) from err
        chunks: list[bytes] = []
        try:
            writer.write(build_command(command).encode() + b"\r\n")
            await writer.drain()
            # The matrix doesn't frame its replies; read until it goes quiet.
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(1024), 1.0)
                except TimeoutError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as err:
            raise BlackbirdConnectionError(str(err)) from err
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        return b"".join(chunks).decode("utf-8", errors="replace")

    async def get_state(self) -> MatrixState:
        """Return the current routing."""
        return state_from_text(await self._exchange("Status."))

    async def send(self, command: str) -> str:
        """Send an RS-232 command."""
        return await self._exchange(command)


class BlackbirdMatrix:
    """High-level client; serialises requests because the matrix handles one at a time."""

    def __init__(
        self,
        host: str,
        port: int = 80,
        protocol: str = PROTOCOL_HTTP,
        timeout: float = 5.0,
    ) -> None:
        """Initialise the client."""
        transport_cls = TcpTransport if protocol == PROTOCOL_TCP else HttpTransport
        self._transport = transport_cls(host, port, timeout)
        self._lock = asyncio.Lock()

    @property
    def host(self) -> str:
        """Return the host."""
        return self._transport.host

    @property
    def port(self) -> int:
        """Return the port."""
        return self._transport.port

    @property
    def protocol(self) -> str:
        """Return the protocol in use."""
        return self._transport.protocol

    async def async_get_state(self) -> MatrixState:
        """Return the current routing."""
        async with self._lock:
            return await self._transport.get_state()

    async def async_set_output(self, output: int, input_: int) -> None:
        """Route ``input_`` to ``output``."""
        await self.async_send_raw(route_command(output, input_))

    async def async_set_all(self, input_: int, outputs: range | list[int]) -> None:
        """Route ``input_`` to every output in ``outputs``.

        Sent one output at a time: not every firmware accepts ``OUT00``.
        """
        for output in outputs:
            await self.async_set_output(output, input_)

    async def async_send_raw(self, command: str) -> str:
        """Send an arbitrary RS-232 command and return the reply text."""
        async with self._lock:
            return await self._transport.send(command)
