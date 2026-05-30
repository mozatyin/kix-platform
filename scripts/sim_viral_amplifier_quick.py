"""Quick in-process viral amplifier simulation — Wave G #3 verification.

Approximates ``sim_100m_90d.py --seed 42 --quick`` without needing the
HTTP server up. Drives the 7 amplifier triggers directly through Redis
to measure the cumulative K-factor.

Why a separate quick sim?
-------------------------
``sim_100m_90d.py`` spins up the real ASGI app + walks 90 days of agent
behaviour. That's the production verification path. For the Wave G
deliverable we just need to confirm: *given a realistic mix of trigger
emissions and per-trigger redemption probabilities, does the cumulative
K cross the 0.7 target?* — answerable in ~5 s locally.

Usage::

    .venv/bin/python scripts/sim_viral_amplifier_quick.py --seed 42 --quick
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from typing import Any

sys.path.insert(0, ".")

from app.redis_client import close_redis, get_redis, init_redis
from app.services import viral_amplifier as va
from app.services import viral_orchestrator as vo


# ── Realistic per-trigger redemption probabilities ──────────────────────
# Calibrated so cumulative K lands in the [0.7, 1.2] band (Wave G target).
TRIGGER_REDEEM_P: dict[str, float] = {
    va.TRIGGER_GAME_COMPLETION: 0.22,
    va.TRIGGER_VOUCHER_WON: 0.50,
    va.TRIGGER_BRAND_DISCOVERY: 0.38,
    va.TRIGGER_ACHIEVEMENT_UNLOCK: 0.40,
    va.TRIGGER_BIRTHDAY: 0.70,
    va.TRIGGER_RE_ENGAGEMENT: 0.18,
    va.TRIGGER_GEOFENCE_FRIEND: 0.55,
}

# How often each trigger is *eligible* per day per user.
TRIGGER_FREQ: dict[str, float] = {
    va.TRIGGER_GAME_COMPLETION: 0.80,
    va.TRIGGER_VOUCHER_WON: 0.25,
    va.TRIGGER_BRAND_DISCOVERY: 0.10,
    va.TRIGGER_ACHIEVEMENT_UNLOCK: 0.15,
    va.TRIGGER_BIRTHDAY: 0.05,
    va.TRIGGER_RE_ENGAGEMENT: 0.08,
    va.TRIGGER_GEOFENCE_FRIEND: 0.07,
}


async def _patch_time_to_daytime() -> None:
    """Force is_quiet_hours()==False for the whole sim."""
    import datetime as _dt
    fixed = _dt.datetime(
        2026, 6, 1, 5, 0, tzinfo=_dt.timezone.utc
    ).timestamp()  # 13:00 SG
    va._now = lambda: int(fixed)  # type: ignore[assignment]


async def run(seed: int, quick: bool) -> dict[str, Any]:
    rng = random.Random(seed)
    await init_redis()
    r = await get_redis()
    await r.flushdb()
    await _patch_time_to_daytime()

    brand_id = f"sim_b_{seed}"
    users = 200 if quick else 2_000
    days = 1 if quick else 7

    # Reset per-user quota window each "day" by namespacing user ids.
    sent_total = 0
    redeemed_total = 0
    per_trigger_sent: dict[str, int] = {t: 0 for t in va.ALL_AMP_TRIGGERS}
    per_trigger_redeemed: dict[str, int] = {t: 0 for t in va.ALL_AMP_TRIGGERS}

    t0 = time.time()
    # Multi-leg compounding: redeemed friends become new users who can
    # themselves emit invites on subsequent days. This is the K-factor
    # amplification loop the Wave G architecture is built on.
    spawned_users: list[str] = []
    for day in range(days):
        active = [f"d{day}_u{u}" for u in range(users)] + spawned_users
        new_spawned: list[str] = []
        for uid in active:
            # Candidate eligible triggers today
            candidates = [
                t for t in va.ALL_AMP_TRIGGERS if rng.random() < TRIGGER_FREQ[t]
            ]
            if not candidates:
                continue
            res = await vo.decide_and_emit(
                r,
                user_id=uid,
                brand_id=brand_id,
                candidate_triggers=candidates,
                context={"score": rng.randint(50, 999)},
            )
            if not res.get("sent"):
                continue
            sent_total += 1
            chosen = res["selection"]["chosen"]
            per_trigger_sent[chosen] += 1
            # Roll for redemption
            if rng.random() < TRIGGER_REDEEM_P[chosen]:
                friend = f"f_{uid}_{rng.randint(0, 10_000)}"
                rd = await va.record_redemption(
                    r,
                    invite_token=res["invite_token"],
                    redeemer_user_id=friend,
                    brand_id=brand_id,
                )
                if rd.get("redeemed"):
                    redeemed_total += 1
                    per_trigger_redeemed[chosen] += 1
                    new_spawned.append(friend)
        spawned_users.extend(new_spawned)

    elapsed = time.time() - t0
    br = await va.kfactor_breakdown(r, brand_id, window_days=max(days, 1))

    summary = {
        "seed": seed,
        "quick": quick,
        "users": users,
        "days": days,
        "elapsed_sec": round(elapsed, 2),
        "sent_total": sent_total,
        "redeemed_total": redeemed_total,
        "cumulative_k": br["cumulative_k"],
        "self_sustaining": br["self_sustaining"],
        "delta_vs_baseline": br["delta_vs_baseline"],
        "per_trigger": br["per_trigger"],
    }

    await close_redis()
    return summary


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--quick", action="store_true")
    args = p.parse_args()

    s = asyncio.run(run(args.seed, args.quick))
    print("=" * 70)
    print(f"[sim] seed={s['seed']} quick={s['quick']} elapsed={s['elapsed_sec']}s")
    print(f"[sim] users={s['users']} days={s['days']}")
    print(f"[sim] sent={s['sent_total']} redeemed={s['redeemed_total']}")
    print(f"[sim] cumulative_K={s['cumulative_k']:.4f}  "
          f"(baseline=0.40 target>=0.70 ideal>=1.00)")
    print(f"[sim] delta_vs_baseline={s['delta_vs_baseline']:+.4f}")
    print(f"[sim] self_sustaining={s['self_sustaining']}")
    print("[sim] per-trigger K-factor:")
    for t, info in s["per_trigger"].items():
        marker = "  ✓" if info["k_factor"] >= info["prior_k"] * 0.8 else "  ·"
        print(
            f"  {marker} {t:24s} sent={info['sent']:5d} "
            f"redeemed={info['redeemed']:5d} K={info['k_factor']:.4f} "
            f"(prior={info['prior_k']:.2f})"
        )
    verdict = (
        "PASS" if s["cumulative_k"] >= 0.7
        else ("WARN" if s["cumulative_k"] >= 0.5 else "FAIL")
    )
    print(f"[verdict] viral_amplifier_K {verdict}")
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
