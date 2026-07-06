"""Request metrics, optionally pushed to Home Assistant as sensor entities.

When running as the Home Assistant App (Supervisor sets SUPERVISOR_TOKEN and
config.yaml requests homeassistant_api: true), the Supervisor proxies
http://supervisor/core/api/* to Home Assistant Core's own REST API using that
token -- the same mechanism this project's run.sh already uses for the
Wyoming discovery POST. That lets the server push its own request-count/cost/
latency counters directly as sensor states, with no MQTT broker and no
separate custom Home Assistant integration required.

In standalone Docker (no SUPERVISOR_TOKEN), there is no Home Assistant Core to
push to, so pushing is skipped entirely and the combined per-request log line
is the only output.
"""

import asyncio
import logging
import os
from typing import Any

import requests

_LOGGER = logging.getLogger(__name__)

_SUPERVISOR_STATES_URL = "http://supervisor/core/api/states/{entity_id}"
_PUSH_TIMEOUT_SECONDS = 5.0


class Metrics:
    """Process-wide request counters.

    Only ever mutated between ``await``s in the single-threaded asyncio event
    loop, so no lock is needed even though multiple connections share one
    instance.
    """

    def __init__(self) -> None:
        self.request_count = 0
        self.total_cost = 0.0
        self.last_latency_ms = 0
        self._latency_sum_ms = 0

    def record(self, latency_ms: int, cost: float) -> None:
        self.request_count += 1
        self.total_cost += cost
        self.last_latency_ms = latency_ms
        self._latency_sum_ms += latency_ms

    @property
    def avg_latency_ms(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self._latency_sum_ms / self.request_count


def _sensor_states(metrics: Metrics) -> dict[str, dict[str, Any]]:
    return {
        "sensor.wyoming_openrouter_stt_request_count": {
            "state": metrics.request_count,
            "attributes": {
                "friendly_name": "Wyoming OpenRouter request count",
                "icon": "mdi:counter",
            },
        },
        "sensor.wyoming_openrouter_stt_total_cost": {
            "state": round(metrics.total_cost, 6),
            "attributes": {
                "friendly_name": "Wyoming OpenRouter total cost",
                "unit_of_measurement": "USD",
                "icon": "mdi:currency-usd",
            },
        },
        "sensor.wyoming_openrouter_stt_last_latency_ms": {
            "state": metrics.last_latency_ms,
            "attributes": {
                "friendly_name": "Wyoming OpenRouter last request latency",
                "unit_of_measurement": "ms",
                "icon": "mdi:timer-outline",
            },
        },
        "sensor.wyoming_openrouter_stt_avg_latency_ms": {
            "state": round(metrics.avg_latency_ms, 1),
            "attributes": {
                "friendly_name": "Wyoming OpenRouter average request latency",
                "unit_of_measurement": "ms",
                "icon": "mdi:timer-outline",
            },
        },
    }


def _push_one(entity_id: str, state: dict[str, Any], token: str) -> None:
    url = _SUPERVISOR_STATES_URL.format(entity_id=entity_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, json=state, timeout=_PUSH_TIMEOUT_SECONDS)
    response.raise_for_status()


async def push_to_supervisor(metrics: Metrics) -> None:
    """Push current metrics to Home Assistant via the Supervisor's Core API proxy.

    No-op when SUPERVISOR_TOKEN isn't set (standalone Docker / local dev).
    Failures are logged, never raised -- a broken metrics push must not affect
    the transcription response already sent to the client.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return

    for entity_id, state in _sensor_states(metrics).items():
        try:
            await asyncio.to_thread(_push_one, entity_id, state, token)
        except Exception:
            _LOGGER.warning("Failed to push %s to Home Assistant", entity_id, exc_info=True)
