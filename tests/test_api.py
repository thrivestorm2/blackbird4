"""Tests for the protocol client, against a fake device on localhost."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
import json
import re
from urllib.parse import unquote_plus

import pytest

from custom_components.blackbird4.api import (
    BlackbirdConnectionError,
    BlackbirdMatrix,
    BlackbirdProtocolError,
    WebTerminalTransport,
    async_probe,
    jquery_encode,
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
        self.keep_open = False

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
            if self.keep_open:
                # Like a keep-alive server: wait for the client to hang up.
                await reader.read()
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


async def test_probe_reports_request_code(device) -> None:
    pages = {
        b"GET / ": b'<html><script src="js/jquery.min.js"></script>'
        b'<script src="js/netRequest.js"></script></html>',
        b"GET /js/jquery.min.js ": b"$.ajax=function(a){return a}; url: noise",
        b"GET /js/netRequest.js ": b"function sw(o,i){$.ajax({url:'/cgi-bin/instr',"
        b"data:JSON.stringify({comhead:'video switch',source:[o,i]})});}",
    }

    def reply(request: bytes) -> bytes:
        for prefix, body in pages.items():
            if request.startswith(prefix):
                return b"HTTP/1.1 200 OK\n\n" + body
        return b"HTTP/1.1 404 Not Found\r\n\r\n"

    fake = await device(reply)
    report = await async_probe("127.0.0.1", fake.port)
    assert "GET /js/netRequest.js: HTTP/1.1 200 OK" in report
    assert "[/js/netRequest.js]" in report
    assert "comhead:'video switch'" in report
    assert "[/js/jquery.min.js]" not in report
    assert "POST /cgi-bin/instr" in report
    assert (
        "POST /cgi-bin/MUH44TP_getsetparams.cgi lcc: HTTP/1.1 404 Not Found" in report
    )


def _chunked(body: bytes) -> bytes:
    # Keep-alive + chunked, like the JSON firmware; the server never closes.
    return (
        b"HTTP/1.1 200 OK\r\nConnection: Keep-Alive\r\nTransfer-Encoding: chunked\r\n"
        b"content-type: text/plain\r\n\r\n"
        + f"{len(body):x}\r\n".encode()
        + body
        + b"\r\n0\r\n\r\n"
    )


async def test_json_transport(device) -> None:
    def reply(request: bytes) -> bytes:
        payload = json.loads(request.split(b"\r\n\r\n", 1)[1])
        if payload["comhead"] == "get video status":
            return _chunked(
                b'{"comhead":"get video status","power":1,"allsource":[2,1,4,3,1]}'
            )
        return _chunked(b'{"comhead":"video switch","result":1}')

    fake = await device(reply)
    fake.keep_open = True
    client = BlackbirdMatrix("127.0.0.1", fake.port, protocol="json", timeout=2)
    state = await client.async_get_state()
    assert state.outputs == {1: 2, 2: 1, 3: 4, 4: 3, 5: 1}
    assert b"Content-Type: application/json" in fake.received[0]

    await client.async_set_output(2, 3)
    assert json.loads(fake.received[1].split(b"\r\n\r\n", 1)[1]) == {
        "comhead": "video switch",
        "language": 0,
        "source": [2, 3],
    }


async def test_json_switch_rejected(device) -> None:
    fake = await device(lambda _: _chunked(b'{"comhead":"video switch","result":0}'))
    client = BlackbirdMatrix("127.0.0.1", fake.port, protocol="json")
    with pytest.raises(BlackbirdProtocolError):
        await client.async_set_output(1, 9)


def test_jquery_encode() -> None:
    # Matches $.param: spaces as "+", "!" left alone, CR percent-encoded.
    assert jquery_encode("r av out 0!\r") == "r+av+out+0!%0D"


def test_state_from_text_bang() -> None:
    text = "r av out 0!\r\ninput 2 -> output 1\r\ninput 1 -> output 2\r\ninput 3 -> output 2\r\n"
    assert state_from_text(text, "bang").outputs == {1: 2, 2: 3}


class FakeTerminal:
    """Replies like the B44H70 web UI: /cgi-bin/instr in, /cgi-bin/query out."""

    def __init__(self, routes: dict[int, int], accept_long_form: bool = True) -> None:
        self.routes = routes
        self.accept_long_form = accept_long_form
        self.commands: list[str] = []
        self.buffer = ""

    def reply(self, request: bytes) -> bytes:
        line = request.split(b"\r\n", 1)[0].decode()
        if line.startswith("GET /cgi-bin/query "):
            body, self.buffer = self.buffer, ""
            return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (
                len(body),
                body.encode(),
            )
        if line.startswith("GET /cgi-bin/instr?cmd="):
            command = unquote_plus(line.split("cmd=", 1)[1].split(" ")[0]).strip()
            self.commands.append(command)
            self.buffer = self._run(command)
            return b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        return b"HTTP/1.1 404 Not Found\r\n\r\n"

    def _run(self, command: str) -> str:
        if command == "r av out 0!":
            return "".join(
                f"input {i} -> output {o}\r\n" for o, i in sorted(self.routes.items())
            )
        if match := re.fullmatch(r"s in (\d) av out (\d)!", command):
            if not self.accept_long_form:
                return "command error!\r\n"
            self.routes[int(match.group(2))] = int(match.group(1))
            return f"input {match.group(1)} -> output {match.group(2)}\r\n"
        if match := re.fullmatch(r"s (\d) av (\d)!", command):
            self.routes[int(match.group(2))] = int(match.group(1))
            return f"input {match.group(1)} -> output {match.group(2)}\r\n"
        return "command error!\r\n"


@pytest.fixture
def fast_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WebTerminalTransport, "reply_delay", 0.01)


@pytest.mark.usefixtures("fast_terminal")
async def test_web_terminal(device) -> None:
    terminal = FakeTerminal({1: 1, 2: 2, 3: 3, 4: 4})
    fake = await device(terminal.reply)
    client = BlackbirdMatrix("127.0.0.1", fake.port, protocol="web_terminal")
    assert (await client.async_get_state()).outputs == {1: 1, 2: 2, 3: 3, 4: 4}

    await client.async_set_output(3, 2)
    assert terminal.commands[-1] == "s in 2 av out 3!"
    assert (await client.async_get_state()).outputs[3] == 2

    assert "input" in await client.async_send_raw("r av out 0")


@pytest.mark.usefixtures("fast_terminal")
async def test_web_terminal_falls_back_to_short_form(device) -> None:
    terminal = FakeTerminal({1: 1, 2: 2, 3: 3, 4: 4}, accept_long_form=False)
    fake = await device(terminal.reply)
    client = BlackbirdMatrix("127.0.0.1", fake.port, protocol="web_terminal")
    await client.async_set_output(4, 1)
    assert terminal.commands[-2:] == ["s in 1 av out 4!", "s 1 av 4!"]
    assert terminal.routes[4] == 1


async def test_web_terminal_missing_endpoints(device) -> None:
    fake = await device(lambda _: b"HTTP/1.1 404 Not Found\r\n\r\n")
    with pytest.raises(BlackbirdProtocolError):
        await BlackbirdMatrix(
            "127.0.0.1", fake.port, protocol="web_terminal"
        ).async_get_state()


async def test_tcp_bang(device) -> None:
    fake = await device(lambda _: b"input 3 -> output 1\r\ninput 4 -> output 2\r\n")
    client = BlackbirdMatrix("127.0.0.1", fake.port, protocol="tcp_bang")
    assert (await client.async_get_state()).outputs == {1: 3, 2: 4}
    assert fake.received[0] == b"r av out 0!\r\n"
    await client.async_set_output(1, 2)
    assert fake.received[1] == b"s in 2 av out 1!\r\n"
