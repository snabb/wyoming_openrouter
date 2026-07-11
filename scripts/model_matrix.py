#!/usr/bin/env python3
"""End-to-end model matrix test for wyoming_openrouter.

For every OpenRouter TTS model paired with every OpenRouter STT model:
synthesize a fixed test phrase with the TTS model, transcribe the resulting
audio with the STT model (both via a real wyoming_openrouter server
process, over the actual Wyoming protocol -- not by calling OpenRouter
directly), and check whether the transcript matches the original phrase.
Produces a pass/fail matrix, a latency matrix, per-model cost/latency
summaries, and the total cost of the whole run.

Runs one wyoming_openrouter server process for the whole test: every STT and
TTS model in scope gets its own task/port in a single config file, so the
full catalog is tested with one server invocation rather than restarting
between batches.

Each TTS model is synthesized exactly ONCE (not once per STT partner) and
the resulting audio is reused for every STT model it's tested against --
audio only depends on (model, voice, phrase), not on which STT model comes
next, so with N_STT x N_TTS combinations this makes N_TTS synthesize calls
and N_STT x N_TTS transcribe calls, not N_STT x N_TTS of each.

Results (and synthesized audio) are cached to --cache-dir, keyed by the
test phrase, and written incrementally after every synthesize/transcribe --
if the script is interrupted (Ctrl-C, killed, crashes), re-running the same
command resumes from the cache instead of re-doing already-completed work
(and re-paying for it). Use --fresh to ignore/overwrite an existing cache.

Every TTS task is configured with audio_format=mp3 for uniform compatibility
across the whole catalog (not every model supports response_format=pcm --
see README.md), which means the `mpg123` binary must be installed on
whichever machine actually runs this script (not just inside the Docker
image, which already has it) -- e.g. `apt install mpg123` / `brew install
mpg123`. Without it every combination fails with "empty audio returned"
(confirmed while building this tool).

Costs real money against OPENROUTER_API_KEY (a full catalog run is
typically well under a few dollars; use --dry-run to preview scope, or
--stt-models/--tts-models to restrict it first). Not part of the test suite
or CI; run manually:

    OPENROUTER_API_KEY=... uv run python scripts/model_matrix.py
    uv run python scripts/model_matrix.py --dry-run
    uv run python scripts/model_matrix.py --stt-models openai/whisper-1,openai/gpt-4o-mini-transcribe
    uv run python scripts/model_matrix.py --tts-models hexgrad/kokoro-82m --phrase "Testing one two three."
    uv run python scripts/model_matrix.py --fresh  # ignore any existing cache, start over
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize, SynthesizeStopped
from wyoming_openrouter.openrouter import list_stt_models, list_tts_models
from wyoming_openrouter.util import slugify

DEFAULT_PHRASE = "The quick brown fox jumps over the lazy dog."
BASE_PORT = 20300  # arbitrary; each model in scope gets BASE_PORT + its index
READY_TIMEOUT = 30.0
SIMILARITY_THRESHOLD = 0.7
# Matches the server's own _resolve_cost retry budget (tts_handler.py) --
# waiting less than this would give up while the server is still legitimately
# retrying the generation-cost lookup.
COST_WAIT_RETRIES = 20
COST_WAIT_DELAY = 1.0

_RE_TRANSCRIBE = re.compile(
    r"Transcribe: model=(?P<model>\S+) language=\S+ audio=[\d.]+s "
    r"latency=(?P<latency>\d+)ms cost=\$(?P<cost>[\d.eE+-]+)"
)
_RE_SYNTH_COST = re.compile(
    r"Synthesize cost: model=(?P<model>\S+) cost=\$(?P<cost>[\d.eE+-]+)"
    r"(?P<estimated> \(estimated\))? \(generation_id=\S+\)"
)


@dataclass
class ComboResult:
    tts_model: str
    stt_model: str
    phrase: str
    transcript: str = ""
    similarity: float = 0.0
    success: bool = False
    error: Optional[str] = None
    tts_latency_ms: Optional[int] = None
    tts_cost: Optional[float] = None
    tts_cost_estimated: bool = False
    stt_latency_ms: Optional[int] = None
    stt_cost: Optional[float] = None

    @property
    def total_latency_ms(self) -> Optional[int]:
        if self.tts_latency_ms is None or self.stt_latency_ms is None:
            return None
        return self.tts_latency_ms + self.stt_latency_ms

    @property
    def total_cost(self) -> float:
        return (self.tts_cost or 0.0) + (self.stt_cost or 0.0)


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text)


def similarity_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def short_id(model_id: str) -> str:
    return model_id.split("/", 1)[-1] if "/" in model_id else model_id


def build_task_config(
    stt_models: list[dict], tts_models: list[dict], api_key: str
) -> tuple[dict, dict[str, int], dict[str, int], list[str]]:
    """Returns (config_dict, stt_ports_by_model, tts_ports_by_model, skipped_tts_model_ids)."""
    tasks = []
    port = BASE_PORT
    stt_ports: dict[str, int] = {}
    for stt_model in stt_models:
        tasks.append(
            {
                "name": slugify(f"stt-{stt_model['id']}"),
                "api_key": api_key,
                "type": "stt",
                "port": port,
                "model": stt_model["id"],
                "languages": ["en"],
            }
        )
        stt_ports[stt_model["id"]] = port
        port += 1

    tts_ports: dict[str, int] = {}
    skipped = []
    for tts_model in tts_models:
        voices = tts_model.get("supported_voices") or []
        if not voices:
            skipped.append(tts_model["id"])
            continue
        tasks.append(
            {
                "name": slugify(f"tts-{tts_model['id']}"),
                "api_key": api_key,
                "type": "tts",
                "port": port,
                "model": tts_model["id"],
                "voice": voices[0],
                # mp3 sidesteps "not every model supports response_format=pcm"
                # uniformly across the whole catalog (at the cost of a local
                # mpg123 decode pass) -- fine for a correctness-focused test.
                "audio_format": "mp3",
            }
        )
        tts_ports[tts_model["id"]] = port
        port += 1

    return {"tasks": tasks}, stt_ports, tts_ports, skipped


async def wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with AsyncTcpClient(host, port) as client:
                await client.write_event(Describe().event())
                event = await asyncio.wait_for(client.read_event(), timeout=3)
                if event is not None and Info.is_type(event.type):
                    return True
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(0.5)
    return False


async def do_synthesize(host: str, port: int, phrase: str, timeout: float):
    """Returns (pcm_bytes, rate, width, channels, elapsed_ms)."""
    async with AsyncTcpClient(host, port) as client:
        t0 = time.monotonic()
        await client.write_event(Synthesize(text=phrase).event())
        rate = width = channels = None
        chunks: list[bytes] = []
        while True:
            event = await asyncio.wait_for(client.read_event(), timeout=timeout)
            if event is None:
                raise RuntimeError("connection closed during synthesize")
            if AudioStart.is_type(event.type):
                start = AudioStart.from_event(event)
                rate, width, channels = start.rate, start.width, start.channels
            elif AudioChunk.is_type(event.type):
                chunks.append(AudioChunk.from_event(event).audio)
            elif SynthesizeStopped.is_type(event.type):
                break
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return b"".join(chunks), rate, width, channels, elapsed_ms


async def do_transcribe(
    host: str,
    port: int,
    pcm: bytes,
    rate: int,
    width: int,
    channels: int,
    timeout: float,
):
    async with AsyncTcpClient(host, port) as client:
        t0 = time.monotonic()
        await client.write_event(Transcribe(language="en").event())
        await client.write_event(
            AudioStart(rate=rate, width=width, channels=channels).event()
        )
        chunk_size = 4096
        for i in range(0, len(pcm), chunk_size):
            await client.write_event(
                AudioChunk(
                    audio=pcm[i : i + chunk_size],
                    rate=rate,
                    width=width,
                    channels=channels,
                ).event()
            )
        await client.write_event(AudioStop().event())
        event = await asyncio.wait_for(client.read_event(), timeout=timeout)
        if event is None:
            raise RuntimeError("connection closed during transcribe")
        transcript = Transcript.from_event(event)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return transcript.text, elapsed_ms


class LogCollector:
    """Reads a subprocess's merged stdout/stderr in the background.

    Cost/latency for a request is only visible in the server's own log lines
    (the Wyoming protocol itself carries no cost info), so this lets us
    scrape it out after each request rather than bypassing the protocol.
    """

    def __init__(self, process: "asyncio.subprocess.Process") -> None:
        self.process = process
        self.lines: list[str] = []
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.process.stdout is not None
        async for raw_line in self.process.stdout:
            self.lines.append(raw_line.decode(errors="replace").rstrip())

    def mark(self) -> int:
        return len(self.lines)

    def new_lines_since(self, mark: int) -> list[str]:
        return self.lines[mark:]

    async def stop(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def extract_stt_cost(
    lines: list[str], model: str
) -> tuple[Optional[int], Optional[float]]:
    for line in lines:
        m = _RE_TRANSCRIBE.search(line)
        if m and m.group("model") == model:
            return int(m.group("latency")), float(m.group("cost"))
    return None, None


def extract_tts_cost(lines: list[str], model: str) -> tuple[Optional[float], bool]:
    for line in lines:
        m = _RE_SYNTH_COST.search(line)
        if m and m.group("model") == model:
            return float(m.group("cost")), bool(m.group("estimated"))
    return None, False


def combo_key(tts_model: str, stt_model: str) -> str:
    return f"{tts_model}||{stt_model}"


def phrase_cache_dir(cache_root: Path, phrase: str) -> Path:
    """One subdirectory per distinct phrase, so switching --phrase never
    mixes incompatible cached audio/results together."""
    digest = hashlib.sha256(phrase.encode("utf-8")).hexdigest()[:12]
    return cache_root / digest


def load_audio_manifest(cache_dir: Path) -> dict[str, dict]:
    path = cache_dir / "audio_manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_audio_manifest(cache_dir: Path, manifest: dict[str, dict]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "audio_manifest.json").write_text(json.dumps(manifest, indent=2))


def audio_pcm_path(cache_dir: Path, tts_model: str) -> Path:
    return cache_dir / "audio" / f"{slugify(tts_model)}.pcm"


def load_cached_audio(
    cache_dir: Path, tts_model: str, manifest: dict[str, dict]
) -> Optional[tuple[bytes, int, int, int]]:
    entry = manifest.get(tts_model)
    path = audio_pcm_path(cache_dir, tts_model)
    if entry is None or not path.exists():
        return None
    return path.read_bytes(), entry["rate"], entry["width"], entry["channels"]


def save_audio_to_cache(
    cache_dir: Path,
    tts_model: str,
    pcm: bytes,
    rate: int,
    width: int,
    channels: int,
    latency_ms: int,
    cost: Optional[float],
    cost_estimated: bool,
    manifest: dict[str, dict],
) -> None:
    path = audio_pcm_path(cache_dir, tts_model)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm)
    manifest[tts_model] = {
        "rate": rate,
        "width": width,
        "channels": channels,
        "latency_ms": latency_ms,
        "cost": cost,
        "cost_estimated": cost_estimated,
    }
    save_audio_manifest(cache_dir, manifest)


def load_results_cache(cache_dir: Path) -> dict[str, dict]:
    path = cache_dir / "results.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_results_cache(cache_dir: Path, results_by_key: dict[str, dict]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "results.json").write_text(json.dumps(results_by_key, indent=2))


async def run_matrix(
    host: str,
    stt_models: list[dict],
    tts_models: list[dict],
    api_key: str,
    phrase: str,
    request_timeout: float,
    cache_dir: Path,
) -> list[ComboResult]:
    audio_manifest = load_audio_manifest(cache_dir)
    results_cache = load_results_cache(cache_dir)

    stt_by_id = {m["id"]: m for m in stt_models}
    tts_by_id = {m["id"]: m for m in tts_models}

    # Only reuse cache entries for the current stt/tts selection -- a prior
    # full run's cache is still valid input when --stt-models/--tts-models
    # narrows the scope, it just won't contribute entries outside it.
    results_by_key: dict[str, ComboResult] = {}
    pending: list[tuple[str, str]] = []
    for tts_id in tts_by_id:
        for stt_id in stt_by_id:
            key = combo_key(tts_id, stt_id)
            cached = results_cache.get(key)
            if cached is not None:
                results_by_key[key] = ComboResult(**cached)
            else:
                pending.append((tts_id, stt_id))

    total_combos = len(tts_by_id) * len(stt_by_id)
    if not pending:
        print(
            f"All {total_combos} combination(s) already cached in {cache_dir} -- nothing to run."
        )
        return list(results_by_key.values())

    print(
        f"{len(pending)}/{total_combos} combination(s) pending "
        f"({len(results_by_key)} already cached in {cache_dir})."
    )

    stt_ids_needed = sorted({stt for _, stt in pending})
    tts_ids_needing_audio = sorted(
        {tts for tts, _ in pending if tts not in audio_manifest}
    )
    stt_models_to_run = [stt_by_id[i] for i in stt_ids_needed]
    tts_models_to_synthesize = [tts_by_id[i] for i in tts_ids_needing_audio]

    config, stt_ports, tts_ports, skipped = build_task_config(
        stt_models_to_run, tts_models_to_synthesize, api_key
    )

    def _record(tts_id: str, stt_id: str, error: str) -> None:
        key = combo_key(tts_id, stt_id)
        r = ComboResult(tts_model=tts_id, stt_model=stt_id, phrase=phrase, error=error)
        results_by_key[key] = r
        results_cache[key] = asdict(r)

    for tts_id in skipped:
        print(f"  skipping {tts_id!r}: no supported_voices listed in catalog")
        for stt_id in stt_ids_needed:
            if (tts_id, stt_id) in pending:
                _record(
                    tts_id, stt_id, "skipped: no supported_voices listed in catalog"
                )
    if skipped:
        save_results_cache(cache_dir, results_cache)

    # stt_models_to_run is never empty here: pending is non-empty (checked
    # above) and every pending combo contributes its stt_id, so config always
    # has at least the STT tasks -- a subprocess is always needed below.
    assert config[
        "tasks"
    ], "internal error: no tasks to run despite pending combinations"

    process: Optional[asyncio.subprocess.Process] = None
    collector: Optional[LogCollector] = None
    config_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            config_path = f.name

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "wyoming_openrouter",
            "--config",
            config_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        collector = LogCollector(process)

        print(f"Waiting for {len(config['tasks'])} task(s) to become ready...")
        all_ports = list(stt_ports.values()) + list(tts_ports.values())
        for port in all_ports:
            if not await wait_for_port(host, port, READY_TIMEOUT):
                raise RuntimeError(f"server did not become ready on port {port}")

        # Synthesize each TTS model's audio exactly once, reused for every
        # STT partner below -- persisted to cache immediately so a later
        # interruption never forces re-synthesis.
        audio_by_model: dict[str, tuple[bytes, int, int, int]] = {}
        for tts_id, tts_port in tts_ports.items():
            print(f"Synthesizing {tts_id!r}...")
            try:
                mark = collector.mark()
                pcm, rate, width, channels, tts_elapsed = await do_synthesize(
                    host, tts_port, phrase, request_timeout
                )
                if not pcm or rate is None:
                    raise RuntimeError("empty audio returned")

                cost = None
                estimated = False
                for _ in range(COST_WAIT_RETRIES):
                    cost, estimated = extract_tts_cost(
                        collector.new_lines_since(mark), tts_id
                    )
                    if cost is not None:
                        break
                    await asyncio.sleep(COST_WAIT_DELAY)

                audio_by_model[tts_id] = (pcm, rate, width, channels)
                save_audio_to_cache(
                    cache_dir,
                    tts_id,
                    pcm,
                    rate,
                    width,
                    channels,
                    tts_elapsed,
                    cost,
                    estimated,
                    audio_manifest,
                )
                print(
                    f"  ok: {len(pcm)} byte(s), latency={tts_elapsed}ms, "
                    f"cost=${cost if cost is not None else 0:.6f}"
                    + (" (estimated)" if estimated else "")
                )
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                print(f"  FAILED: {err}")
                for stt_id in stt_ids_needed:
                    if (tts_id, stt_id) in pending:
                        _record(tts_id, stt_id, err)
                save_results_cache(cache_dir, results_cache)

        # Load audio for TTS models already cached from a prior run.
        for tts_id in {tts for tts, _ in pending} - set(audio_by_model) - set(skipped):
            cached_audio = load_cached_audio(cache_dir, tts_id, audio_manifest)
            if cached_audio is not None:
                audio_by_model[tts_id] = cached_audio

        remaining = [
            (tts_id, stt_id)
            for tts_id, stt_id in pending
            if combo_key(tts_id, stt_id) not in results_by_key
        ]
        total = len(remaining)
        for i, (tts_id, stt_id) in enumerate(remaining, 1):
            if tts_id not in audio_by_model:
                continue  # synthesis failed earlier; already recorded

            pcm, rate, width, channels = audio_by_model[tts_id]
            stt_port = stt_ports[stt_id]
            meta = audio_manifest.get(tts_id, {})
            result = ComboResult(
                tts_model=tts_id,
                stt_model=stt_id,
                phrase=phrase,
                tts_latency_ms=meta.get("latency_ms"),
                tts_cost=meta.get("cost"),
                tts_cost_estimated=bool(meta.get("cost_estimated", False)),
            )
            try:
                mark2 = collector.mark()
                transcript_text, stt_elapsed = await do_transcribe(
                    host, stt_port, pcm, rate, width, channels, request_timeout
                )
                await asyncio.sleep(0.3)  # let the server finish its own log line
                stt_latency, stt_cost = extract_stt_cost(
                    collector.new_lines_since(mark2), stt_id
                )
                result.transcript = transcript_text
                result.stt_latency_ms = (
                    stt_latency if stt_latency is not None else stt_elapsed
                )
                result.stt_cost = stt_cost
                result.similarity = similarity_ratio(phrase, transcript_text)
                result.success = result.similarity >= SIMILARITY_THRESHOLD
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"

            key = combo_key(tts_id, stt_id)
            results_by_key[key] = result
            results_cache[key] = asdict(result)
            save_results_cache(cache_dir, results_cache)

            status = "PASS" if result.success else ("ERROR" if result.error else "FAIL")
            print(
                f"  [{i}/{total}] {tts_id} -> {stt_id}: {status} "
                f"(similarity={result.similarity:.2f}, "
                f"latency={result.total_latency_ms}ms, cost=${result.total_cost:.6f}"
                + (f", error={result.error}" if result.error else "")
                + ")"
            )
    finally:
        if collector is not None:
            await collector.stop()
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if config_path is not None:
            Path(config_path).unlink(missing_ok=True)

    return list(results_by_key.values())


def _avg(values: list[float]) -> Optional[float]:
    return statistics.mean(values) if values else None


@dataclass
class LatencyStats:
    mean: float
    min: float
    max: float

    @staticmethod
    def compute(values: list[float]) -> "Optional[LatencyStats]":
        if not values:
            return None
        return LatencyStats(
            mean=statistics.mean(values), min=min(values), max=max(values)
        )

    def format(self) -> str:
        return f"{self.mean:.0f} / {self.min:.0f} / {self.max:.0f}"


def build_report(
    results: list[ComboResult], phrase: str, elapsed_s: float
) -> tuple[str, dict]:
    tts_ids = sorted({r.tts_model for r in results})
    stt_ids = sorted({r.stt_model for r in results})
    by_pair = {(r.tts_model, r.stt_model): r for r in results}

    total_cost = sum(r.total_cost for r in results)
    unknown_cost_count = sum(
        1
        for r in results
        if r.error is None and (r.tts_cost is None or r.stt_cost is None)
    )
    passed = sum(1 for r in results if r.success)
    errored = sum(1 for r in results if r.error)

    lines = [
        "# Wyoming OpenRouter Model Matrix Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Test phrase: `{phrase}`",
        f"Wall-clock run time: {elapsed_s:.0f}s",
        f"Total combinations: {len(results)}",
        f"Passed: {passed} / {len(results)}  |  Errored: {errored}",
        f"**Total cost of this test run: ${total_cost:.6f}**"
        + (
            f" (excludes {unknown_cost_count} combination(s) with unresolved cost)"
            if unknown_cost_count
            else ""
        ),
        "",
        "## Pass/fail matrix (rows: TTS model, columns: STT model)",
        "",
        "Cell shows `<result> <similarity>` -- ✓ pass, ✗ fail (below "
        f"similarity {SIMILARITY_THRESHOLD}), ERR on request failure, `-` not tested.",
        "",
    ]

    header = "| TTS \\ STT | " + " | ".join(short_id(s) for s in stt_ids) + " |"
    sep = "|---|" + "---|" * len(stt_ids)
    lines += [header, sep]
    for tts_id in tts_ids:
        row = [f"**{short_id(tts_id)}**"]
        for stt_id in stt_ids:
            r = by_pair.get((tts_id, stt_id))
            if r is None:
                row.append("-")
            elif r.error:
                row.append("ERR")
            elif r.success:
                row.append(f"✓ {r.similarity:.2f}")
            else:
                row.append(f"✗ {r.similarity:.2f}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines += [
        "## Latency matrix (ms, TTS + STT combined round trip)",
        "",
        header,
        sep,
    ]
    for tts_id in tts_ids:
        row = [f"**{short_id(tts_id)}**"]
        for stt_id in stt_ids:
            r = by_pair.get((tts_id, stt_id))
            if r is None or r.total_latency_ms is None:
                row.append("-")
            else:
                row.append(str(r.total_latency_ms))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines += [
        "## Per-TTS-model summary",
        "",
        "| Model | Success rate | Avg cost | Latency ms (mean / min / max) |",
        "|---|---|---|---|",
    ]
    for tts_id in tts_ids:
        rows = [r for r in results if r.tts_model == tts_id]
        succ = sum(1 for r in rows if r.success)
        avg_cost = _avg([r.tts_cost for r in rows if r.tts_cost is not None])
        lat_stats = LatencyStats.compute(
            [r.tts_latency_ms for r in rows if r.tts_latency_ms is not None]
        )
        lines.append(
            f"| {tts_id} | {succ}/{len(rows)} | "
            f"{f'${avg_cost:.6f}' if avg_cost is not None else 'n/a'} | "
            f"{lat_stats.format() if lat_stats is not None else 'n/a'} |"
        )
    lines.append("")

    lines += [
        "## Per-STT-model summary",
        "",
        "| Model | Success rate | Avg cost | Latency ms (mean / min / max) |",
        "|---|---|---|---|",
    ]
    for stt_id in stt_ids:
        rows = [r for r in results if r.stt_model == stt_id]
        succ = sum(1 for r in rows if r.success)
        avg_cost = _avg([r.stt_cost for r in rows if r.stt_cost is not None])
        lat_stats = LatencyStats.compute(
            [r.stt_latency_ms for r in rows if r.stt_latency_ms is not None]
        )
        lines.append(
            f"| {stt_id} | {succ}/{len(rows)} | "
            f"{f'${avg_cost:.6f}' if avg_cost is not None else 'n/a'} | "
            f"{lat_stats.format() if lat_stats is not None else 'n/a'} |"
        )
    lines.append("")

    failures = [r for r in results if not r.success]
    if failures:
        lines.append("## Failures / errors")
        lines.append("")
        for r in failures:
            lines.append(f"### {r.tts_model} → {r.stt_model}")
            if r.error:
                lines.append(f"- Error: {r.error}")
            else:
                lines.append(f"- Similarity: {r.similarity:.2f}")
                lines.append(f"- Expected: `{phrase}`")
                lines.append(f"- Got: `{r.transcript}`")
            lines.append("")

    markdown = "\n".join(lines)
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phrase": phrase,
        "elapsed_s": elapsed_s,
        "total_cost": total_cost,
        "passed": passed,
        "total": len(results),
        "results": [asdict(r) for r in results],
    }
    return markdown, data


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--phrase", default=DEFAULT_PHRASE)
    parser.add_argument(
        "--stt-models", help="Comma-separated STT model ids to test (default: all)"
    )
    parser.add_argument(
        "--tts-models", help="Comma-separated TTS model ids to test (default: all)"
    )
    parser.add_argument("--api-key", default=os.environ.get("OPENROUTER_API_KEY"))
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="Per-request timeout (seconds)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--output", default="model_matrix_report.md")
    parser.add_argument(
        "--cache-dir",
        default=".model_matrix_cache",
        help="Where to persist results/audio so an interrupted run can resume (default: .model_matrix_cache)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore/overwrite any existing cache for this phrase",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list the models/combos that would be tested",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.api_key:
        print("ERROR: set OPENROUTER_API_KEY or pass --api-key", file=sys.stderr)
        sys.exit(1)

    print("Fetching live OpenRouter STT/TTS model catalogs...")
    stt_models = await asyncio.to_thread(list_stt_models, args.api_key)
    tts_models = await asyncio.to_thread(list_tts_models, args.api_key)

    if args.stt_models:
        wanted = set(args.stt_models.split(","))
        stt_models = [m for m in stt_models if m["id"] in wanted]
    if args.tts_models:
        wanted = set(args.tts_models.split(","))
        tts_models = [m for m in tts_models if m["id"] in wanted]

    total_combos = len(stt_models) * len(tts_models)
    print(
        f"{len(stt_models)} STT model(s) x {len(tts_models)} TTS model(s) = {total_combos} combination(s)"
    )
    for m in stt_models:
        print(f"  STT: {m['id']}")
    for m in tts_models:
        print(f"  TTS: {m['id']}")

    if args.dry_run:
        print("\n--dry-run: no requests made.")
        return

    if total_combos == 0:
        print("Nothing to test.", file=sys.stderr)
        sys.exit(1)

    cache_dir = phrase_cache_dir(Path(args.cache_dir), args.phrase)
    if args.fresh and cache_dir.exists():
        print(f"--fresh: removing existing cache at {cache_dir}")
        shutil.rmtree(cache_dir)

    t_start = time.monotonic()
    results = await run_matrix(
        args.host,
        stt_models,
        tts_models,
        args.api_key,
        args.phrase,
        args.timeout,
        cache_dir,
    )
    elapsed_s = time.monotonic() - t_start

    markdown, data = build_report(results, args.phrase, elapsed_s)
    Path(args.output).write_text(markdown)
    json_path = Path(args.output).with_suffix(".json")
    json_path.write_text(json.dumps(data, indent=2))

    passed = sum(1 for r in results if r.success)
    total_cost = sum(r.total_cost for r in results)
    print(f"\nReport written to {args.output} and {json_path}")
    print(
        f"{passed}/{len(results)} combinations passed. Total cost: ${total_cost:.6f}. Took {elapsed_s:.0f}s."
    )


if __name__ == "__main__":
    asyncio.run(main())
