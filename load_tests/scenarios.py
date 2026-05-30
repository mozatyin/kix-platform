"""Worst-case stress scenarios (one User class each).

Each scenario is invokable via:
    locust -f load_tests/scenarios.py:ThunderingHerdUser --host ...
    locust -f load_tests/scenarios.py:HotKeyUser --host ...
    locust -f load_tests/scenarios.py:AuctionStormUser --host ...
    locust -f load_tests/scenarios.py:WebhookFloodUser --host ...
    locust -f load_tests/scenarios.py:PacingTestUser --host ...

They exercise paths normal traffic dilutes: same-row contention, hot Redis
keys, write-lock pile-ups, webhook fan-out, pacing-controller contention.
"""
from __future__ import annotations

import random

from locust import HttpUser, constant, constant_pacing, task

from load_tests.locustfile import _auth_headers, _BRANDS, _CONSUMERS, _CAMPAIGNS_BY_BRAND


class ThunderingHerdUser(HttpUser):
    """1000 merchants all create campaigns at once.

    Spawn 1000 concurrently → tests connection pool, write lock, ID generator.
    """
    wait_time = constant(0)  # no think-time, fire as fast as possible after start

    def on_start(self) -> None:
        self.brand = random.choice(_BRANDS)
        self.headers = _auth_headers(self.brand["api_token"])
        self._fired = False

    @task
    def create_campaign_once(self) -> None:
        if self._fired:
            self.environment.runner.stop()  # one shot per user
            return
        self._fired = True
        payload = {
            "brand_id": self.brand["brand_id"],
            "objective": random.choice(["awareness", "conversion"]),
            "daily_budget": 500,
            "bid_amount": 1.0,
            "name": f"herd-{random.randint(1, 1_000_000)}",
            "mock": True,
        }
        self.client.post(
            "/api/v1/campaigns/create", json=payload, headers=self.headers,
            name="HERD POST /campaigns/create",
        )


class HotKeyUser(HttpUser):
    """1000 consumers all play the SAME campaign — single hot Redis key."""
    wait_time = constant_pacing(1.0)

    # picked once at class-load so all users share it
    _HOT_BRAND = _BRANDS[0]
    _HOT_CAMPAIGNS = _CAMPAIGNS_BY_BRAND.get(_HOT_BRAND["brand_id"], [])

    def on_start(self) -> None:
        self.consumer = random.choice(_CONSUMERS)
        self.headers = _auth_headers(self.consumer["auth_token"])

    @task
    def play_hot_campaign(self) -> None:
        if not self._HOT_CAMPAIGNS:
            return
        cmp_ = self._HOT_CAMPAIGNS[0]  # always the same → hot key
        payload = {
            "campaign_id": cmp_["campaign_id"],
            "kix_id": self.consumer["kix_id"],
            "score": random.randint(0, 10_000),
            "mock": True,
        }
        self.client.post(
            "/api/v1/auction/report-engagement",
            json=payload, headers=self.headers,
            name="HOT POST /auction/report-engagement",
        )


class AuctionStormUser(HttpUser):
    """Send ~10K bids in 1 second.

    Achieve via: 10000 users × 1 req each with wait_time=constant(0).
    """
    wait_time = constant(1.0)

    def on_start(self) -> None:
        self.consumer = random.choice(_CONSUMERS)
        self.headers = _auth_headers(self.consumer["auth_token"])
        self.brand = random.choice(_BRANDS)

    @task
    def fire_bid(self) -> None:
        campaigns = _CAMPAIGNS_BY_BRAND.get(self.brand["brand_id"], [])
        if not campaigns:
            return
        cmp_ = random.choice(campaigns)
        payload = {
            "campaign_id": cmp_["campaign_id"],
            "kix_id": self.consumer["kix_id"],
            "bid": round(random.uniform(0.05, 5.0), 2),
            "mock": True,
        }
        self.client.post(
            "/api/v1/auction/run", json=payload, headers=self.headers,
            name="STORM POST /auction/run",
        )


class WebhookFloodUser(HttpUser):
    """Simulate Stripe firing ~1000 webhooks/sec to the deposits endpoint."""
    wait_time = constant_pacing(0.001)  # 1000 req/sec/user — combine with N users

    def on_start(self) -> None:
        # webhook endpoint usually accepts unauth + signature header — mock both
        self.headers = {
            "Content-Type": "application/json",
            "Stripe-Signature": "t=0,v1=mock,v0=mock",
            "X-Mock-Webhook": "1",
            "X-Load-Test": "1",
        }
        self.brand = random.choice(_BRANDS)

    @task
    def fire_webhook(self) -> None:
        payload = {
            "type": "payment_intent.succeeded",
            "data": {"object": {
                "id": f"pi_loadtest_{random.randint(1, 1_000_000_000):010d}",
                "metadata": {"brand_id": self.brand["brand_id"]},
                "amount_received": random.choice([5_000, 10_000, 25_000]),
                "currency": "sgd",
            }},
            "mock": True,
        }
        self.client.post(
            "/api/v1/deposits/webhook/stripe",
            json=payload, headers=self.headers,
            name="WEBHOOK POST /deposits/webhook/stripe",
        )


class PacingTestUser(HttpUser):
    """100 brands all spending at max rate.

    Each user picks one brand, hammers the report-impression endpoint so the
    pacing controller has to throttle in real time.
    """
    wait_time = constant(0.05)

    def on_start(self) -> None:
        self.brand = random.choice(_BRANDS[:100])
        self.headers = _auth_headers(self.brand["api_token"])
        self.campaigns = _CAMPAIGNS_BY_BRAND.get(self.brand["brand_id"], [])

    @task
    def burn_budget(self) -> None:
        if not self.campaigns:
            return
        cmp_ = random.choice(self.campaigns)
        payload = {
            "campaign_id": cmp_["campaign_id"],
            "impression_token": f"imp_{random.randint(1, 9_999_999_999):010d}",
            "mock": True,
        }
        self.client.post(
            "/api/v1/auction/report-impression",
            json=payload, headers=self.headers,
            name="PACING POST /auction/report-impression",
        )
