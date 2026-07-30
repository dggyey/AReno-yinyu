"""Tests for training event detection and overlay (Issue #271).

All tests are CPU-only — no GPU, model weights, or network services required.
"""

from __future__ import annotations

import math
import unittest
from typing import Any

from areno.dashboard.server import DashboardState, Job


def _make_job(metrics: list[dict[str, Any]] | None = None) -> Job:
    """Create a minimal Job with the given metric points."""
    job = Job(
        kind="train",
        name="test job",
        command=["areno", "train"],
        config={},
        metrics_dir=None,
    )
    job.metrics = metrics or []
    return job


def _metric(name: str, value: float, step: int) -> dict[str, Any]:
    """Shorthand to create a metric point dict."""
    return {"name": name, "value": value, "step": step, "time": "2026-01-01T00:00:00Z"}


class TestTrainingEvents(unittest.TestCase):
    """Verify that training_events() correctly detects anomalies."""

    def setUp(self) -> None:
        self.state = DashboardState()

    def _inject_and_get_events(self, job: Job) -> list[dict[str, Any]]:
        """Inject a job into DashboardState and return its detected events."""
        self.state.jobs[job.id] = job
        return self.state.training_events(job.id)

    # ------------------------------------------------------------------
    # Success path — normal metrics produce no events.
    # ------------------------------------------------------------------

    def test_normal_metrics_no_events(self) -> None:
        """Healthy training with varied rewards and decreasing loss → no events."""
        job = _make_job([
            _metric("rollout/rewards_mean", 0.1, 0),
            _metric("rollout/rewards_mean", 0.3, 1),
            _metric("rollout/rewards_mean", 0.5, 2),
            _metric("train/loss", 2.5, 0),
            _metric("train/loss", 1.8, 1),
            _metric("train/loss", 1.2, 2),
        ])
        events = self._inject_and_get_events(job)
        self.assertEqual(events, [])

    # ------------------------------------------------------------------
    # Non-finite values.
    # ------------------------------------------------------------------

    def test_non_finite_value_detected(self) -> None:
        """NaN or Inf in any metric should produce a non_finite event."""
        job = _make_job([
            _metric("train/loss", float("nan"), 0),
            _metric("train/loss", 1.0, 1),
        ])
        events = self._inject_and_get_events(job)
        non_finite = [e for e in events if e["type"] == "non_finite"]
        self.assertEqual(len(non_finite), 1)
        self.assertEqual(non_finite[0]["step"], 0)
        self.assertEqual(non_finite[0]["severity"], "error")
        self.assertIn("train/loss", non_finite[0]["message"])

    def test_inf_value_detected(self) -> None:
        """Inf values should also be flagged."""
        job = _make_job([
            _metric("rollout/rewards_mean", float("inf"), 5),
            _metric("rollout/rewards_mean", 0.5, 6),
        ])
        events = self._inject_and_get_events(job)
        non_finite = [e for e in events if e["type"] == "non_finite"]
        self.assertEqual(len(non_finite), 1)

    # ------------------------------------------------------------------
    # Constant rewards.
    # ------------------------------------------------------------------

    def test_constant_reward_detected(self) -> None:
        """Reward unchanged for 3+ consecutive steps → constant_reward event."""
        job = _make_job([
            _metric("rollout/rewards_mean", 0.5, 0),
            _metric("rollout/rewards_mean", 0.5, 1),
            _metric("rollout/rewards_mean", 0.5, 2),
            _metric("rollout/rewards_mean", 0.5, 3),
        ])
        events = self._inject_and_get_events(job)
        constant = [e for e in events if e["type"] == "constant_reward"]
        self.assertGreaterEqual(len(constant), 1)
        self.assertEqual(constant[0]["severity"], "warn")
        self.assertIn("constant", constant[0]["message"].lower())

    def test_varying_reward_no_event(self) -> None:
        """Reward changes between steps → no constant_reward event."""
        job = _make_job([
            _metric("rollout/rewards_mean", 0.1, 0),
            _metric("rollout/rewards_mean", 0.2, 1),
            _metric("rollout/rewards_mean", 0.3, 2),
        ])
        events = self._inject_and_get_events(job)
        constant = [e for e in events if e["type"] == "constant_reward"]
        self.assertEqual(constant, [])

    # ------------------------------------------------------------------
    # Zero / large loss.
    # ------------------------------------------------------------------

    def test_zero_loss_detected(self) -> None:
        """Loss exactly 0.0 → zero_loss event."""
        job = _make_job([
            _metric("train/loss", 1.0, 0),
            _metric("train/loss", 0.0, 1),
            _metric("train/loss", 0.5, 2),
        ])
        events = self._inject_and_get_events(job)
        zero = [e for e in events if e["type"] == "zero_loss"]
        self.assertEqual(len(zero), 1)
        self.assertEqual(zero[0]["step"], 1)

    def test_large_loss_detected(self) -> None:
        """Loss > 1e4 → large_loss event."""
        job = _make_job([
            _metric("train/loss", 1.0, 0),
            _metric("train/loss", 50000.0, 1),
            _metric("train/loss", 1.0, 2),
        ])
        events = self._inject_and_get_events(job)
        large = [e for e in events if e["type"] == "large_loss"]
        self.assertEqual(len(large), 1)
        self.assertEqual(large[0]["step"], 1)

    # ------------------------------------------------------------------
    # Invalid-batch streak (missing steps).
    # ------------------------------------------------------------------

    def test_invalid_batch_streak_detected(self) -> None:
        """Gaps in step sequence → invalid_batch_streak event."""
        job = _make_job([
            _metric("train/loss", 1.0, 0),
            _metric("train/loss", 0.9, 1),
            # Steps 2, 3 missing
            _metric("train/loss", 0.7, 4),
            # Steps 5, 6 missing
            _metric("train/loss", 0.5, 7),
        ])
        events = self._inject_and_get_events(job)
        streak = [e for e in events if e["type"] == "invalid_batch_streak"]
        self.assertGreaterEqual(len(streak), 1)

    # ------------------------------------------------------------------
    # Edge cases.
    # ------------------------------------------------------------------

    def test_empty_metrics_no_events(self) -> None:
        """No metrics at all → no events, no crash."""
        job = _make_job([])
        events = self._inject_and_get_events(job)
        self.assertEqual(events, [])

    def test_job_not_found_returns_empty(self) -> None:
        """Non-existent job ID → empty list, no crash."""
        events = self.state.training_events("nonexistent-id")
        self.assertEqual(events, [])

    def test_events_sorted_by_step(self) -> None:
        """Events should be sorted by step ascending."""
        job = _make_job([
            _metric("train/loss", 50000.0, 5),
            _metric("train/loss", float("nan"), 1),
            _metric("train/loss", 1.0, 3),
        ])
        events = self._inject_and_get_events(job)
        steps = [e["step"] for e in events]
        self.assertEqual(steps, sorted(steps))

    def test_events_do_not_mutate_metrics(self) -> None:
        """Calling training_events must not modify job.metrics."""
        metrics = [
            _metric("train/loss", float("nan"), 0),
            _metric("train/loss", 1.0, 1),
        ]
        job = _make_job(list(metrics))  # copy
        original_len = len(job.metrics)
        self._inject_and_get_events(job)
        self.assertEqual(len(job.metrics), original_len)
        # Verify metric values unchanged (use math.isnan for NaN comparison)
        for i, original in enumerate(metrics):
            orig_val = original["value"]
            curr_val = job.metrics[i]["value"]
            if math.isnan(orig_val):
                self.assertTrue(math.isnan(curr_val))
            else:
                self.assertEqual(curr_val, orig_val)

    def test_legacy_run_without_events(self) -> None:
        """A run with only normal metrics (no anomalies) → backward compatible."""
        job = _make_job([
            _metric("rollout/rewards_mean", 0.1, 0),
            _metric("rollout/rewards_mean", 0.2, 1),
            _metric("train/loss", 1.5, 0),
            _metric("train/loss", 1.0, 1),
        ])
        events = self._inject_and_get_events(job)
        self.assertEqual(events, [])

    # ------------------------------------------------------------------
    # Event structure.
    # ------------------------------------------------------------------

    def test_event_structure_has_required_fields(self) -> None:
        """Each event must have step, type, severity, message, metric."""
        job = _make_job([
            _metric("train/loss", float("nan"), 0),
        ])
        events = self._inject_and_get_events(job)
        self.assertGreater(len(events), 0)
        for event in events:
            self.assertIn("step", event)
            self.assertIn("type", event)
            self.assertIn("severity", event)
            self.assertIn("message", event)
            self.assertIn("metric", event)

    def test_severity_values_are_valid(self) -> None:
        """Severity must be either 'error' or 'warn'."""
        job = _make_job([
            _metric("train/loss", float("nan"), 0),
            _metric("train/loss", 0.0, 1),
        ])
        events = self._inject_and_get_events(job)
        for event in events:
            self.assertIn(event["severity"], {"error", "warn"})


if __name__ == "__main__":
    unittest.main()