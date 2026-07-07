"""Tests for wyoming_openrouter.probe."""

import json
import socket
import threading
import time

from wyoming_openrouter.probe import probe


def _build_event_bytes(event_type: str, data: dict) -> bytes:
    """Mimic wyoming.event.async_write_event's wire format: a JSON header
    line with a data_length field, followed by exactly that many bytes of
    separate JSON data (not newline-terminated, not part of the header
    line) -- matches the real protocol, not just an easy-to-grep fake.
    """
    data_bytes = json.dumps(data).encode("utf-8")
    header = json.dumps({"type": event_type, "data_length": len(data_bytes)}).encode()
    return header + b"\n" + data_bytes


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


def test_probe_returns_true_when_data_segment_matches():
    # "openrouter" only appears in the data segment (the attribution URL),
    # never in the header line itself -- matches the real server's actual
    # Info response shape.
    response = _build_event_bytes(
        "info",
        {"asr": [{"attribution": {"url": "https://openrouter.ai"}}]},
    )
    assert b"openrouter" not in response.split(b"\n", 1)[0]
    port, thread = _start_server(response)
    try:
        assert probe(port, timeout=1.0) is True
    finally:
        thread.join(timeout=1.0)


def test_probe_returns_false_when_data_segment_does_not_match():
    response = _build_event_bytes(
        "info", {"asr": [{"attribution": {"url": "https://example.com"}}]}
    )
    port, thread = _start_server(response)
    try:
        assert probe(port, timeout=1.0) is False
    finally:
        thread.join(timeout=1.0)


def test_probe_returns_false_when_nothing_is_listening():
    assert probe(59999, timeout=0.5) is False


def test_probe_returns_false_on_malformed_header():
    port, thread = _start_server(b"not json at all\n")
    try:
        assert probe(port, timeout=1.0) is False
    finally:
        thread.join(timeout=1.0)


def test_probe_is_fast_when_server_responds_immediately():
    """The whole point: no artificial idle-timeout floor like `nc -w N` has."""
    response = _build_event_bytes(
        "info", {"asr": [{"attribution": {"url": "https://openrouter.ai"}}]}
    )
    port, thread = _start_server(response)
    try:
        start = time.monotonic()
        assert probe(port, timeout=5.0) is True
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
    finally:
        thread.join(timeout=1.0)
