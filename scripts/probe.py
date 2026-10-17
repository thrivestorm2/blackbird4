#!/usr/bin/env python3
"""Probe a Monoprice Blackbird matrix and print what its control interfaces say.

Standard library only. Run it from a computer on the same network as the matrix:

    python3 probe.py 192.168.1.39            # read-only
    python3 probe.py 192.168.1.39 --switch   # also routes input 1 -> output 1

Paste the output into a GitHub issue if the integration can't talk to your unit.
"""

from __future__ import annotations

import argparse
import re
import socket
import time

TIMEOUT = 5


def http(host: str, port: int, method: str, path: str, body: str = "") -> bytes:
    """Send a raw HTTP request and return the raw response (headers included)."""
    request = (
        f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nAccept: */*\r\n"
        "Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n{body}"
    ).encode()
    with socket.create_connection((host, port), TIMEOUT) as sock:
        sock.sendall(request)
        chunks = []
        while chunk := sock.recv(4096):
            chunks.append(chunk)
    return b"".join(chunks)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def show(data: bytes, limit: int = 1500) -> None:
    text = data.decode("utf-8", errors="replace")
    print(text[:limit] + ("\n... [truncated]" if len(text) > limit else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument(
        "--switch",
        action="store_true",
        help="send OUT01:01. and re-read the status (changes output 1!)",
    )
    args = parser.parse_args()
    host, port = args.host, args.port

    section(f"GET / on {host}:{port}")
    try:
        index = http(host, port, "GET", "/")
        show(index, 600)
        paths = sorted(
            set(re.findall(rb"[\w./-]+\.(?:cgi|js|html?)", index)), key=bytes.lower
        )
        print("\nReferenced files:", ", ".join(p.decode() for p in paths) or "none")
        for script in [p for p in paths if p.endswith(b".js")][:5]:
            section(f"CGI/commands referenced in {script.decode()}")
            try:
                js = http(host, port, "GET", "/" + script.decode().lstrip("/"))
                hits = sorted(set(re.findall(rb"[\w/-]+\.cgi|CMD=[^\"']{0,40}", js)))
                print("\n".join(h.decode(errors="replace") for h in hits) or "none")
            except OSError as err:
                print("failed:", err)
    except OSError as err:
        print("failed:", err)

    section("POST /cgi-bin/MUH44TP_getsetparams.cgi (status)")
    try:
        show(http(host, port, "POST", "/cgi-bin/MUH44TP_getsetparams.cgi", "lcc"))
    except OSError as err:
        print("failed:", err)

    if args.switch:
        section("POST /cgi-bin/MMX32_Keyvalue.cgi {CMD=OUT01:01.")
        try:
            show(
                http(
                    host, port, "POST", "/cgi-bin/MMX32_Keyvalue.cgi", "{CMD=OUT01:01."
                )
            )
            time.sleep(1)
            section("Status after switch")
            show(http(host, port, "POST", "/cgi-bin/MUH44TP_getsetparams.cgi", "lcc"))
        except OSError as err:
            print("failed:", err)

    section("Raw TCP control ports")
    for tcp_port in (23, 4001, 5000, 8000):
        try:
            with socket.create_connection((host, tcp_port), 2) as sock:
                sock.sendall(b"Status.\r\n")
                sock.settimeout(2)
                try:
                    reply = sock.recv(2048)
                except TimeoutError:
                    reply = b"(no reply)"
                print(
                    f"port {tcp_port}: open ->", reply.decode(errors="replace").strip()
                )
        except OSError as err:
            print(f"port {tcp_port}: {err}")


if __name__ == "__main__":
    main()
