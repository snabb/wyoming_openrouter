"""Minimal local Wyoming Describe/Info probe, used by healthcheck.sh and
run.sh's discovery-readiness check.

Deliberately not `nc`-based: `nc -w N` doesn't exit as soon as it receives
the expected response, only when the connection closes or its own idle
timeout elapses -- and Wyoming connections are intentionally kept open by
the server (a client may send further events on the same socket), so every
`nc` probe pays the full idle timeout as a fixed floor regardless of how
fast the real answer arrives (confirmed live: an instantly-responding
server still took the full `-w` duration wall-clock). This connects, sends
Describe, reads one line, and exits immediately either way.
"""

import socket
import sys

_TIMEOUT_SECONDS = 2.0


def probe(port: int, timeout: float = _TIMEOUT_SECONDS) -> bool:
    """Return True if the local Wyoming server on `port` answers Describe."""
    try:
        with socket.create_connection(("localhost", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b'{"type": "describe"}\n')
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError:
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
