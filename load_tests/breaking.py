"""Breaking-point profile: ramp 100 → 50000 users until SLO violation.

Step shape (10 levels) so analyze.py can attribute the breaking step:
    100, 500, 1000, 2500, 5000, 10000, 15000, 25000, 35000, 50000 users
Each level held for 90s so p95/p99 stabilize before we declare safe.

Breaking criteria (matches the locustfile detector):
    p95 > 1000 ms  OR  error rate > 1%
"""
from __future__ import annotations

from locust import LoadTestShape

from load_tests.locustfile import MerchantUser, ConsumerUser  # noqa: F401


class BreakingShape(LoadTestShape):
    # 1 merchant per 10 consumers — matches the 1:10 ratio in baseline/stress
    steps = [100, 500, 1000, 2500, 5000, 10000, 15000, 25000, 35000, 50000]
    step_hold = 90  # seconds per level
    ramp_per_step = 30  # seconds to ramp to next level

    def tick(self):
        run_time = self.get_run_time()
        elapsed = 0
        for i, target in enumerate(self.steps):
            prev = self.steps[i - 1] if i > 0 else 0
            ramp_start = elapsed
            hold_start = ramp_start + self.ramp_per_step
            step_end = hold_start + self.step_hold
            if run_time < hold_start:
                # ramping into this step
                frac = (run_time - ramp_start) / self.ramp_per_step
                users = int(prev + (target - prev) * max(0.0, min(1.0, frac)))
                return users, max(1.0, (target - prev) / self.ramp_per_step)
            if run_time < step_end:
                return target, 50.0
            elapsed = step_end
        return None  # finished all steps


if __name__ == "__main__":  # pragma: no cover
    import os, sys
    os.execvp("locust", [
        "locust", "-f", __file__,
        "--headless",
        "--host", os.environ.get("KIX_HOST", "http://localhost:8000"),
        "--csv", "load_tests/results/breaking",
        "--run-time", "30m",
        *sys.argv[1:],
    ])
