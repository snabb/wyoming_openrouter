#!/usr/bin/env python3
"""Record one utterance from the local microphone and transcribe it with
every configured OpenRouter STT model (and optionally remote Wyoming STT
servers), printing text/latency/cost/usage per model side by side.

Calls OpenRouter's transcription API directly (this project's own
openrouter.transcribe(), the same client wyoming_openrouter uses) for
MODELS. Costs real money against OPENROUTER_API_KEY (one transcription
call per model). Optionally also speaks the real Wyoming ASR protocol
directly to any host:port servers in WYOMING_ENDPOINTS (e.g. a
self-hosted wyoming-whisper add-on) -- no Home Assistant or Docker
involved for either path. Wyoming endpoints have no per-request price, so
their cost column always reads "N/A".

Recording uses `rec` (part of SoX). By default it stops on whichever comes
first: ~2s of silence after speech is detected, or pressing Enter -- needs
a real interactive terminal (run this directly, not through something else
driving it, e.g. an agent). Pass --duration <seconds> for a fixed-length
recording instead, with no silence detection and no Enter-to-stop.

Plays the recording back (via SoX's `play`) before transcribing it, so you
can confirm it captured what you meant to say before spending API calls --
pass --no-playback to skip that.

Edit MODELS/WYOMING_ENDPOINTS below to change what's compared, or pass
--models / --wyoming as comma-separated overrides.

Usage:
    OPENROUTER_API_KEY=... uv run python3 scripts/stt_compare.py
    OPENROUTER_API_KEY=... uv run python3 scripts/stt_compare.py --duration 6
    OPENROUTER_API_KEY=... uv run python3 scripts/stt_compare.py --models openai/whisper-1,openai/gpt-4o-mini-transcribe
    OPENROUTER_API_KEY=... uv run python3 scripts/stt_compare.py --wyoming 192.0.2.10:10400
    OPENROUTER_API_KEY=... uv run python3 scripts/stt_compare.py --wav existing.wav
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wyoming.asr import Transcribe, Transcript  # noqa: E402
from wyoming.audio import AudioChunk, AudioStart, AudioStop  # noqa: E402
from wyoming.client import AsyncTcpClient  # noqa: E402
from wyoming_openrouter.openrouter import (  # noqa: E402
    OpenRouterError,
    transcribe,
)

# Comma-separated host:port entries for remote Wyoming STT servers to
# compare against alongside the OpenRouter models above -- e.g. a
# self-hosted wyoming-whisper add-on. Edit this list, or pass --wyoming as
# a comma-separated override. These have no per-request price (self-hosted,
# not billed), so their cost column always reads "N/A".
WYOMING_ENDPOINTS: list[str] = []

_WYOMING_TIMEOUT_SECONDS = 30.0
_WYOMING_CHUNK_BYTES = 4096

# Edit this list to change which STT models are compared by default.
MODELS = [
    "openai/gpt-4o-mini-transcribe",
    "openai/gpt-4o-transcribe",
    "openai/whisper-1",
    "openai/whisper-large-v3",
    "openai/whisper-large-v3-turbo",
    "microsoft/mai-transcribe-1.5",
    "nvidia/parakeet-tdt-0.6b-v3",
    "mistralai/voxtral-mini-transcribe",
    "qwen/qwen3-asr-flash-2026-02-10",
    "google/chirp-3",
]

SAMPLE_RATE = 16000
# sox `silence` effect args: stop after SILENCE_DURATION seconds below
# SILENCE_THRESHOLD, once at least ONSET_THRESHOLD of sound has been seen.
ONSET_THRESHOLD = "2%"
SILENCE_DURATION = "2.0"
SILENCE_THRESHOLD = "2%"


def record_utterance(path: Path, duration: float | None) -> None:
    """Record from the default mic.

    duration=None: stop on whichever comes first -- ~2s of silence after
    speech is detected, or pressing Enter. Requires an interactive terminal
    (a real stdin) and a mic noise floor comfortably under the silence
    threshold; tune ONSET_THRESHOLD/SILENCE_THRESHOLD above if it never
    stops, or if it cuts off while you're still talking.

    duration=<seconds>: fixed-length recording, no silence detection and no
    Enter-to-stop -- use this when nothing can send this process a real
    keypress (e.g. driven by an agent/script rather than a person at a
    terminal), or when the silence-detection mode isn't reliable on this
    mic.
    """
    if duration is not None:
        cmd = [
            "rec",
            "-q",
            "-r",
            str(SAMPLE_RATE),
            "-c",
            "1",
            "-b",
            "16",
            str(path),
            "trim",
            "0",
            str(duration),
        ]
        print(f"Recording for {duration:.1f}s -- speak now.")
        subprocess.run(cmd, check=True)
        print("Recording finished.\n")
        return

    cmd = [
        "rec",
        "-q",
        "-r",
        str(SAMPLE_RATE),
        "-c",
        "1",
        "-b",
        "16",
        str(path),
        "silence",
        "1",
        "0.1",
        ONSET_THRESHOLD,
        "1",
        SILENCE_DURATION,
        SILENCE_THRESHOLD,
    ]
    print(
        "Recording... speak now.\n"
        f"Stops automatically after ~{SILENCE_DURATION}s of silence, "
        "or press Enter to stop early."
    )
    proc = subprocess.Popen(cmd)

    stop_event = threading.Event()

    def wait_for_enter() -> None:
        try:
            input()
        except EOFError:
            return
        stop_event.set()

    input_thread = threading.Thread(target=wait_for_enter, daemon=True)
    input_thread.start()

    while proc.poll() is None:
        if stop_event.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        time.sleep(0.1)

    print("Recording finished.\n")


def play_audio(path: Path) -> None:
    """Play the recording back locally so you can confirm it captured what
    you meant to say before spending API calls on it."""
    print("Playing back recording...")
    subprocess.run(["play", "-q", str(path)], check=True)
    print("Playback finished.\n")


def _describe_exc(exc: BaseException) -> str:
    """str(exc) is empty for some exception types (notably TimeoutError --
    str(TimeoutError()) == ""), which would otherwise produce a falsy
    "error" value that gets misread as "no error" downstream."""
    return str(exc) or f"{type(exc).__name__} (no message)"


def transcribe_one(
    api_key: str, model: str, wav_bytes: bytes, language: str | None
) -> dict:
    try:
        result = transcribe(api_key, model, wav_bytes, language=language)
        return {
            "model": model,
            "text": result.text,
            "cost": result.cost,
            "latency_ms": result.elapsed_ms,
            "usage": result.usage,
            "error": None,
        }
    except OpenRouterError as exc:
        return {
            "model": model,
            "text": None,
            "cost": None,
            "latency_ms": None,
            "usage": None,
            "error": _describe_exc(exc),
        }


def _wav_pcm(wav_bytes: bytes) -> tuple[bytes, int, int, int]:
    """Return (pcm_bytes, rate, width, channels) from a WAV file's actual
    header, rather than assuming SAMPLE_RATE/16-bit/mono -- a file passed
    via --wav might not match what this script itself records."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        rate = wav_file.getframerate()
        width = wav_file.getsampwidth()
        channels = wav_file.getnchannels()
        pcm = wav_file.readframes(wav_file.getnframes())
    return pcm, rate, width, channels


