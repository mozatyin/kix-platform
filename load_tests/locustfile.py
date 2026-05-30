"""Main Locust scenarios for the KiX platform.

Two user classes:
- MerchantUser: dashboard, campaigns, wallet, bidding, search
- ConsumerUser: QR scan, game play, storefront browsing, voucher redeem

Both pull from pre-seeded brands/campaigns/consumers (load_tests/data/).
Mock mode only — no real Stripe / FCM hits.

Run examples:
    locust -f load_tests/locustfile.py --host http://localhost:8000
    locust -f load_tests/baseline.py --headless

Endpoints are paths only; the host is supplied at invocation time so we can
target staging, local, or a perf VM without code changes.
"""
from __future__ import annotations

import random
from typing import Optional

from locust import HttpUser, between, task, events

from load_tests.seed_data import (
    load_brands,
    load_campaigns,
    load_consumers,
    lunch_dinner_weight,
    seed,
)


# Pre-seed on import so worker processes share the same data files.
seed()
_BRANDS = load_brands()
_CAMPAIGNS_BY_BRAND: dict[str, list[dict]] = {}
for _c in load_campaigns():
    _CAMPAIGNS_BY_BRAND.setdefault(_c["brand_id"], []).append(_c)
_CONSUMERS = load_consumers()


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Mock-Auth": "1",        # server-side mock-auth hook bypasses Stripe/FCM
        "X-Load-Test": "1",
        "Content-Type": "application/json",
    }


class MerchantUser(HttpUser):
    """Simulates a brand portal user."""
    # Realistic merchant think-time: 2-8 s between actions (dashboards are sticky)
    wait_time = between(2, 8)

    def on_start(self) -> None:
        self.brand = random.choice(_BRANDS)
        self.headers = _auth_headers(self.brand["api_token"])
        self.campaigns = _CAMPAIGNS_BY_BRAND.get(self.brand["brand_id"], [])

    # ── Tasks (weights match the brief) ──────────────────────────────
    @task(60)
    def view_dashboard(self) -> None:
        self.client.get(
            f"/api/v1/dashboards/brand/{self.brand['brand_id']}",
            headers=self.headers, name="GET /dashboards/brand/[id]",
        )

    @task(20)
    def list_campaigns(self) -> None:
        self.client.get(
            f"/api/v1/campaigns/{self.brand['brand_id']}",
            headers=self.headers, name="GET /campaigns/[brand_id]",
        )

    @task(5)
    def create_campaign(self) -> None:
        payload = {
            "brand_id": self.brand["brand_id"],
            "objective": random.choice(
                ["awareness", "consideration", "conversion", "retention"]
            ),
            "daily_budget": random.choice([50, 100, 250, 500]),
            "bid_amount": round(random.uniform(0.05, 2.5), 2),
            "name": f"loadtest-{random.randint(1, 1_000_000)}",
            "mock": True,
        }
        self.client.post(
            "/api/v1/campaigns/create", json=payload, headers=self.headers,
            name="POST /campaigns/create",
        )

    @task(10)
    def check_wallet(self) -> None:
        self.client.get(
            f"/api/v1/wallet/{self.brand['brand_id']}",
            headers=self.headers, name="GET /wallet/[brand_id]",
        )

    @task(3)
    def update_bid(self) -> None:
        if not self.campaigns:
            return
        cmp_ = random.choice(self.campaigns)
        self.client.post(
            f"/api/v1/campaigns/{cmp_['campaign_id']}/update",
            json={"bid_amount": round(random.uniform(0.05, 2.5), 2)},
            headers=self.headers, name="POST /campaigns/[id]/update",
        )

    @task(2)
    def search(self) -> None:
        term = random.choice(["coffee", "chicken", "noodle", "pizza", "rice", "tea"])
        self.client.get(
            f"/api/v1/search?q={term}",
            headers=self.headers, name="GET /search",
        )


class ConsumerUser(HttpUser):
    """Simulates an end-consumer in a brand's gamified storefront."""
    # Consumers act faster (snap, swipe, play) — 0.5-3 s between actions.
    wait_time = between(0.5, 3.0)

    def on_start(self) -> None:
        self.consumer = random.choice(_CONSUMERS)
        self.headers = _auth_headers(self.consumer["auth_token"])
        self.brand = random.choice(_BRANDS)
        self.campaigns = _CAMPAIGNS_BY_BRAND.get(self.brand["brand_id"], [])
        # apply lunch/dinner curve as a per-user pacing dial
        self._traffic_weight = lunch_dinner_weight()

    @task(30)
    def scan_qr(self) -> None:
        payload = {
            "qr_token": f"qr_{random.randint(1, 9_999_999):07x}",
            "brand_id": self.brand["brand_id"],
            "mock": True,
        }
        self.client.post(
            "/internal/qr/scan", json=payload, headers=self.headers,
            name="POST /qr/scan",
        )

    @task(40)
    def play_game(self) -> None:
        if not self.campaigns:
            return
        cmp_ = random.choice(self.campaigns)
        payload = {
            "campaign_id": cmp_["campaign_id"],
            "kix_id": self.consumer["kix_id"],
            "score": random.randint(0, 10_000),
            "mock": True,
        }
        # auction.report-engagement is the closest "I played a sponsored game" event
        self.client.post(
            "/api/v1/auction/report-engagement",
            json=payload, headers=self.headers,
            name="POST /auction/report-engagement",
        )

    @task(20)
    def browse_storefront(self) -> None:
        self.client.get(
            f"/api/v1/brands/{self.brand['brand_id']}",
            headers=self.headers, name="GET /brands/[id]",
        )

    @task(10)
    def redeem_voucher(self) -> None:
        payload = {
            "brand_id": self.brand["brand_id"],
            "kix_id": self.consumer["kix_id"],
            "voucher_code": f"VCH{random.randint(1, 9_999_999):07d}",
            "mock": True,
        }
        self.client.post(
            f"/api/v1/vouchers/{self.brand['brand_id']}/redeem",
            json=payload, headers=self.headers,
            name="POST /vouchers/[brand_id]/redeem",
        )


# ── Breaking-point detector ─────────────────────────────────────────
# Tracks rolling p95 + error-rate; emits an event when either crosses the
# threshold so the runner can record the breaking point even mid-run.
_RECENT: list[tuple[float, bool]] = []  # (latency_ms, is_error)
_BREAKING_POINT_EMITTED = False


@events.request.add_listener
def _on_request(request_type, name, response_time, response_length,
                exception, context, **kw) -> None:
    global _BREAKING_POINT_EMITTED
    is_err = exception is not None
    _RECENT.append((float(response_time or 0.0), is_err))
    if len(_RECENT) > 2000:
        del _RECENT[:1000]
    if _BREAKING_POINT_EMITTED or len(_RECENT) < 200:
        return
    sample = _RECENT[-500:]
    sample.sort(key=lambda t: t[0])
    p95 = sample[int(len(sample) * 0.95)][0]
    err_rate = sum(1 for _, e in sample if e) / len(sample)
    if p95 > 1000.0 or err_rate > 0.01:
        _BREAKING_POINT_EMITTED = True
        events.user_error.fire(  # type: ignore[attr-defined]
            user_instance=None,
            exception=RuntimeError(
                f"BREAKING_POINT: p95={p95:.0f}ms err_rate={err_rate:.3%}"
            ),
            tb=None,
        )
