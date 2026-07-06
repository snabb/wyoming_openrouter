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

Each task (one Wyoming TCP port, one dedicated model) gets its own Metrics
instance, disambiguated in its pushed entity IDs by task type + slug.
"""

import asyncio
import logging
import os
from typing import Any, Optional

import requests

_LOGGER = logging.getLogger(__name__)

_SUPERVISOR_STATES_URL = "http://supervisor/core/api/states/{entity_id}"
_PUSH_TIMEOUT_SECONDS = 5.0


class Metrics:
    """Per-task request counters.

    Only ever mutated between ``await``s in the single-threaded asyncio event
    loop, so no lock is needed even though multiple connections to the same
    task share one instance.
    """

    def __init__(self, task_type: str, task_slug: str) -> None:
        self.task_type = task_type
        self.task_slug = task_slug
        self.request_count = 0
        self.total_cost = 0.0
        self.unknown_cost_count = 0
        self.last_latency_ms = 0
        self._latency_sum_ms = 0

    def record(self, latency_ms: int, cost: Optional[float]) -> None:
        """Record one completed request. cost=None means the cost could not
        be determined (e.g. a TTS generation-cost lookup that never resolved
        for a model whose pricing can't be safely estimated) -- tracked
        separately rather than silently guessed into total_cost."""
        self.request_count += 1
        self.last_latency_ms = latency_ms
        self._latency_sum_ms += latency_ms
        if cost is None:
            self.unknown_cost_count += 1
        else:
            self.total_cost += cost

    @property
    def avg_latency_ms(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self._latency_sum_ms / self.request_count

    @property
    def entity_prefix(self) -> str:
        return f"wyoming_openrouter_{self.task_type}_{self.task_slug}"


def _sensor_states(metrics: Metrics) -> dict[str, dict[str, Any]]:
    prefix = metrics.entity_prefix
    return {
        f"sensor.{prefix}_request_count": {
            "state": metrics.request_count,
            "attributes": {
                "friendly_name": f"Wyoming OpenRouter {metrics.task_slug} request count",
                "icon": "mdi:counter",
            },
        },
        f"sensor.{prefix}_total_cost": {
            "state": round(metrics.total_cost, 6),
            "attributes": {
                "friendly_name": f"Wyoming OpenRouter {metrics.task_slug} total cost",
                "unit_of_measurement": "USD",
                "icon": "mdi:currency-usd",
            },
        },
        f"sensor.{prefix}_unknown_cost_count": {
            "state": metrics.unknown_cost_count,
            "attributes": {
                "friendly_name": f"Wyoming OpenRouter {metrics.task_slug} requests with unknown cost",
                "icon": "mdi:help-circle-outline",
            },
        },
        f"sensor.{prefix}_last_latency_ms": {
            "state": metrics.last_latency_ms,
            "attributes": {
                "friendly_name": f"Wyoming OpenRouter {metrics.task_slug} last request latency",
                "unit_of_measurement": "ms",
                "icon": "mdi:timer-outline",
            },
        },
        f"sensor.{prefix}_avg_latency_ms": {
            "state": round(metrics.avg_latency_ms, 1),
            "attributes": {
                "friendly_name": f"Wyoming OpenRouter {metrics.task_slug} average request latency",
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
    the transcription/synthesis response already sent to the client.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return

    for entity_id, state in _sensor_states(metrics).items():
        try:
            await asyncio.to_thread(_push_one, entity_id, state, token)
        except Exception:
            _LOGGER.warning("Failed to push %s to Home Assistant", entity_id, exc_info=True)