async def _wyoming_transcribe_async(
    host: str, port: int, wav_bytes: bytes, language: str | None
) -> tuple[str, int]:
    """Speak the real Wyoming ASR protocol to a remote STT server: Transcribe,
    AudioStart, AudioChunk(s), AudioStop, then read events until a Transcript
    arrives -- the same sequence Home Assistant's own wyoming/stt.py uses."""
    pcm, rate, width, channels = _wav_pcm(wav_bytes)
    start = time.monotonic()
    async with AsyncTcpClient(host, port) as client:
        await asyncio.wait_for(
            client.write_event(Transcribe(language=language).event()),
            _WYOMING_TIMEOUT_SECONDS,
        )
        await asyncio.wait_for(
            client.write_event(
                AudioStart(rate=rate, width=width, channels=channels).event()
            ),
            _WYOMING_TIMEOUT_SECONDS,
        )
        for i in range(0, len(pcm), _WYOMING_CHUNK_BYTES):
            chunk = AudioChunk(
                rate=rate,
                width=width,
                channels=channels,
                audio=pcm[i : i + _WYOMING_CHUNK_BYTES],
            )
            await asyncio.wait_for(
                client.write_event(chunk.event()), _WYOMING_TIMEOUT_SECONDS
            )
        await asyncio.wait_for(
            client.write_event(AudioStop().event()), _WYOMING_TIMEOUT_SECONDS
        )

        while True:
            event = await asyncio.wait_for(
                client.read_event(), _WYOMING_TIMEOUT_SECONDS
            )
            if event is None:
                raise RuntimeError("connection closed before a Transcript arrived")
            if Transcript.is_type(event.type):
                text = Transcript.from_event(event).text
                break

    latency_ms = int((time.monotonic() - start) * 1000)
    return text, latency_ms


