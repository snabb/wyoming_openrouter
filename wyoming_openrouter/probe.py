"""Minimal local Wyoming Describe/Info probe, used by healthcheck.sh and
run.sh's discovery-readiness check.

Deliberately not `nc`-based: `nc -w N` doesn't exit as soon as it receives
the expected response, only when the connection closes or its own idle
timeout elapses -- and Wyoming connections are intentionally kept open by
the server (a client may send further events on the same socket), so every
`nc` probe pays the full idle timeout as a fixed floor regardless of how
fast the real answer arrives (confirmed live: an instantly-responding
server still took the full `-w` duration wall-clock).

This speaks just enough of the actual Wyoming wire format to be correct,
rather than treating the response as an opaque blob to grep like the `nc`
version did: each event is a JSON header line (with a `data_length` field)
followed by exactly that many additional bytes of JSON data -- NOT
newline-terminated, and NOT part of the header line. The header line alone
never contains "openrouter" (it's just `{"type": "info", "data_length":
N}`); that text only appears in the data segment that follows. Confirmed
live: an earlier version that only read up to the first newline always
failed, since it never read far enough to see it.
"""

import json
import socket
import sys

_TIMEOUT_SECONDS = 2.0


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def probe(port: int, timeout: float = _TIMEOUT_SECONDS) -> bool:
    """Return True if the local Wyoming server on `port` answers Describe."""
    try:
        with socket.create_connection(("localhost", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b'{"type": "describe"}\n')

            header = b""
            while b"\n" not in header:
                chunk = sock.recv(4096)
                if not chunk:
                    return False
                header += chunk
            header_line, _, rest = header.partition(b"\n")
            event = json.loads(header_line)

            data_length = event.get("data_length") or 0
            data = rest
            if len(data) < data_length:
                data += _recv_exact(sock, data_length - len(data))
    except (OSError, ValueError):
        return False
    return b"openrouter" in data


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python3 -m wyoming_openrouter.probe <port>", file=sys.stderr)
        sys.exit(2)
    port = int(sys.argv[1])
    if not probe(port):
        print(f"probe: task on port {port} did not respond", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
