"""Baseline profile: 100 merchants + 1000 consumers for 5 minutes.

Confirms the system handles routine business-day load with headroom.
Run:
    locust -f load_tests/baseline.py --headless \
        --host http://localhost:8000 \
        --csv load_tests/results/baseline
"""
from __future__ import annotations

from locust import LoadTestShape

from load_tests.locustfile import MerchantUser, ConsumerUser  # noqa: F401


class BaselineShape(LoadTestShape):
    """100 merchants + 1000 consumers, ramped over 60 s, held for 5 min."""
    ramp_duration = 60
    hold_duration = 300  # 5 min
    target_merchants = 100
    target_consumers = 1000

    def tick(self):
        run_time = self.get_run_time()
        total = self.target_merchants + self.target_consumers
        if run_time < self.ramp_duration:
            users = int(total * run_time / self.ramp_duration)
            spawn_rate = max(1.0, total / self.ramp_duration)
            return users, spawn_rate
        if run_time < self.ramp_duration + self.hold_duration:
            return total, 50.0
        return None  # stop


# When invoked directly, run headless for convenience during local checks.
if __name__ == "__main__":  # pragma: no cover
    import os, sys
    os.execvp("locust", [
        "locust", "-f", __file__,
        "--headless",
        "--host", os.environ.get("KIX_HOST", "http://localhost:8000"),
        "--csv", "load_tests/results/baseline",
        "--run-time", "6m",
        *sys.argv[1:],
    ])
