"""Game Builder Worker — polls Redis for game orders, routes to ELTM KiX Channel.

Uses the DEDICATED kix_channel in ELTM (not the generic build_core pipeline).
The kix_channel handles:
  - Platform constraints (iframe, postMessage, single-player, short session)
  - Brand theming (colors, name)
  - Score bridge injection
  - HTML post-processing

Run with Code-Soul venv:
  cd /Users/mozat/code-soul && .venv/bin/python /Users/mozat/kix-platform/workers/game_builder.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import redis

ELTM_PATH = "/Users/mozat/eltm"
CODESOUL_PATH = "/Users/mozat/code-soul"
KIX_PATH = "/Users/mozat/kix-platform"

for p in [ELTM_PATH, CODESOUL_PATH]:
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(Path(ELTM_PATH) / ".env")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
POLL_INTERVAL = 5


def get_redis_client():
    return redis.from_url(REDIS_URL, decode_responses=True)


def find_pending_orders(r) -> list[dict]:
    orders = []
    for key in r.scan_iter("game_order:*"):
        data = r.hgetall(key)
        if data.get("status") == "pending":
            order_id = key.split(":", 1)[1]
            data["order_id"] = order_id
            orders.append(data)
    return orders


def update_order(r, order_id: str, status: str, extra: dict | None = None):
    key = f"game_order:{order_id}"
    r.hset(key, "status", status)
    if extra:
        for k, v in extra.items():
            r.hset(key, k, v)


def load_brand_config(r, brand_id: str) -> dict:
    """Load brand config from Redis to get brand name/colors."""
    raw = r.get(f"config:{brand_id}")
    if raw:
        return json.loads(raw)
    return {}


def _handle_result(r, order_id: str, result):
    """Update order status based on KiXGameResult — shared by both flows."""
    if result.ok:
        update_order(r, order_id, "completed", {
            "game_file": result.relative_path,
            "game_name": result.game_name,
            "game_slug": result.game_slug,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": str(round(result.elapsed_s, 1)),
        })
        print(f"\n[worker] Order {order_id} COMPLETED → {result.relative_path}")
    elif result.spec_available:
        update_order(r, order_id, "spec_ready", {
            "game_name": result.game_name,
            "game_slug": result.game_slug,
            "error": result.error,
            "note": "Game spec generated but HTML build failed. Spec available for manual build.",
        })
        print(f"\n[worker] Order {order_id} SPEC_READY (HTML failed: {result.error})")
    else:
        update_order(r, order_id, "failed", {
            "error": result.error[:500],
        })
        print(f"\n[worker] Order {order_id} FAILED: {result.error}")


def _process_fulfill_order(r, order: dict):
    """Old flow: custom game order via fulfill_order()."""
    from eltm.kix_channel import KiXOrder, fulfill_order

    order_id = order["order_id"]
    brand_id = order.get("brand_id", "unknown")
    description = order.get("description", "")

    # Load brand config for theming
    brand_cfg = load_brand_config(r, brand_id)

    kix_order = KiXOrder(
        order_id=order_id,
        brand_id=brand_id,
        description=description,
        game_slug=order.get("game_slug") or None,
        theme=order.get("theme") or None,
        requirements=order.get("requirements") or None,
        brand_name=brand_cfg.get("brand_name"),
        brand_color=brand_cfg.get("brand_color"),
        accent_color=brand_cfg.get("accent_color"),
        energy_cost=int(order.get("energy_cost", 10)),
        max_session_minutes=3,
    )

    result = fulfill_order(
        kix_order,
        api_key=API_KEY,
        on_progress=lambda msg: print(f"  {msg}", flush=True),
    )

    _handle_result(r, order_id, result)


def _process_build_for_business(r, order: dict):
    """New flow: merchant describes business, gets branded game."""
    from eltm.kix_channel import research_business, build_for_business

    order_id = order["order_id"]
    brand_id = order.get("brand_id", "unknown")
    business_description = order.get("business_description", "")
    game_slug = order.get("game_slug", "")

    if not business_description:
        update_order(r, order_id, "failed", {
            "error": "Missing business_description for build_for_business order",
        })
        print(f"\n[worker] Order {order_id} FAILED: missing business_description")
        return

    if not game_slug:
        update_order(r, order_id, "failed", {
            "error": "Missing game_slug for build_for_business order",
        })
        print(f"\n[worker] Order {order_id} FAILED: missing game_slug")
        return

    progress_cb = lambda msg: print(f"  {msg}", flush=True)

    # Step 1: Research the merchant's business → BusinessProfile
    print(f"  [build-for-business] Researching business...", flush=True)
    profile = research_business(
        business_description,
        api_key=API_KEY,
        on_progress=progress_cb,
    )
    print(f"  [build-for-business] Profile: {profile.brand_name} ({profile.industry})", flush=True)

    # Step 2: Build branded game using profile + selected game
    result = build_for_business(
        profile,
        game_slug,
        brand_id=brand_id,
        order_id=order_id,
        api_key=API_KEY,
        on_progress=progress_cb,
    )

    _handle_result(r, order_id, result)


def process_order(r, order: dict):
    order_id = order["order_id"]
    order_type = order.get("order_type", "custom")
    brand_id = order.get("brand_id", "unknown")

    print(f"\n{'='*60}")
    print(f"[worker] Order: {order_id}")
    print(f"  Type: {order_type}")
    print(f"  Brand: {brand_id}")
    if order_type == "build_for_business":
        print(f"  Business: {order.get('business_description', '')[:80]}")
        print(f"  Game: {order.get('game_slug', '')}")
    else:
        print(f"  Description: {order.get('description', '')[:80]}")
    print(f"{'='*60}")

    update_order(r, order_id, "building")

    if order_type == "build_for_business":
        _process_build_for_business(r, order)
    else:
        _process_fulfill_order(r, order)


def main():
    print("=" * 60)
    print("KiX Game Builder Worker (via kix_channel)")
    print(f"  ELTM: {ELTM_PATH}")
    print(f"  Code-Soul: {CODESOUL_PATH}")
    print(f"  Redis: {REDIS_URL}")
    print(f"  API key: {'set' if API_KEY else 'MISSING'}")
    print("=" * 60)

    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    r = get_redis_client()
    r.ping()
    print("Redis connected. Polling for orders...\n")

    while True:
        try:
            orders = find_pending_orders(r)
            if orders:
                print(f"[worker] Found {len(orders)} pending order(s)")
                for order in orders:
                    process_order(r, order)
            else:
                sys.stdout.write(".")
                sys.stdout.flush()
        except KeyboardInterrupt:
            print("\nShutting down.")
            break
        except Exception as e:
            print(f"\n[worker error] {e}")
            import traceback
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
