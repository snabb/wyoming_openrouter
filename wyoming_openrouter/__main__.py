#!/usr/bin/env python3
"""Wyoming server for OpenRouter speech-to-text."""

import argparse
import asyncio
import logging
import os
import sys
from functools import partial
from typing import cast

from wyoming.server import AsyncTcpServer

from . import __version__
from .ha_metrics import Metrics
from .handler import OpenRouterEventHandler, get_wyoming_info
from .openrouter import list_stt_models

_LOGGER = logging.getLogger(__name__)

DEFAULT_MODELS = "openai/gpt-4o-mini-transcribe"
DEFAULT_LANGUAGES = "en"


async def main() -> None:
    """Run the Wyoming OpenRouter server."""
    parser = argparse.ArgumentParser(
        description="Wyoming server for OpenRouter speech-to-text"
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=10300, help="Port to bind to (default: 10300)"
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENROUTER_API_KEY"),
        help="OpenRouter API key (default: OPENROUTER_API_KEY environment variable)",
    )
    parser.add_argument(
        "--models",
        default=DEFAULT_MODELS,
        help=(
            "Comma-separated OpenRouter STT model slugs to advertise, e.g. "
            "'openai/gpt-4o-mini-transcribe' or "
            "'openai/gpt-4o-mini-transcribe,openai/whisper-1'. The first is "
            f"the default model used for every request from Home Assistant "
            f"(default: {DEFAULT_MODELS})"
        ),
    )
    parser.add_argument(
        "--languages",
        default=DEFAULT_LANGUAGES,
        help=(
            "Comma-separated languages to advertise to Home Assistant "
            f"(default: {DEFAULT_LANGUAGES})"
        ),
    )
    parser.add_argument(
        "--default-language",
        default=None,
        help=(
            "Language hint sent to OpenRouter when a request doesn't specify "
            "one (default: unset, let the model auto-detect)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds for OpenRouter requests (default: 60)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="version", version=__version__)

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    _LOGGER.info("Starting Wyoming OpenRouter server v%s", __version__)

    if not args.api_key:
        _LOGGER.critical(
            "No OpenRouter API key configured; set --api-key or the "
            "OPENROUTER_API_KEY environment variable"
        )
        sys.exit(1)

    args.models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not args.models:
        _LOGGER.critical("No models configured")
        sys.exit(1)

    args.languages = [
        lang.strip() for lang in args.languages.split(",") if lang.strip()
    ] or ["en"]

    _LOGGER.info(
        "Models: %s (default: %s) | languages: %s",
        ", ".join(args.models),
        args.models[0],
        ", ".join(args.languages),
    )

    # Best-effort only: users pick --models by checking this log line (or
    # OpenRouter's own model listing directly), so a failure here must never
    # block startup.
    try:
        catalog = await asyncio.to_thread(list_stt_models, args.api_key)
        catalog_line = ", ".join(
            f"{model['id']} (${model.get('pricing', {}).get('prompt', '?')})"
            for model in catalog
        )
        _LOGGER.info(
            "Live OpenRouter STT model catalog: %s", catalog_line or "<none returned>"
        )
    except Exception:
        _LOGGER.warning(
            "Could not fetch the live OpenRouter STT model catalog "
            "(startup continues regardless)",
            exc_info=True,
        )

    wyoming_info = get_wyoming_info(args.models, args.languages)
    metrics = Metrics()

    # Bind all interfaces (IPv4 + IPv6) when host is the wildcard: Home
    # Assistant's hassio network is dual-stack and may resolve the add-on to an
    # IPv6 address, so an IPv4-only socket would be unreachable. host=None makes
    # asyncio listen on every address family. asyncio accepts host=None at
    # runtime even though wyoming types the parameter as str.
    bind_host = None if args.host in ("", "0.0.0.0", "::") else args.host
    server = AsyncTcpServer(host=cast("str", bind_host), port=args.port)
    _LOGGER.info("Server listening on %s:%d", args.host, args.port)

    await server.run(partial(OpenRouterEventHandler, wyoming_info, args, metrics))


def run() -> None:
    """Entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