def wyoming_transcribe_one(
    endpoint: str, wav_bytes: bytes, language: str | None
) -> dict:
    """Like transcribe_one, but for a remote Wyoming STT server instead of
    an OpenRouter model -- cost is always None (shown as "N/A"), since a
    self-hosted server has no per-request price."""
    label = f"wyoming:{endpoint}"
    host, _, port_str = endpoint.rpartition(":")
    try:
        port = int(port_str)
        text, latency_ms = asyncio.run(
            _wyoming_transcribe_async(host, port, wav_bytes, language)
        )
        return {
            "model": label,
            "text": text,
            "cost": None,
            "latency_ms": latency_ms,
            "usage": None,
            "error": None,
        }
    except Exception as exc:
        return {
            "model": label,
            "text": None,
            "cost": None,
            "latency_ms": None,
            "usage": None,
            "error": _describe_exc(exc),
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--models",
        help="Comma-separated model list, overriding the MODELS constant in this file",
    )
    parser.add_argument(
        "--wyoming",
        help=(
            "Comma-separated host:port list of remote Wyoming STT servers "
            "to compare alongside the OpenRouter models, overriding the "
            "WYOMING_ENDPOINTS constant in this file (e.g. "
            "192.0.2.10:10400)"
        ),
    )
    parser.add_argument(
        "--wav",
        help="Use an existing WAV file instead of recording from the mic",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help=(
            "Fixed recording length in seconds instead of silence/Enter "
            "detection (no interactive terminal needed)"
        ),
    )
    parser.add_argument(
        "--no-playback",
        action="store_true",
        help="Skip playing the recording back before transcribing it",
    )
    parser.add_argument(
        "--language",
        default="en",
        help=(
            "Language hint sent to every model (ISO-639-1, e.g. 'en') -- "
            "matches what the real deployed server always sends from its "
            "configured task, so this is on by default. Without it, some "
            "models can auto-detect the wrong language for a short/ambiguous "
            "clip and transcribe (or effectively translate) into that "
            "instead. Pass --language '' to test auto-detect on purpose."
        ),
    )
    args = parser.parse_args()
    language = args.language or None

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: set OPENROUTER_API_KEY", file=sys.stderr)
        sys.exit(1)

    models = args.models.split(",") if args.models else MODELS
    wyoming_endpoints = args.wyoming.split(",") if args.wyoming else WYOMING_ENDPOINTS

    if args.wav:
        wav_path = Path(args.wav)
        wav_bytes = wav_path.read_bytes()
        if not args.no_playback:
            play_audio(wav_path)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "utterance.wav"
            record_utterance(wav_path, args.duration)
            wav_bytes = wav_path.read_bytes()
            if not args.no_playback:
                play_audio(wav_path)

    total_targets = len(models) + len(wyoming_endpoints)
    print(
        f"Recorded {len(wav_bytes)} bytes. Transcribing with {total_targets} "
        f"target(s) ({len(models)} OpenRouter, {len(wyoming_endpoints)} Wyoming)...\n"
    )

    results = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(total_targets, 1)
    ) as pool:
        futures = [
            pool.submit(transcribe_one, api_key, model, wav_bytes, language)
            for model in models
        ]
        futures.extend(
            pool.submit(wyoming_transcribe_one, endpoint, wav_bytes, language)
            for endpoint in wyoming_endpoints
        )
        for future in futures:
            results.append(future.result())

    name_width = max(len(r["model"]) for r in results) + 2
    print(f"{'Model':<{name_width}} {'Latency(ms)':>12} {'Cost(USD)':>12}  Text")
    print("-" * (name_width + 30))
    total_cost = 0.0
    for r in results:
        if r["error"] is not None:
            print(f"{r['model']:<{name_width}} {'ERROR':>12} {'':>12}  {r['error']}")
        else:
            cost_str = "N/A"
            if r["cost"] is not None:
                total_cost += r["cost"]
                cost_str = f"{r['cost']:.6f}"
            print(
                f"{r['model']:<{name_width}} {r['latency_ms']:>12} "
                f"{cost_str:>12}  {r['text']!r}"
            )

    print(
        f"\nTotal cost: ${total_cost:.6f} (Wyoming endpoints excluded -- no price data)"
    )
    print("\nRaw usage per model:")
    for r in results:
        if r["error"] is None and r["usage"] is not None:
            print(f"  {r['model']}: {r['usage']}")


if __name__ == "__main__":
    main()
