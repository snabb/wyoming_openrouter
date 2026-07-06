"""CI smoke test: Wyoming Describe/Info handshake against a running container.

Not a pytest test (pytest never boots a container) -- invoked directly by
job-docker.yml's smoke-test job after the image is up and healthy.

Deliberately does NOT make a real OpenRouter transcription call: that would
need an OPENROUTER_API_KEY repository secret and would cost real money on
every merge to master. The OpenRouter HTTP call itself is covered by
tests/test_openrouter.py with a mocked requests.post; this script only proves
the server starts, speaks the Wyoming protocol, and advertises the expected
ASR program.
"""

import asyncio
import sys

from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info


async def main() -> None:
    async with AsyncTcpClient("127.0.0.1", 10300) as client:
        await client.write_event(Describe().event())
        event = await client.read_event()

    if event is None or not Info.is_type(event.type):
        print("FAIL: did not receive an Info event in response to Describe", file=sys.stderr)
        sys.exit(1)

    info = Info.from_event(event)
    if not info.asr or info.asr[0].name != "openrouter":
        print(f"FAIL: unexpected asr programs: {info.asr}", file=sys.stderr)
        sys.exit(1)

    if not info.asr[0].models:
        print("FAIL: no models advertised", file=sys.stderr)
        sys.exit(1)

    print(f"OK: asr program '{info.asr[0].name}' with models: {[m.name for m in info.asr[0].models]}")


if __name__ == "__main__":
    asyncio.run(main())
