"""Async client for the Monoprice Blackbird 4K 4x4 HDMI matrix.

Blackbird matrices are rebadged OEM hardware, and the web UI's protocol
depends on the OEM firmware. Three are supported:

``http``: the web UI talks to two CGI endpoints:

* ``/cgi-bin/MUH44TP_getsetparams.cgi`` returns the current routing as a
  JavaScript-ish object wrapped in parentheses.
* ``/cgi-bin/MMX32_Keyvalue.cgi`` accepts ``{CMD=<rs232 command>`` and passes
  the command through to the matrix's RS-232 command processor.

The embedded web server sends HTTP responses that are not standards
compliant, so aiohttp/httpx reject them. This module speaks just enough HTTP
over a raw socket to work around that.

``json``: the web UI posts JSON commands such as
``{"comhead": "get video status", "language": 0}`` to ``/cgi-bin/instr``.

``tcp``: the RS-232 command set on a raw TCP port.
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

JSON_PATH = "/cgi-bin/instr"
JSON_STATUS = {"comhead": "get video status", "language": 0}

PROTOCOL_HTTP = "http"
PROTOCOL_JSON = "json"
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
            headers, body = text.split(separator, 1)
            if re.search(r"transfer-encoding:\s*chunked", headers, re.IGNORECASE):
                return _dechunk(body)
            return body
    return ""


def _dechunk(body: str) -> str:
    """Decode a chunked body, tolerating a missing terminator."""
    out: list[str] = []
    data = body.encode()
    while data:
        size_line, _, rest = data.partition(b"\r\n")
        try:
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return body
        if size == 0:
            break
        out.append(rest[:size].decode("utf-8", errors="replace"))
        data = rest[size + 2 :]
    return "".join(out)


def _response_complete(data: bytes) -> bool:
    """Whether a raw HTTP response has been fully received (for keep-alive servers)."""
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep:
        head, sep, body = data.partition(b"\n\n")
    if not sep:
        return False
    headers = head.decode("latin-1").lower()
    if match := re.search(r"content-length:\s*(\d+)", headers):
        return len(body) >= int(match.group(1))
    if "chunked" in headers:
        return body.endswith(b"0\r\n\r\n")
    return False


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


def state_from_json(data: Any) -> MatrixState:
    """Build a MatrixState from a ``get video status`` reply.

    ``allsource[n]`` is the input routed to output n+1. Some firmware adds a
    trailing "all outputs" entry; it is kept but no entity uses it.
    """
    routes = data.get("allsource") if isinstance(data, dict) else None
    if not isinstance(routes, list):
        raise BlackbirdProtocolError(f"No 'allsource' in reply: {str(data)[:200]!r}")
    state = MatrixState(raw=data)
    for index, value in enumerate(routes):
        try:
            state.outputs[index + 1] = int(value)
        except (TypeError, ValueError):
            continue
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
        return parse_http_response(await self.request("POST", path, body))

    async def request(
        self,
        method: str,
        path: str,
        body: str = "",
        content_type: str = "application/x-www-form-urlencoded",
    ) -> bytes:
        """Send a request and return the raw response, headers included."""
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Accept: */*\r\n"
            f"Content-Type: {content_type}\r\n"
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
                    response = b""
                    # Some servers ignore "Connection: close", so stop once
                    # the response is complete instead of waiting for EOF.
                    while chunk := await reader.read(4096):
                        response += chunk
                        if _response_complete(response):
                            break
                finally:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
        except (OSError, TimeoutError) as err:
            raise BlackbirdConnectionError(
                f"Cannot reach {self.host}:{self.port}: {err}"
            ) from err
        return response

    async def get_state(self) -> MatrixState:
        """Return the current routing."""
        return state_from_status(
            parse_status_body(await self._post(STATUS_PATH, STATUS_BODY))
        )

    async def send(self, command: str) -> str:
        """Send an RS-232 command through the web UI passthrough."""
        return await self._post(COMMAND_PATH, "{CMD=" + build_command(command))

    async def set_output(self, output: int, input_: int) -> None:
        """Route ``input_`` to ``output``."""
        await self.send(route_command(output, input_))


class JsonTransport(HttpTransport):
    """Talk to firmware whose web UI posts JSON commands to /cgi-bin/instr."""

    protocol = PROTOCOL_JSON

    async def command(self, payload: dict[str, Any]) -> Any:
        """Send a JSON command and return the decoded reply."""
        body = await self._post_json(json.dumps(payload))
        try:
            return json.loads(body)
        except ValueError as err:
            raise BlackbirdProtocolError(f"Not JSON: {body[:200]!r}") from err

    async def _post_json(self, body: str) -> str:
        return parse_http_response(
            await self.request("POST", JSON_PATH, body, "application/json")
        )

    async def get_state(self) -> MatrixState:
        """Return the current routing."""
        return state_from_json(await self.command(JSON_STATUS))

    async def send(self, command: str) -> str:
        """Send a raw JSON command, e.g. ``{"comhead": "get status", "language": 0}``."""
        try:
            json.loads(command)
        except ValueError as err:
            raise BlackbirdProtocolError(
                "This matrix takes JSON commands, e.g. "
                '{"comhead": "get video status", "language": 0}'
            ) from err
        return await self._post_json(command)

    async def set_output(self, output: int, input_: int) -> None:
        """Route ``input_`` to ``output``."""
        reply = await self.command(
            {"comhead": "video switch", "language": 0, "source": [output, input_]}
        )
        if isinstance(reply, dict) and reply.get("result") in (0, "0"):
            raise BlackbirdProtocolError(f"Matrix rejected the switch: {reply}")


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

    async def set_output(self, output: int, input_: int) -> None:
        """Route ``input_`` to ``output``."""
        await self.send(route_command(output, input_))


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
        transport_cls = {
            PROTOCOL_TCP: TcpTransport,
            PROTOCOL_JSON: JsonTransport,
        }.get(protocol, HttpTransport)
        self._transport: HttpTransport | TcpTransport = transport_cls(
            host, port, timeout
        )
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
        async with self._lock:
            await self._transport.set_output(output, input_)

    async def async_set_all(self, input_: int, outputs: range | list[int]) -> None:
        """Route ``input_`` to every output in ``outputs``.

        Sent one output at a time: not every firmware accepts ``OUT00``.
        """
        for output in outputs:
            await self.async_set_output(output, input_)

    async def async_send_raw(self, command: str) -> str:
        """Send a raw command (RS-232 text, or JSON for ``json``) and return the reply."""
        async with self._lock:
            return await self._transport.send(command)


_REF_RE = re.compile(
    r"""(?:src|href|action|url)\s*[=:(]\s*["']?([\w./?=&-]+\.(?:cgi|js|html?|asp|php)[\w?=&]*)"""
    r"""|location(?:\.href)?\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
# Code that talks to the device: endpoints, ajax calls and command payloads.
_REQUEST_RE = re.compile(
    r"""cgi-bin|comhead|\$\.(?:ajax|post|get)\b|XMLHttpRequest|\.open\(|\burl\s*:"""
    r"""|JSON\.stringify|CMD=""",
)
_MAX_SNIPPETS = 25


def _first_line(response: bytes) -> str:
    return response.decode("utf-8", errors="replace").split("\n", 1)[0].strip()


def _snippet(text: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _request_snippets(body: str) -> list[str]:
    """Return short, de-duplicated excerpts of code that makes device requests."""
    snippets: list[str] = []
    last_end = -1
    for match in _REQUEST_RE.finditer(body):
        if match.start() < last_end:
            continue
        start, last_end = max(0, match.start() - 60), match.end() + 100
        snippet = _snippet(body[start:last_end], 160)
        if snippet not in snippets:
            snippets.append(snippet)
    return snippets


async def _probe_post(
    http: HttpTransport, path: str, body: str, content_type: str, lines: list[str]
) -> None:
    try:
        raw = await http.request("POST", path, body, content_type)
    except BlackbirdConnectionError as err:
        lines.append(f"POST {path}: {err}")
        return
    try:
        reply = parse_http_response(raw)
    except BlackbirdProtocolError:
        reply = ""
    lines.append(f"POST {path} {body[:40]}: {_first_line(raw)}")
    if reply.strip():
        lines.append(f"  {_snippet(reply, 300)}")


async def async_probe(host: str, port: int, timeout: float = 3.0) -> str:
    """Describe what the web server at host:port serves, for unsupported firmware.

    Fetches the index page and the scripts it references, quotes the code
    that sends requests to the device, and shows the replies to the status
    requests of each supported protocol.
    """
    http = HttpTransport(host, port, timeout)
    lines: list[str] = []
    snippets: list[str] = []
    queue = ["/"]
    seen: set[str] = set()
    while queue and len(seen) < 10:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            raw = await http.request("GET", path)
        except BlackbirdConnectionError as err:
            lines.append(f"GET {path}: {err}")
            continue
        try:
            body = parse_http_response(raw)
        except BlackbirdProtocolError:
            body = ""
        lines.append(f"GET {path}: {_first_line(raw)} ({len(body)} bytes)")
        for match in _REF_RE.finditer(body):
            ref = next(group for group in match.groups() if group)
            ref = "/" + ref.lstrip("./").split("?")[0].lstrip("/")
            if ref not in seen and not ref.lower().endswith((".png", ".jpg", ".css")):
                queue.append(ref)
        # jQuery and similar libraries only produce noise.
        if "jquery" not in path.lower() and ".min." not in path:
            snippets += [f"[{path}] {snip}" for snip in _request_snippets(body)]
    lines.append("Device request code:")
    lines += [f"  {snip}" for snip in snippets[:_MAX_SNIPPETS]] or ["  none found"]
    if len(snippets) > _MAX_SNIPPETS:
        lines.append(f"  ... {len(snippets) - _MAX_SNIPPETS} more")
    await _probe_post(
        http, JSON_PATH, json.dumps(JSON_STATUS), "application/json", lines
    )
    await _probe_post(
        http, STATUS_PATH, STATUS_BODY, "application/x-www-form-urlencoded", lines
    )
    return "\n".join(lines)
