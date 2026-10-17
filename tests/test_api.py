"""Tests for the protocol client, against a fake device on localhost."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable

import pytest

from custom_components.blackbird4.api import (
    BlackbirdConnectionError,
    BlackbirdMatrix,
    BlackbirdProtocolError,
    async_probe,
    parse_http_response,
    parse_status_body,
    route_command,
    state_from_text,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

STATUS_BODY = "({'Outputbuttom':['2','1','4','3'],'Inputname':['A','B','C','D']})"


class FakeDevice:
    """A minimal TCP server that records what it receives."""

    def __init__(self, reply: Callable[[bytes], bytes]) -> None:
        self.reply = reply
        self.received: list[bytes] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.server: asyncio.Server | None = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            data = await reader.read(4096)
            self.received.append(data)
            await asyncio.sleep(0.01)
            writer.write(self.reply(data))
            await writer.drain()
        finally:
            self.concurrent -= 1
            writer.close()

    @property
    def port(self) -> int:
        assert self.server
        return self.server.sockets[0].getsockname()[1]


@pytest.fixture
async def device() -> AsyncGenerator[Callable[[Callable[[bytes], bytes]], FakeDevice]]:
    servers: list[asyncio.Server] = []

    async def _start(reply: Callable[[bytes], bytes]) -> FakeDevice:
        fake = FakeDevice(reply)
        fake.server = await asyncio.start_server(fake._handle, "127.0.0.1", 0)
        servers.append(fake.server)
        return fake

    yield _start
    for server in servers:
        server.close()
        await server.wait_closed()


def _malformed_ok(body: str) -> bytes:
    # Mimics the device: \n-only line endings and no Content-Length.
    return f"HTTP/1.1 200 OK\nContent-type: text/html\n\n{body}".encode()


def test_parse_http_response_variants() -> None:
    assert parse_http_response(b"HTTP/1.1 200 OK\r\nA: b\r\n\r\nbody") == "body"
    assert parse_http_response(b"HTTP/1.0 200 OK\nA: b\n\nbody") == "body"
    assert parse_http_response(b"no status line") == "no status line"
    with pytest.raises(BlackbirdProtocolError):
        parse_http_response(b"HTTP/1.1 404 Not Found\r\n\r\n")


def test_parse_status_body() -> None:
    assert parse_status_body(STATUS_BODY)["Outputbuttom"] == ["2", "1", "4", "3"]
    assert parse_status_body('({"Outputbuttom":[1,2]});')["Outputbuttom"] == [1, 2]
    with pytest.raises(BlackbirdProtocolError):
        parse_status_body("<html>login</html>")


def test_state_from_text() -> None:
    state = state_from_text("Output 01 Switch To In 03!\r\nOUT02:04\r\n")
    assert state.outputs == {1: 3, 2: 4}
    with pytest.raises(BlackbirdProtocolError):
        state_from_text("Command FAILED")


def test_route_command() -> None:
    assert route_command(1, 2) == "OUT01:02."


async def test_http_get_state(device) -> None:
    fake = await device(lambda _: _malformed_ok(STATUS_BODY))
    client = BlackbirdMatrix("127.0.0.1", fake.port)
    state = await client.async_get_state()
    assert state.outputs == {1: 2, 2: 1, 3: 4, 4: 3}
    request = fake.received[0]
    assert request.startswith(b"POST /cgi-bin/MUH44TP_getsetparams.cgi HTTP/1.1\r\n")
    assert request.endswith(b"\r\n\r\nlcc")


async def test_http_set_output(device) -> None:
    fake = await device(lambda _: _malformed_ok(""))
    client = BlackbirdMatrix("127.0.0.1", fake.port)
    await client.async_set_output(3, 2)
    request = fake.received[0]
    assert request.startswith(b"POST /cgi-bin/MMX32_Keyvalue.cgi ")
    assert request.endswith(b"{CMD=OUT03:02.")


async def test_requests_are_serialised(device) -> None:
    fake = await device(lambda _: _malformed_ok(STATUS_BODY))
    client = BlackbirdMatrix("127.0.0.1", fake.port)
    await asyncio.gather(*(client.async_get_state() for _ in range(4)))
    assert len(fake.received) == 4
    assert fake.max_concurrent == 1


async def test_set_all_sends_each_output(device) -> None:
    fake = await device(lambda _: _malformed_ok(""))
    client = BlackbirdMatrix("127.0.0.1", fake.port)
    await client.async_set_all(4, range(1, 5))
    assert [r.rsplit(b"\r\n\r\n", 1)[1] for r in fake.received] == [
        b"{CMD=OUT01:04.",
        b"{CMD=OUT02:04.",
        b"{CMD=OUT03:04.",
        b"{CMD=OUT04:04.",
    ]


async def test_http_404_is_protocol_error(device) -> None:
    fake = await device(lambda _: b"HTTP/1.1 404 Not Found\r\n\r\n")
    with pytest.raises(BlackbirdProtocolError):
        await BlackbirdMatrix("127.0.0.1", fake.port).async_get_state()


async def test_connection_refused() -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(BlackbirdConnectionError):
        await BlackbirdMatrix("127.0.0.1", port, timeout=1).async_get_state()


async def test_tcp_transport(device) -> None:
    fake = await device(
        lambda _: b"Output 01 Switch To In 02!\r\nOutput 02 Switch To In 02!\r\n"
    )
    client = BlackbirdMatrix("127.0.0.1", fake.port, protocol="tcp")
    state = await client.async_get_state()
    assert state.outputs == {1: 2, 2: 2}
    assert fake.received[0] == b"Status.\r\n"
    await client.async_send_raw("PowerOFF")
    assert fake.received[1] == b"PowerOFF.\r\n"


async def test_probe_reports_endpoints(device) -> None:
    pages = {
        b"GET / ": b'<html><frameset><frame src="main.html"></frameset></html>',
        b"GET /main.html ": b'<script src="js/app.js"></script>',
        b"GET /js/app.js ": b'$.post("/cgi-bin/query.cgi", "{CMD=OUT"+o+":"+i+".");',
    }

    def reply(request: bytes) -> bytes:
        for prefix, body in pages.items():
            if request.startswith(prefix):
                return b"HTTP/1.1 200 OK\n\n" + body
        return b"HTTP/1.1 404 Not Found\r\n\r\n"

    fake = await device(reply)
    report = await async_probe("127.0.0.1", fake.port)
    assert "GET /main.html: HTTP/1.1 200 OK" in report
    assert "GET /js/app.js: HTTP/1.1 200 OK" in report
    assert "CGI endpoints: /cgi-bin/query.cgi" in report
    assert "CMD=OUT" in report
    assert "POST /cgi-bin/MUH44TP_getsetparams.cgi: HTTP/1.1 404 Not Found" in report
