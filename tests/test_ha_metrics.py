"""Tests for wyoming_openrouter.ha_metrics."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from wyoming_openrouter.ha_metrics import Metrics, push_to_supervisor


def _run(coro):
    return asyncio.run(coro)


def test_metrics_record_accumulates():
    metrics = Metrics(task_type="stt", task_slug="kitchen_stt")
    metrics.record(100, 0.001)
    metrics.record(300, 0.002)

    assert metrics.request_count == 2
    assert metrics.total_cost == pytest.approx(0.003)
    assert metrics.unknown_cost_count == 0
    assert metrics.last_latency_ms == 300
    assert metrics.avg_latency_ms == pytest.approx(200)


def test_metrics_record_with_none_cost_tracked_as_unknown():
    metrics = Metrics(task_type="tts", task_slug="assist_tts")
    metrics.record(100, 0.001)
    metrics.record(200, None)

    assert metrics.request_count == 2
    assert metrics.total_cost == pytest.approx(0.001)  # unknown cost never added
    assert metrics.unknown_cost_count == 1
    assert metrics.last_latency_ms == 200


def test_metrics_avg_latency_zero_when_no_requests():
    assert Metrics(task_type="stt", task_slug="x").avg_latency_ms == 0.0


def test_entity_prefix_disambiguates_by_type_and_slug():
    stt = Metrics(task_type="stt", task_slug="kitchen_stt")
    tts = Metrics(task_type="tts", task_slug="assist_tts")
    another_stt = Metrics(task_type="stt", task_slug="office_stt")

    prefixes = {stt.entity_prefix, tts.entity_prefix, another_stt.entity_prefix}
    assert len(prefixes) == 3
    assert stt.entity_prefix == "wyoming_openrouter_stt_kitchen_stt"
    assert tts.entity_prefix == "wyoming_openrouter_tts_assist_tts"


def test_push_to_supervisor_noop_without_token(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    metrics = Metrics(task_type="stt", task_slug="kitchen_stt")
    metrics.record(50, 0.0001)

    with patch("wyoming_openrouter.ha_metrics.requests.post") as mock_post:
        _run(push_to_supervisor(metrics))

    mock_post.assert_not_called()


def test_push_to_supervisor_posts_expected_urls_with_token(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    metrics = Metrics(task_type="stt", task_slug="kitchen_stt")
    metrics.record(50, 0.0001)

    response = MagicMock()
    response.raise_for_status.return_value = None
    with patch(
        "wyoming_openrouter.ha_metrics.requests.post", return_value=response
    ) as mock_post:
        _run(push_to_supervisor(metrics))

    assert mock_post.call_count == 5
    urls = {call.args[0] for call in mock_post.call_args_list}
    assert urls == {
        "http://supervisor/core/api/states/sensor.wyoming_openrouter_stt_kitchen_stt_request_count",
        "http://supervisor/core/api/states/sensor.wyoming_openrouter_stt_kitchen_stt_total_cost",
        "http://supervisor/core/api/states/sensor.wyoming_openrouter_stt_kitchen_stt_unknown_cost_count",
        "http://supervisor/core/api/states/sensor.wyoming_openrouter_stt_kitchen_stt_last_latency_ms",
        "http://supervisor/core/api/states/sensor.wyoming_openrouter_stt_kitchen_stt_avg_latency_ms",
    }
    for call in mock_post.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_push_to_supervisor_urls_for_tts_task_use_tts_prefix(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    metrics = Metrics(task_type="tts", task_slug="assist_tts")
    metrics.record(50, 0.0001)

    response = MagicMock()
    response.raise_for_status.return_value = None
    with patch(
        "wyoming_openrouter.ha_metrics.requests.post", return_value=response
    ) as mock_post:
        _run(push_to_supervisor(metrics))

    urls = {call.args[0] for call in mock_post.call_args_list}
    assert (
        "http://supervisor/core/api/states/sensor.wyoming_openrouter_tts_assist_tts_request_count"
        in urls
    )


def test_push_to_supervisor_failure_is_logged_not_raised(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    metrics = Metrics(task_type="stt", task_slug="kitchen_stt")
    metrics.record(50, 0.0001)

    with patch(
        "wyoming_openrouter.ha_metrics.requests.post", side_effect=Exception("boom")
    ):
        _run(push_to_supervisor(metrics))  # must not raise
