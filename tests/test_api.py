"""Tests for the protocol client, against a fake device on localhost."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
import json
import re
from urllib.parse import unquote_plus

import pytest

from custom_components.blackbird4.api import (
    BEEP_OFF_FRAME,
    BEEP_ON_FRAME,
    GET_BEEP_FRAME,
    GET_POWER_FRAME,
    POWER_OFF_FRAME,
    POWER_ON_FRAME,
    BlackbirdConnectionError,
    BlackbirdMatrix,
    BlackbirdProtocolError,
    WebHexTransport,
    WebTerminalTransport,
    async_probe,
    async_web_ui_command_code,
    build_frame,
    connection_byte,
    frame_to_hex,
    jquery_encode,
    parse_frames,
    parse_http_response,
    parse_status_body,
    query_frame,
    route_command,
    state_from_text,
    switch_frame,
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

    def __init__(
        self,
        routes: dict[int, int],
        accept_long_form: bool = True,
        unframed: bool = False,
    ) -> None:
        self.routes = routes
        self.accept_long_form = accept_long_form
        # Unframed: no Content-Length, the connection is left open, and an
        # empty reply buffer gets no response at all.
        self.unframed = unframed
        self.commands: list[str] = []
        self.buffer = ""

    def reply(self, request: bytes) -> bytes:
        line = request.split(b"\r\n", 1)[0].decode()
        if line.startswith("GET /cgi-bin/query "):
            body, self.buffer = self.buffer, ""
            if self.unframed:
                if not body:
                    return b""
                return b"HTTP/1.1 200 OK\r\n\r\n" + body.encode()
            return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (
                len(body),
                body.encode(),
            )
        if line.startswith("GET /cgi-bin/instr?cmd="):
            command = unquote_plus(line.split("cmd=", 1)[1].split(" ")[0]).strip()
            self.commands.append(command)
            self.buffer = self._run(command)
            if self.unframed:
                return b"HTTP/1.1 200 OK\r\n\r\n"
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
async def test_web_terminal_unframed_keep_alive(
    device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(WebTerminalTransport, "idle_timeout", 0.05)
    monkeypatch.setattr(WebTerminalTransport, "query_timeout", 0.2)
    terminal = FakeTerminal({1: 2, 2: 2, 3: 1, 4: 4}, unframed=True)
    fake = await device(terminal.reply)
    fake.keep_open = True
    client = BlackbirdMatrix("127.0.0.1", fake.port, protocol="web_terminal", timeout=2)
    assert (await client.async_get_state()).outputs == {1: 2, 2: 2, 3: 1, 4: 4}
    await client.async_set_output(1, 3)
    assert terminal.routes[1] == 3


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


# The frame the real PN 39668 had in its reply buffer.
SEEN_FRAME = "hex(a5,5b,01,03,01,00,12,00,00,00,00,00,e9)"


def test_frames() -> None:
    assert frame_to_hex(query_frame(1)) == "hex(a5,5b,02,01,01,00,00,00,00,00,00,00,fc)"
    # Input byte first, as the device's web UI builds it (input 2 -> output 1).
    assert switch_frame(1, 2).hex() == "a55b0203020001" + "00" * 5 + "f8"
    assert parse_frames(SEEN_FRAME) == [
        bytes.fromhex("a55b010301001200000000" + "00e9")
    ]
    assert parse_frames("hex(a5,5b,01,03,01,00,12,00,00,00,00,00,e8)") == []
    assert build_frame(bytes.fromhex("a55b020101"))[-1] == 0xFC


def test_power_and_beep_frames_match_web_ui() -> None:
    # Literal strings from the PN 39668's js/processor.js.
    assert frame_to_hex(POWER_ON_FRAME) == "hex(a5,5b,08,0b,0f,00,00,00,00,00,00,00,de)"
    assert (
        frame_to_hex(POWER_OFF_FRAME) == "hex(a5,5b,08,0b,f0,00,00,00,00,00,00,00,fd)"
    )
    assert (
        frame_to_hex(GET_POWER_FRAME) == "hex(a5,5b,08,0c,00,00,00,00,00,00,00,00,ec)"
    )
    assert frame_to_hex(BEEP_ON_FRAME) == "hex(a5,5b,06,01,0f,00,00,00,00,00,00,00,ea)"
    assert frame_to_hex(BEEP_OFF_FRAME) == "hex(a5,5b,06,01,f0,00,00,00,00,00,00,00,09)"
    assert frame_to_hex(GET_BEEP_FRAME) == "hex(a5,5b,01,0b,00,00,00,00,00,00,00,00,f4)"


def _frame(*body: int) -> bytes:
    return build_frame(bytes([0xA5, 0x5B, *body]))


class FakeHexMatrix:
    """Mimics the PN 39668 web UI: /cgi-bin/submit in, /cgi-bin/query out.

    The reply buffer keeps the last reply (it is not cleared by reading).
    While powered off, every command is answered with "power off".
    """

    #: Like the real unit's General tab: every input has a source; output 1
    #: has no display. 00 means connected on this firmware.
    connected_inputs = (1, 2, 3, 4)
    connected_outputs = (2, 3, 4)

    def __init__(self, routes: dict[int, int]) -> None:
        self.routes = routes
        self.power = True
        self.beep = True
        self.frames: list[bytes] = []
        self.buffer = SEEN_FRAME

    def reply(self, request: bytes) -> bytes:
        line = request.split(b"\r\n", 1)[0].decode()
        if line.startswith("GET /cgi-bin/query "):
            body = self.buffer.encode()
            chunk = f"{len(body):X}\r\n".encode() + body + b"\r\n0\r\n\r\n"
            return (
                b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
                b"Transfer-Encoding: chunked\r\ncontent-type: text/plain\r\n\r\n"
                + chunk
            )
        if line.startswith("GET /cgi-bin/submit?cmd="):
            (frame,) = parse_frames(
                unquote_plus(line.split("cmd=", 1)[1].split(" ")[0])
            )
            self.frames.append(frame)
            self.buffer = frame_to_hex(self._run(frame))
            return b"HTTP/1.1 200 OK\r\n\r\n"
        return b"HTTP/1.1 404 Not Found\r\n\r\n"

    def _run(self, frame: bytes) -> bytes:
        command, value = frame[2:4], frame[4]
        if command == b"\x08\x0b":
            self.power = value == 0x0F
            return _frame(0x08, 0x0B, 0x00)
        if not self.power:
            return _frame(0x08, 0x0C, 0xF0)
        if command == b"\x08\x0c":
            return _frame(0x08, 0x0C, 0x0F)
        if command == b"\x06\x01":
            self.beep = value == 0x0F
            return _frame(0x06, 0x01, 0x00)
        if command == b"\x01\x0b":
            return _frame(0x01, 0x0B, 0x00, 0x00, 0x00 if self.beep else 0xFF)
        if command == b"\x01\x04":
            return _frame(
                0x01, 0x04, value, 0x00 if value in self.connected_inputs else 0xFF
            )
        if command == b"\x01\x05":
            return _frame(
                0x01, 0x05, value, 0x00 if value in self.connected_outputs else 0xFF
            )
        if command == b"\x02\x01":
            return _frame(0x02, 0x01, value, 0, self.routes[value])
        if command == b"\x02\x03":
            self.routes[frame[6]] = frame[4]
        return frame


@pytest.fixture
async def hex_matrix(device, fast_terminal, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(WebHexTransport, "idle_timeout", 0.05)

    async def _start() -> tuple[FakeHexMatrix, BlackbirdMatrix]:
        matrix = FakeHexMatrix({1: 1, 2: 2, 3: 3, 4: 4})
        fake = await device(matrix.reply)
        return matrix, BlackbirdMatrix("127.0.0.1", fake.port, protocol="web_hex")

    return _start


async def test_web_hex_state(hex_matrix) -> None:
    matrix, client = await hex_matrix()
    matrix.routes.update({1: 2, 3: 4})
    state = await client.async_get_state()
    assert state.outputs == {1: 2, 2: 2, 3: 4, 4: 4}
    assert state.power is True
    # Power first, then one query per output.
    assert [f[2:5] for f in matrix.frames] == [
        bytes([0x08, 0x0C, 0x00]),
        *(bytes([0x02, 0x01, n]) for n in range(1, 5)),
    ]


async def test_web_hex_switch(hex_matrix) -> None:
    matrix, client = await hex_matrix()
    await client.async_set_output(1, 3)
    assert matrix.routes == {1: 3, 2: 2, 3: 3, 4: 4}
    assert matrix.frames == [switch_frame(1, 3)]


async def test_web_hex_power(hex_matrix) -> None:
    matrix, client = await hex_matrix()
    await client.async_set_power(False)
    assert matrix.power is False
    state = await client.async_get_state()
    assert state.power is False
    assert state.outputs == {}
    # Off: no routing queries are sent.
    assert matrix.frames[-1] == GET_POWER_FRAME

    await client.async_set_power(True)
    assert (await client.async_get_state()).power is True


async def test_web_hex_beep(hex_matrix) -> None:
    matrix, client = await hex_matrix()
    assert (await client.async_get_connections([("beep", 0)])).beep is True
    await client.async_set_beep(False)
    assert matrix.beep is False
    assert (await client.async_get_connections([("beep", 0)])).beep is False


async def test_web_hex_raw_command(hex_matrix) -> None:
    matrix, client = await hex_matrix()
    matrix.routes[2] = 3
    reply = await client.async_send_raw("a5 5b 02 01 02")
    assert reply == "hex(a5,5b,02,01,02,00,03,00,00,00,00,00,f8)"


async def test_web_hex_connections(hex_matrix) -> None:
    _, client = await hex_matrix()
    assert client.supports_connections
    state = await client.async_get_connections()
    assert state.inputs == {1: True, 2: True, 3: True, 4: True}
    assert state.outputs == {1: False, 2: True, 3: True, 4: True}
    assert state.raw["output_1"] == "ff"
    assert state.raw["input_1"] == "00"
    assert state.frames["output_1"].startswith("hex(a5,5b,01,05,01,ff")


def test_connection_byte_positions() -> None:
    # Some sheets put the status in byte 7 rather than byte 6.
    frame = build_frame(bytes([0xA5, 0x5B, 0x01, 0x05, 0x02, 0x00, 0xFF]))
    assert connection_byte([frame], "output", 2) == 0xFF
    assert connection_byte([frame], "output", 1) is None


async def test_web_ui_command_code(device) -> None:
    pages = {
        b"GET / ": b'<script src="js/jquery.min.js"></script><script src="js/processor.js"></script>',
        b"GET /js/jquery.min.js ": b"power power power",
        b"GET /js/processor.js ": b"function setPower(on){send([0xa5,0x5b,0x08,0x0b,on?0x0f:0xf0]);}",
    }

    def reply(request: bytes) -> bytes:
        for prefix, body in pages.items():
            if request.startswith(prefix):
                return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (
                    len(body),
                    body,
                )
        return b"HTTP/1.1 404 Not Found\r\n\r\n"

    fake = await device(reply)
    code = await async_web_ui_command_code("127.0.0.1", fake.port)
    assert any("[/js/processor.js]" in snip and "0xa5,0x5b" in snip for snip in code)
    assert not any("jquery" in snip for snip in code)
