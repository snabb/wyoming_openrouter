"""Decode a stream of MP3 bytes to raw PCM via the mpg123 CLI.

mpg123 is a small (~1.7 MB with its runtime deps), purpose-built MPEG audio
decoder -- chosen over installing a full ffmpeg (which pulls in a much
larger codec/container library set this project would never otherwise use)
purely to support the occasional TTS model that only offers
response_format=mp3 (Wyoming itself always needs raw PCM either way).
"""

import subprocess
import threading
from typing import Iterator

# Forced regardless of the source mp3's actual encoding, so callers know the
# final PCM format upfront without parsing mpg123's own output. mpg123's -s
# raw-to-stdout mode is always 16-bit signed native-endian.
DECODED_RATE = 24000
DECODED_WIDTH = 2
DECODED_CHANNELS = 1

_READ_CHUNK_BYTES = 4096


def decode_mp3_stream(mp3_chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Pipe mp3_chunks into an mpg123 subprocess, yielding decoded PCM chunks
    as they become available.

    mpg123 is a genuinely streaming decoder (built for real-time playback of
    internet radio, etc.), so output starts flowing before all input has
    arrived -- this doesn't force buffering the whole clip, mirroring the
    incremental-delivery goal of the pcm path.
    """
    process = subprocess.Popen(
        ["mpg123", "-q", "-s", "-r", str(DECODED_RATE), "-m", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    # Never None: guaranteed by passing stdin=PIPE/stdout=PIPE above. Captured
    # into locals (rather than referencing process.stdin/.stdout again below)
    # so both the type checker and the _feed closure see the narrowed,
    # definitely-not-None type.
    stdin = process.stdin
    stdout = process.stdout
    assert stdin is not None
    assert stdout is not None

    def _feed() -> None:
        try:
            for chunk in mp3_chunks:
                stdin.write(chunk)
                stdin.flush()
        except (BrokenPipeError, OSError):
            # mpg123 exited early (e.g. decode error) -- the read loop below
            # will see EOF on stdout and stop; nothing more to feed it.
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()

    try:
        while True:
            chunk = stdout.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        stdout.close()
        feeder.join(timeout=5)
        process.wait(timeout=5)
