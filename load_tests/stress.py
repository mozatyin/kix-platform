"""Stress profile: 1000 merchants + 10000 consumers for 15 minutes.

This is the *claimed* operating point per the marketing page. We hold it for
15 minutes to expose slow leaks: Redis growth, PG bloat, slow GC, connection
exhaustion.
"""
from __future__ import annotations

from locust import LoadTestShape

from load_tests.locustfile import MerchantUser, ConsumerUser  # noqa: F401


class StressShape(LoadTestShape):
    ramp_duration = 180  # 3 min ramp
    hold_duration = 900  # 15 min hold
    target_merchants = 1000
    target_consumers = 10000

    def tick(self):
        run_time = self.get_run_time()
        total = self.target_merchants + self.target_consumers
        if run_time < self.ramp_duration:
            users = int(total * run_time / self.ramp_duration)
            return users, max(1.0, total / self.ramp_duration)
        if run_time < self.ramp_duration + self.hold_duration:
            return total, 100.0
        return None


if __name__ == "__main__":  # pragma: no cover
    import os, sys
    os.execvp("locust", [
        "locust", "-f", __file__,
        "--headless",
        "--host", os.environ.get("KIX_HOST", "http://localhost:8000"),
        "--csv", "load_tests/results/stress",
        "--run-time", "20m",
        *sys.argv[1:],
    ])
