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

    # Logged unconditionally, before config validation: the default shipped
    # config (a placeholder task with no api_key) fails validation below, and
    # users are meant to pick a `model` slug by reading this log -- it must
    # not be skipped just because the config isn't valid (or isn't filled in)
    # yet. Best-effort only, so a fetch failure never blocks startup. No auth
    # needed -- OpenRouter's /models listing is public.
    try:
        stt_catalog = await asyncio.to_thread(list_stt_models)
        stt_line = ", ".join(
            f"{m['id']} ({describe_stt_price(m)})" for m in stt_catalog
        )
        _LOGGER.info(
            "Live OpenRouter STT model catalog (a model's real per-request "
            "cost always comes from its response, not this catalog; "
            "duration-unit prices are per-second for some models and "
            "per-minute for others, with no reliable way to tell which from "
            "here -- see the model's OpenRouter page): %s",
            stt_line or "<none returned>",
        )
    except Exception as exc:
        # A network hiccup (e.g. a slow/timed-out response from OpenRouter's
        # public /models endpoint) is expected often enough that a full
        # traceback here would look like a real failure when it isn't --
        # startup continues regardless. Keep the traceback only for anything
        # other than a plain request failure, since that's actually unexpected.
        _LOGGER.warning(
            "Could not fetch the live OpenRouter STT model catalog: %s "
            "(startup continues regardless)",
            exc,
            exc_info=not isinstance(exc, requests.exceptions.RequestException),
        )

    tts_catalog: list = []
    try:
        tts_catalog = await asyncio.to_thread(list_tts_models)
        tts_line = ", ".join(
            f"{m['id']} ({describe_tts_price(m)})" for m in tts_catalog
        )
        _LOGGER.info(
            "Live OpenRouter TTS model catalog: %s", tts_line or "<none returned>"
        )
    except Exception as exc:
        _LOGGER.warning(
            "Could not fetch the live OpenRouter TTS model catalog: %s "
            "(startup continues regardless)",
            exc,
            exc_info=not isinstance(exc, requests.exceptions.RequestException),
        )
    tts_pricing = build_price_per_char_table(tts_catalog)

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

    # Bind all interfaces (IPv4 + IPv6) when host is the wildcard: Home
    # Assistant's hassio network is dual-stack and may resolve the add-on to an
    # IPv6 address, so an IPv4-only socket would be unreachable. host=None makes
    # asyncio listen on every address family. asyncio accepts host=None at
    # runtime even though wyoming types the parameter as str.
    bind_host = None if args.host in ("", "0.0.0.0", "::") else args.host

    # TaskGroup (not gather): if one task's port is already in use, the whole
    # process fails fast via structured-concurrency cancellation rather than
    # silently running a partial set of tasks -- the right behavior for a
    # supervised container process.
    async with asyncio.TaskGroup() as tg:
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
