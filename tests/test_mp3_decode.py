"""Tests for wyoming_openrouter.mp3_decode."""

from unittest.mock import MagicMock, patch

from wyoming_openrouter.mp3_decode import (
    DECODED_CHANNELS,
    DECODED_RATE,
    DECODED_WIDTH,
    decode_mp3_stream,
)


def _fake_process(stdout_chunks):
    process = MagicMock()
    process.stdin = MagicMock()
    stdout_iter = iter(stdout_chunks + [b""])  # b"" signals EOF to .read()
    process.stdout = MagicMock()
    process.stdout.read.side_effect = lambda _n: next(stdout_iter)
    process.wait.return_value = 0
    return process


def test_decode_mp3_stream_invokes_mpg123_with_fixed_output_format():
    process = _fake_process([b"\x00\x01\x02\x03"])
    with patch(
        "wyoming_openrouter.mp3_decode.subprocess.Popen", return_value=process
    ) as mock_popen:
        list(decode_mp3_stream(iter([b"id3...", b"mp3data"])))

    args, kwargs = mock_popen.call_args
    command = args[0]
    assert command[0] == "mpg123"
    assert "-r" in command
    assert str(DECODED_RATE) in command
    assert "-m" in command  # force mono
    assert kwargs["stdin"] is not None
    assert kwargs["stdout"] is not None


def test_decode_mp3_stream_feeds_input_and_yields_decoded_output():
    process = _fake_process([b"\x00\x01", b"\x02\x03"])
    with patch("wyoming_openrouter.mp3_decode.subprocess.Popen", return_value=process):
        chunks = list(decode_mp3_stream(iter([b"mp3-bytes-1", b"mp3-bytes-2"])))

    assert chunks == [b"\x00\x01", b"\x02\x03"]
    write_calls = [call.args[0] for call in process.stdin.write.call_args_list]
    assert write_calls == [b"mp3-bytes-1", b"mp3-bytes-2"]
    process.stdin.close.assert_called_once()
    process.stdout.close.assert_called_once()
    process.wait.assert_called_once()


def test_decode_mp3_stream_stops_cleanly_when_input_iterator_is_empty():
    process = _fake_process([])
    with patch("wyoming_openrouter.mp3_decode.subprocess.Popen", return_value=process):
        chunks = list(decode_mp3_stream(iter([])))

    assert chunks == []


def test_decoded_constants_are_16bit_mono():
    assert DECODED_WIDTH == 2
    assert DECODED_CHANNELS == 1
    assert DECODED_RATE == 24000
