"""Tests for wyoming_openrouter.probe."""

import socket
import threading

from wyoming_openrouter.probe import probe


def _serve_once(sock: socket.socket, response: bytes, ready: threading.Event) -> None:
    sock.listen(1)
    ready.set()
    conn, _ = sock.accept()
    with conn:
        conn.recv(4096)
        conn.sendall(response)


def _start_server(response: bytes) -> tuple[int, threading.Thread]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("localhost", 0))
    port = sock.getsockname()[1]
    ready = threading.Event()
    thread = threading.Thread(
        target=_serve_once, args=(sock, response, ready), daemon=True
    )
    thread.start()
    ready.wait()
    return port, thread


def test_probe_returns_true_when_response_matches():
    port, thread = _start_server(b'{"type": "info", "data": "openrouter"}\n')
    try:
        assert probe(port, timeout=1.0) is True
    finally:
        thread.join(timeout=1.0)


def test_probe_returns_false_when_response_does_not_match():
    port, thread = _start_server(b'{"type": "info", "data": "someone-else"}\n')
    try:
        assert probe(port, timeout=1.0) is False
    finally:
        thread.join(timeout=1.0)


def test_probe_returns_false_when_nothing_is_listening():
    assert probe(59999, timeout=0.5) is False


def test_probe_is_fast_when_server_responds_immediately():
    """The whole point: no artificial idle-timeout floor like `nc -w N` has."""
    import time

    port, thread = _start_server(b'{"type": "info", "data": "openrouter"}\n')
    try:
        start = time.monotonic()
        assert probe(port, timeout=5.0) is True
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
    finally:
        thread.join(timeout=1.0)
