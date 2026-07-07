#!/usr/bin/env python3
"""Wyoming server for OpenRouter speech-to-text and text-to-speech."""

import argparse
import asyncio
import logging
import sys
from functools import partial
from typing import cast

import requests
from wyoming.server import AsyncTcpServer

from . import __version__
from .config import ConfigError, load_config
from .ha_metrics import Metrics
from .openrouter import (
    build_price_per_char_table,
    describe_stt_price,
    describe_tts_price,
    list_stt_models,
    list_tts_models,
)
from .stt_handler import OpenRouterSttEventHandler, get_stt_wyoming_info
from .tts_handler import OpenRouterTtsEventHandler, get_tts_wyoming_info

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/data/options.json"


async def main() -> None:
    """Run the Wyoming OpenRouter server."""
    parser = argparse.ArgumentParser(
        description="Wyoming server for OpenRouter speech-to-text and text-to-speech"
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to the tasks config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="version", version=__version__)

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    _LOGGER.info("Starting Wyoming OpenRouter server v%s", __version__)

    try:
        tasks = load_config(args.config)
    except (ConfigError, OSError) as exc:
        _LOGGER.critical("Invalid configuration (%s): %s", args.config, exc)
        sys.exit(1)

    _LOGGER.info(
        "Configured %d task(s): %s",
        len(tasks),
        ", ".join(f"{t.name} ({t.type}:{t.port})" for t in tasks),
    )

    # Populated in place once the TTS catalog fetch below completes (used as
    # a fallback cost estimate -- see tts_handler.py); starts empty rather
    # than being awaited up front, since every task's port binds below
    # without waiting on it.
    tts_pricing: dict[str, float] = {}

    # Logged, not awaited, before any task's port binds below: catalog
    # fetching is purely informational (never required for the server to
    # function) and was previously blocking every task's port from binding
    # until both catalog HTTP calls finished -- directly extending the
    # container's healthcheck start-period for a slow/timed-out OpenRouter
    # response, for zero benefit. Both run as TaskGroup members alongside the
    # per-task servers instead, so a slow catalog fetch no longer delays
    # startup at all. No auth needed -- OpenRouter's /models listing is
    # public.
    async def _log_stt_catalog() -> None:
        try:
            stt_catalog = await asyncio.to_thread(list_stt_models)
            stt_line = ", ".join(
                f"{m['id']} ({describe_stt_price(m)})" for m in stt_catalog
            )
            _LOGGER.info(
                "Live OpenRouter STT model catalog (a model's real per-request "
                "cost always comes from its response, not this catalog; "
                "duration-unit prices are per-second for some models and "
                "per-minute for others, with no reliable way to tell which "
                "from here -- see the model's OpenRouter page): %s",
                stt_line or "<none returned>",
            )
        except Exception as exc:
            # A network hiccup (e.g. a slow/timed-out response from
            # OpenRouter's public /models endpoint) is expected often enough
            # that a full traceback here would look like a real failure when
            # it isn't -- startup continues regardless. Keep the traceback
            # only for anything other than a plain request failure, since
            # that's actually unexpected.
            _LOGGER.warning(
                "Could not fetch the live OpenRouter STT model catalog: %s "
                "(startup continues regardless)",
                exc,
                exc_info=not isinstance(exc, requests.exceptions.RequestException),
            )

    async def _log_tts_catalog_and_update_pricing() -> None:
        try:
            tts_catalog = await asyncio.to_thread(list_tts_models)
            tts_line = ", ".join(
                f"{m['id']} ({describe_tts_price(m)})" for m in tts_catalog
            )
            _LOGGER.info(
                "Live OpenRouter TTS model catalog: %s", tts_line or "<none returned>"
            )
            tts_pricing.update(build_price_per_char_table(tts_catalog))
        except Exception as exc:
            _LOGGER.warning(
                "Could not fetch the live OpenRouter TTS model catalog: %s "
                "(startup continues regardless)",
                exc,
                exc_info=not isinstance(exc, requests.exceptions.RequestException),
            )

    # Bind all interfaces (IPv4 + IPv6) when host is the wildcard: Home
    # Assistant's hassio network is dual-stack and may resolve the add-on to an
    # IPv6 address, so an IPv4-only socket would be unreachable. host=None makes
    # asyncio listen on every address family. asyncio accepts host=None at
    # runtime even though wyoming types the parameter as str.
    bind_host = None if args.host in ("", "0.0.0.0", "::") else args.host

    # TaskGroup (not gather): if one task's port is already in use, the whole
    # process fails fast via structured-concurrency cancellation rather than
    # silently running a partial set of tasks -- the right behavior for a
    # supervised container process. The two catalog tasks never raise (both
    # catch everything internally), so they can't trigger that cancellation.
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_log_stt_catalog(), name="stt-catalog-log")
        tg.create_task(_log_tts_catalog_and_update_pricing(), name="tts-catalog-log")
        for task in tasks:
            metrics = Metrics(task_type=task.type, task_slug=task.slug)
            if task.type == "stt":
                wyoming_info = get_stt_wyoming_info(task)
                handler_factory = partial(
                    OpenRouterSttEventHandler, wyoming_info, task, metrics
                )
            else:
                wyoming_info = get_tts_wyoming_info(task)
                handler_factory = partial(
                    OpenRouterTtsEventHandler,
                    wyoming_info,
                    task,
                    metrics,
                    tts_pricing,
                )

            server = AsyncTcpServer(host=cast("str", bind_host), port=task.port)
            _LOGGER.info(
                "Task '%s' (%s) listening on %s:%d, model=%s",
                task.name,
                task.type,
                args.host,
                task.port,
                task.model,
            )
            tg.create_task(server.run(handler_factory), name=f"wyoming-{task.slug}")


def run() -> None:
    """Entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
