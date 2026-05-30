"""Multi-week Campaign Arc Engine.

Single-session campaigns (``app/routers/campaigns.py``) cover Google-Ads
style "run an ad / pay per conversion" workloads. They do NOT cover the
*multi-week story arc* pattern — the one McDonald's Monopoly, Starbucks
For Life, NBA Playoff brackets, and most sweepstakes use:

  * Day 1 .. Day N progressive drops (a new piece, an advent reveal, a
    new bracket round, a new ticket entry).
  * Cumulative progress per user (collected pieces, opened doors,
    bracket survivors, ticket count).
  * Grand-prize redemption windows that often outlive the play window.
  * Per-region legal compliance (sweeps "no purchase necessary", AMOE,
    Quebec / Italy / Singapore exclusions, etc.).

This service introduces the **CampaignArc** primitive on top of the
existing campaign infrastructure. It is purely additive: an arc is a
*wrapper* around any number of single-session campaigns / standalone
plays. Existing campaign endpoints keep working unchanged.

Redis Schema (single namespace, ``arc:*``)
------------------------------------------
  arc:{arc_id}                       HASH  — arc definition (JSON fields)
  arc:{arc_id}:daily_drops           LIST  — JSON drop per day (index = day)
  arc:{arc_id}:participants          SET   — user ids who joined the arc
  arc:{arc_id}:user:{uid}            HASH  — per-user progress
  arc:{arc_id}:user:{uid}:pieces     SET   — collected piece ids
  arc:{arc_id}:user:{uid}:tickets    STR   — ticket count (INT)
  arc:{arc_id}:user:{uid}:claims     SET   — prize ids already claimed
  arc:{arc_id}:leaderboard           ZSET  — score per user (progress %)
  arc:{arc_id}:claims:{prize_id}     SET   — winners (cap by prize.quantity)
  arc:{arc_id}:emitted_drops         SET   — day_index strings already cron'd
  brand:{bid}:arcs                   SET   — arcs owned by brand

All writes are idempotent; the worker / endpoints can be re-run safely.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

ARC_KEY = "arc:{arc_id}"
ARC_DROPS_KEY = "arc:{arc_id}:daily_drops"
ARC_PARTICIPANTS_KEY = "arc:{arc_id}:participants"
ARC_USER_KEY = "arc:{arc_id}:user:{uid}"
ARC_USER_PIECES_KEY = "arc:{arc_id}:user:{uid}:pieces"
ARC_USER_TICKETS_KEY = "arc:{arc_id}:user:{uid}:tickets"
ARC_USER_CLAIMS_KEY = "arc:{arc_id}:user:{uid}:claims"
ARC_LEADERBOARD_KEY = "arc:{arc_id}:leaderboard"
ARC_PRIZE_CLAIMS_KEY = "arc:{arc_id}:claims:{prize_id}"
ARC_EMITTED_DROPS_KEY = "arc:{arc_id}:emitted_drops"
BRAND_ARCS_KEY = "brand:{bid}:arcs"

VALID_ARC_TYPES = {
    "monopoly_collect_n",   # collect N pieces to win
    "advent_calendar",      # daily reveal for X days
    "tournament_bracket",   # elimination rounds
    "sweepstakes_entries",  # each play = ticket, grand draw at end
}

VALID_ARC_STATUS = {"draft", "scheduled", "active", "ended", "redemption_only"}

DAY_SECONDS = 86400
DEFAULT_REDEMPTION_DAYS = 30  # post-arc claim window


# ── Arc Templates ───────────────────────────────────────────────────────


def _template_monopoly_collect_n(
    duration_days: int,
    piece_set: list[str],
    rare_piece_id: str | None = None,
) -> list[dict[str, Any]]:
    """McDonald's Monopoly pattern: collect N pieces → win grand prize.

    Each day drops a *common* piece with high frequency and (rarely) the
    rare piece that completes the set. The pacing is deliberately
    front-loaded with commons so users build collections fast, then taper.
    """
    drops: list[dict[str, Any]] = []
    if not piece_set:
        piece_set = [f"piece_{i}" for i in range(duration_days)]
    for day in range(duration_days):
        piece = piece_set[day % len(piece_set)]
        is_rare = rare_piece_id is not None and piece == rare_piece_id
        drops.append({
            "day": day,
            "type": "piece_drop",
            "piece_id": piece,
            "rare": is_rare,
            # Rare pieces drop with low probability (1/100k) on any given
            # day for any given user — controlled in get_today_play.
            "drop_probability": 0.00001 if is_rare else 1.0,
            "reveal_copy": (
                f"Day {day + 1}: collect the {piece} piece!"
            ),
        })
    return drops


def _template_advent_calendar(
    duration_days: int,
    daily_rewards: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Starbucks-For-Life pattern: a sealed door per day; reveal at 00:00."""
    drops: list[dict[str, Any]] = []
    for day in range(duration_days):
        reward = (
            daily_rewards[day]
            if daily_rewards and day < len(daily_rewards)
            else {"type": "voucher", "value_cents": 100}
        )
        drops.append({
            "day": day,
            "type": "advent_reveal",
            "door_id": f"door_{day + 1}",
            "reward": reward,
            "reveal_copy": f"Open door {day + 1} of {duration_days}!",
            "drop_probability": 1.0,
        })
    return drops


def _template_tournament_bracket(
    duration_days: int,
    bracket_size: int = 16,
) -> list[dict[str, Any]]:
    """NBA-Playoff-style elimination bracket; one round per N days."""
    drops: list[dict[str, Any]] = []
    # Compute rounds needed (log2 bracket_size).
    rounds = 1
    while (1 << rounds) < bracket_size:
        rounds += 1
    days_per_round = max(1, duration_days // rounds)
    for day in range(duration_days):
        round_idx = min(rounds - 1, day // days_per_round)
        survivors = max(1, bracket_size >> round_idx)
        drops.append({
            "day": day,
            "type": "bracket_round",
            "round": round_idx,
            "survivors": survivors,
            "reveal_copy": (
                f"Round {round_idx + 1}: {survivors} survivors remaining"
            ),
            "drop_probability": 1.0,
        })
    return drops


def _template_sweepstakes_entries(
    duration_days: int,
) -> list[dict[str, Any]]:
    """Each play = N tickets; grand draw on last day."""
    drops: list[dict[str, Any]] = []
    for day in range(duration_days):
        is_finale = day == duration_days - 1
        drops.append({
            "day": day,
            "type": "ticket_drop",
            "tickets_per_play": 1,
            "finale": is_finale,
            "reveal_copy": (
                f"Grand draw TODAY!" if is_finale
                else f"Day {day + 1}: every play earns 1 entry"
            ),
            "drop_probability": 1.0,
        })
    return drops


ARC_TEMPLATES = {
    "monopoly_collect_n": _template_monopoly_collect_n,
    "advent_calendar": _template_advent_calendar,
    "tournament_bracket": _template_tournament_bracket,
    "sweepstakes_entries": _template_sweepstakes_entries,
}


def build_daily_drops(
    arc_type: str,
    duration_days: int,
    **template_kwargs: Any,
) -> list[dict[str, Any]]:
    """Compile the per-day drop schedule for an arc type.

    Raises ValueError on unknown arc_type so callers fail fast at create
    time rather than silently producing an empty schedule.
    """
    if arc_type not in ARC_TEMPLATES:
        raise ValueError(
            f"arc_type must be one of {sorted(VALID_ARC_TYPES)}, got {arc_type}"
        )
    fn = ARC_TEMPLATES[arc_type]
    # Pass only the kwargs the template understands — keeps the public
    # entry point flexible without requiring callers to know each
    # template's signature.
    if arc_type == "monopoly_collect_n":
        return fn(
            duration_days=duration_days,
            piece_set=template_kwargs.get("piece_set", []),
            rare_piece_id=template_kwargs.get("rare_piece_id"),
        )
    if arc_type == "advent_calendar":
        return fn(
            duration_days=duration_days,
            daily_rewards=template_kwargs.get("daily_rewards"),
        )
    if arc_type == "tournament_bracket":
        return fn(
            duration_days=duration_days,
            bracket_size=template_kwargs.get("bracket_size", 16),
        )
    return fn(duration_days=duration_days)


# ── Arc dataclass ───────────────────────────────────────────────────────


@dataclass
class CampaignArc:
    """A multi-day campaign arc.

    The dataclass is the *in-memory* shape; persistence is done via
    ``save()`` which fans out to Redis. Methods that touch user state
    take a Redis handle so they integrate cleanly with the existing
    aioredis-based routers.
    """

    arc_id: str
    brand_id: str
    name: str
    duration_days: int
    arc_type: str  # one of VALID_ARC_TYPES
    daily_drops: list[dict[str, Any]] = field(default_factory=list)
    prize_pool: dict[str, Any] = field(default_factory=dict)
    redemption_window: dict[str, Any] = field(default_factory=dict)
    legal_compliance: dict[str, Any] = field(default_factory=dict)
    start_at: float = 0.0
    status: str = "draft"
    # Optional linkage: arcs can wrap any number of existing single-session
    # campaigns so the underlying ad spend keeps flowing through auction.
    wrapped_campaign_ids: list[str] = field(default_factory=list)
    # Arc-type-specific config (e.g. monopoly piece set, bracket size).
    config: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    # ── Construction helpers ────────────────────────────────────────

    @classmethod
    def new(
        cls,
        brand_id: str,
        name: str,
        duration_days: int,
        arc_type: str,
        prize_pool: dict[str, Any] | None = None,
        redemption_window: dict[str, Any] | None = None,
        legal_compliance: dict[str, Any] | None = None,
        start_at: float | None = None,
        wrapped_campaign_ids: list[str] | None = None,
        config: dict[str, Any] | None = None,
    ) -> "CampaignArc":
        """Build a fresh arc with sensible defaults + compiled daily_drops."""
        if arc_type not in VALID_ARC_TYPES:
            raise ValueError(
                f"arc_type must be one of {sorted(VALID_ARC_TYPES)}"
            )
        if duration_days < 1:
            raise ValueError("duration_days must be >= 1")
        now = time.time()
        start = start_at if start_at is not None else now
        cfg = config or {}
        drops = build_daily_drops(
            arc_type=arc_type, duration_days=duration_days, **cfg
        )
        # Default redemption window: open through arc end + 30 days.
        rwindow = redemption_window or {
            "opens_at": start,
            "closes_at": start + (duration_days + DEFAULT_REDEMPTION_DAYS) * DAY_SECONDS,
        }
        # Default legal: per-region empty allow-list = open worldwide;
        # the merchant is expected to fill this in for sweeps.
        legal = legal_compliance or {
            "amoe_required": arc_type == "sweepstakes_entries",
            "min_age": 18,
            "excluded_regions": [],
            "rules_url": None,
        }
        return cls(
            arc_id=f"arc_{uuid4().hex[:16]}",
            brand_id=brand_id,
            name=name,
            duration_days=duration_days,
            arc_type=arc_type,
            daily_drops=drops,
            prize_pool=prize_pool or {},
            redemption_window=rwindow,
            legal_compliance=legal,
            start_at=start,
            status="scheduled" if start > now else "active",
            wrapped_campaign_ids=wrapped_campaign_ids or [],
            config=cfg,
            created_at=now,
            updated_at=now,
        )

    # ── Persistence ────────────────────────────────────────────────

    def to_redis_mapping(self) -> dict[str, str]:
        return {
            "arc_id": self.arc_id,
            "brand_id": self.brand_id,
            "name": self.name,
            "duration_days": str(self.duration_days),
            "arc_type": self.arc_type,
            "prize_pool": json.dumps(self.prize_pool),
            "redemption_window": json.dumps(self.redemption_window),
            "legal_compliance": json.dumps(self.legal_compliance),
            "start_at": str(self.start_at),
            "status": self.status,
            "wrapped_campaign_ids": json.dumps(self.wrapped_campaign_ids),
            "config": json.dumps(self.config),
            "created_at": str(self.created_at),
            "updated_at": str(self.updated_at),
        }

    @classmethod
    def from_redis(
        cls,
        raw: dict[str, str],
        daily_drops: list[dict[str, Any]] | None = None,
    ) -> "CampaignArc":
        return cls(
            arc_id=raw["arc_id"],
            brand_id=raw["brand_id"],
            name=raw.get("name", ""),
            duration_days=int(raw.get("duration_days", "0") or 0),
            arc_type=raw.get("arc_type", "monopoly_collect_n"),
            daily_drops=daily_drops or [],
            prize_pool=_safe_loads(raw.get("prize_pool"), {}),
            redemption_window=_safe_loads(raw.get("redemption_window"), {}),
            legal_compliance=_safe_loads(raw.get("legal_compliance"), {}),
            start_at=float(raw.get("start_at", "0") or 0),
            status=raw.get("status", "draft"),
            wrapped_campaign_ids=_safe_loads(
                raw.get("wrapped_campaign_ids"), []
            ),
            config=_safe_loads(raw.get("config"), {}),
            created_at=float(raw.get("created_at", "0") or 0),
            updated_at=float(raw.get("updated_at", "0") or 0),
        )

    async def save(self, r: aioredis.Redis) -> None:
        """Persist arc + drops + brand index in one pipeline."""
        self.updated_at = time.time()
        pipe = r.pipeline()
        pipe.hset(
            ARC_KEY.format(arc_id=self.arc_id),
            mapping=self.to_redis_mapping(),
        )
        # Drops list — fully rewritten on save so updating an arc
        # template idempotently replaces the schedule.
        drops_key = ARC_DROPS_KEY.format(arc_id=self.arc_id)
        pipe.delete(drops_key)
        if self.daily_drops:
            pipe.rpush(drops_key, *[json.dumps(d) for d in self.daily_drops])
        pipe.sadd(
            BRAND_ARCS_KEY.format(bid=self.brand_id), self.arc_id
        )
        await pipe.execute()

    # ── Day-index math ─────────────────────────────────────────────

    def current_day_index(self, now: float | None = None) -> int:
        """Days since start_at, clamped to [0, duration_days - 1].

        Returns -1 when arc hasn't started yet, ``duration_days`` when it
        has ended (so callers can branch on "in play" vs "redemption").
        """
        ts = now if now is not None else time.time()
        if ts < self.start_at:
            return -1
        elapsed = ts - self.start_at
        day = int(elapsed // DAY_SECONDS)
        if day >= self.duration_days:
            return self.duration_days
        return day

    def get_today_play(self, day_index: int | None = None) -> dict[str, Any]:
        """Return today's drop spec.

        Pre-start arcs return a ``waiting`` payload; ended arcs return a
        ``ended`` payload so clients can render the redemption UI.
        """
        if day_index is None:
            day_index = self.current_day_index()

        if day_index < 0:
            return {
                "arc_id": self.arc_id,
                "status": "waiting",
                "starts_at": self.start_at,
                "seconds_until_start": max(0, int(self.start_at - time.time())),
            }
        if day_index >= self.duration_days:
            in_redemption = (
                time.time() < (self.redemption_window.get("closes_at") or 0)
            )
            return {
                "arc_id": self.arc_id,
                "status": "ended",
                "redemption_open": in_redemption,
                "redemption_closes_at": self.redemption_window.get(
                    "closes_at"
                ),
            }

        # In-play: return today's drop + arc-level metadata so the client
        # can render a "Day N of M" UI without an extra round-trip.
        if 0 <= day_index < len(self.daily_drops):
            drop = self.daily_drops[day_index]
        else:
            drop = {"day": day_index, "type": "unknown"}
        return {
            "arc_id": self.arc_id,
            "status": "in_play",
            "day_index": day_index,
            "day_label": f"Day {day_index + 1} of {self.duration_days}",
            "drop": drop,
            "prize_pool": self.prize_pool,
        }

    # ── Per-user progress ───────────────────────────────────────────

    async def join(self, r: aioredis.Redis, user_id: str) -> dict[str, Any]:
        """Idempotent: enrol a user in the arc."""
        added = await r.sadd(
            ARC_PARTICIPANTS_KEY.format(arc_id=self.arc_id), user_id
        )
        now = time.time()
        if added:
            await r.hset(
                ARC_USER_KEY.format(arc_id=self.arc_id, uid=user_id),
                mapping={
                    "joined_at": str(now),
                    "last_play_at": str(now),
                    "plays": "0",
                },
            )
            # New users start at the bottom of the leaderboard with 0.
            await r.zadd(
                ARC_LEADERBOARD_KEY.format(arc_id=self.arc_id),
                {user_id: 0.0},
            )
        return {"arc_id": self.arc_id, "user_id": user_id, "newly_joined": bool(added)}

    async def record_play(
        self,
        r: aioredis.Redis,
        user_id: str,
        day_index: int | None = None,
    ) -> dict[str, Any]:
        """Record a play event and dispense today's drop reward.

        For ``monopoly_collect_n`` this awards the day's piece.
        For ``advent_calendar`` this opens today's door if not yet opened.
        For ``tournament_bracket`` this records survival.
        For ``sweepstakes_entries`` this credits 1 ticket.
        Returns a dict describing what changed (for client toasts/UI).
        """
        await self.join(r, user_id)
        if day_index is None:
            day_index = self.current_day_index()
        if day_index < 0 or day_index >= self.duration_days:
            return {"ok": False, "reason": "arc_not_in_play"}

        drop = self.daily_drops[day_index] if day_index < len(self.daily_drops) else {}
        now = time.time()
        pipe = r.pipeline()
        pipe.hincrby(
            ARC_USER_KEY.format(arc_id=self.arc_id, uid=user_id), "plays", 1
        )
        pipe.hset(
            ARC_USER_KEY.format(arc_id=self.arc_id, uid=user_id),
            mapping={"last_play_at": str(now)},
        )

        awarded: dict[str, Any] = {"type": drop.get("type")}

        if self.arc_type == "monopoly_collect_n":
            piece = drop.get("piece_id")
            if piece:
                pipe.sadd(
                    ARC_USER_PIECES_KEY.format(
                        arc_id=self.arc_id, uid=user_id
                    ),
                    piece,
                )
                awarded["piece_id"] = piece
                awarded["rare"] = bool(drop.get("rare"))
        elif self.arc_type == "advent_calendar":
            door = drop.get("door_id")
            if door:
                # Idempotent: one door per user per day.
                pipe.sadd(
                    ARC_USER_PIECES_KEY.format(
                        arc_id=self.arc_id, uid=user_id
                    ),
                    door,
                )
                awarded["door_id"] = door
                awarded["reward"] = drop.get("reward")
        elif self.arc_type == "tournament_bracket":
            awarded["round"] = drop.get("round")
            awarded["survived"] = True
        elif self.arc_type == "sweepstakes_entries":
            tickets = int(drop.get("tickets_per_play", 1))
            pipe.incrby(
                ARC_USER_TICKETS_KEY.format(
                    arc_id=self.arc_id, uid=user_id
                ),
                tickets,
            )
            awarded["tickets_awarded"] = tickets

        await pipe.execute()

        # Update leaderboard outside the pipe — depends on post-play state.
        progress = await self.compute_progression(r, user_id)
        await r.zadd(
            ARC_LEADERBOARD_KEY.format(arc_id=self.arc_id),
            {user_id: progress.get("score", 0.0)},
        )

        return {
            "ok": True,
            "day_index": day_index,
            "awarded": awarded,
            "progress": progress,
        }

    async def compute_progression(
        self, r: aioredis.Redis, user_id: str
    ) -> dict[str, Any]:
        """Return the user's current arc progress (shape varies by type)."""
        user_key = ARC_USER_KEY.format(arc_id=self.arc_id, uid=user_id)
        pieces_key = ARC_USER_PIECES_KEY.format(
            arc_id=self.arc_id, uid=user_id
        )
        tickets_key = ARC_USER_TICKETS_KEY.format(
            arc_id=self.arc_id, uid=user_id
        )

        raw = await r.hgetall(user_key)
        plays = int(raw.get("plays", "0") or 0)

        if self.arc_type == "monopoly_collect_n":
            collected = await r.smembers(pieces_key)
            piece_set = self.config.get("piece_set") or []
            target_n = self.config.get("collect_n") or len(set(piece_set)) or 1
            unique = len(collected)
            score = min(1.0, unique / target_n) if target_n else 0.0
            return {
                "user_id": user_id,
                "arc_type": self.arc_type,
                "plays": plays,
                "pieces_collected": sorted(collected),
                "unique_pieces": unique,
                "target": target_n,
                "score": round(score, 4),
                "complete": unique >= target_n,
            }
        if self.arc_type == "advent_calendar":
            opened = await r.smembers(pieces_key)
            score = len(opened) / self.duration_days if self.duration_days else 0
            return {
                "user_id": user_id,
                "arc_type": self.arc_type,
                "plays": plays,
                "doors_opened": sorted(opened),
                "doors_opened_count": len(opened),
                "duration_days": self.duration_days,
                "score": round(score, 4),
                "complete": len(opened) >= self.duration_days,
            }
        if self.arc_type == "tournament_bracket":
            # Score = plays / duration_days proxy for survival depth.
            score = min(1.0, plays / max(1, self.duration_days))
            return {
                "user_id": user_id,
                "arc_type": self.arc_type,
                "plays": plays,
                "score": round(score, 4),
                "complete": plays >= self.duration_days,
            }
        if self.arc_type == "sweepstakes_entries":
            tickets_raw = await r.get(tickets_key)
            tickets = int(tickets_raw or 0)
            return {
                "user_id": user_id,
                "arc_type": self.arc_type,
                "plays": plays,
                "tickets": tickets,
                "score": float(tickets),
                "complete": False,  # determined at grand draw
            }
        return {
            "user_id": user_id,
            "arc_type": self.arc_type,
            "plays": plays,
            "score": 0.0,
        }

    # ── Claim eligibility ────────────────────────────────────────────

    async def can_user_claim(
        self,
        r: aioredis.Redis,
        user_id: str,
        prize_id: str,
        user_region: str | None = None,
    ) -> tuple[bool, str]:
        """Gate a prize claim against arc state + legal + progression.

        Returns ``(ok, reason)``. ``reason`` is "ok" on success and a
        machine-readable code on failure (used by the router to render
        merchant + user-facing copy).
        """
        # 1. Arc must exist & be in a claimable phase.
        now = time.time()
        if now < self.start_at:
            return False, "arc_not_started"
        opens_at = self.redemption_window.get("opens_at") or self.start_at
        closes_at = self.redemption_window.get("closes_at")
        if now < opens_at:
            return False, "redemption_not_open"
        if closes_at and now > closes_at:
            return False, "redemption_closed"

        # 2. Legal compliance — region exclusion.
        excluded = self.legal_compliance.get("excluded_regions") or []
        if user_region and user_region.upper() in {r.upper() for r in excluded}:
            return False, "region_excluded"

        # 3. Prize must exist in the pool. ``prize_pool`` is permissive:
        #    can be {grand: {...}, tier_2: {...}} or {prizes: [{id,...}]}.
        prize = self._find_prize(prize_id)
        if not prize:
            return False, "prize_not_in_pool"

        # 4. Capacity check — prizes have an optional quantity cap.
        cap = prize.get("quantity")
        if cap is not None:
            try:
                cap_int = int(cap)
            except (TypeError, ValueError):
                cap_int = 0
            already = await r.scard(
                ARC_PRIZE_CLAIMS_KEY.format(
                    arc_id=self.arc_id, prize_id=prize_id
                )
            )
            if cap_int > 0 and already >= cap_int:
                return False, "prize_exhausted"

        # 5. Already claimed?
        already_claimed = await r.sismember(
            ARC_USER_CLAIMS_KEY.format(arc_id=self.arc_id, uid=user_id),
            prize_id,
        )
        if already_claimed:
            return False, "already_claimed"

        # 6. Progression gate — depends on arc type.
        progress = await self.compute_progression(r, user_id)
        required_score = float(prize.get("required_score", 0.0))
        if self.arc_type == "monopoly_collect_n":
            # Monopoly: must hold every required_piece.
            required_pieces = set(prize.get("required_pieces") or [])
            if required_pieces:
                held = set(progress.get("pieces_collected") or [])
                if not required_pieces.issubset(held):
                    return False, "pieces_missing"
            elif required_score and progress.get("score", 0.0) < required_score:
                return False, "score_too_low"
        elif self.arc_type == "advent_calendar":
            min_doors = int(prize.get("required_doors", 0))
            if progress.get("doors_opened_count", 0) < min_doors:
                return False, "doors_missing"
        elif self.arc_type == "tournament_bracket":
            if progress.get("score", 0.0) < required_score:
                return False, "round_not_reached"
        elif self.arc_type == "sweepstakes_entries":
            # Sweepstakes grand draw can only be claimed once arc has ended.
            if now < (self.start_at + self.duration_days * DAY_SECONDS):
                return False, "draw_not_yet"
            min_tickets = int(prize.get("required_tickets", 1))
            if progress.get("tickets", 0) < min_tickets:
                return False, "insufficient_tickets"

        return True, "ok"

    async def record_claim(
        self,
        r: aioredis.Redis,
        user_id: str,
        prize_id: str,
    ) -> None:
        """Mark a prize as claimed (called after can_user_claim returned True)."""
        pipe = r.pipeline()
        pipe.sadd(
            ARC_USER_CLAIMS_KEY.format(arc_id=self.arc_id, uid=user_id),
            prize_id,
        )
        pipe.sadd(
            ARC_PRIZE_CLAIMS_KEY.format(
                arc_id=self.arc_id, prize_id=prize_id
            ),
            user_id,
        )
        await pipe.execute()

    def _find_prize(self, prize_id: str) -> dict[str, Any] | None:
        """Search the prize_pool for a prize entry by id.

        Supports two shapes for convenience:
          - ``{grand: {id, ...}, tier_2: {id, ...}}`` (named tiers)
          - ``{prizes: [{id, ...}, ...]}``           (flat list)
        """
        if not self.prize_pool:
            return None
        prizes_list = self.prize_pool.get("prizes")
        if isinstance(prizes_list, list):
            for p in prizes_list:
                if isinstance(p, dict) and p.get("id") == prize_id:
                    return p
        for k, v in self.prize_pool.items():
            if isinstance(v, dict) and (v.get("id") == prize_id or k == prize_id):
                return v
        return None


# ── Module-level helpers ────────────────────────────────────────────────


def _safe_loads(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default


async def load_arc(r: aioredis.Redis, arc_id: str) -> CampaignArc | None:
    """Hydrate an arc + its drops list from Redis."""
    raw = await r.hgetall(ARC_KEY.format(arc_id=arc_id))
    if not raw:
        return None
    drops_raw = await r.lrange(
        ARC_DROPS_KEY.format(arc_id=arc_id), 0, -1
    )
    drops = [_safe_loads(d, {}) for d in drops_raw]
    return CampaignArc.from_redis(raw, daily_drops=drops)


async def list_brand_arcs(
    r: aioredis.Redis, brand_id: str
) -> list[CampaignArc]:
    """Return every arc owned by ``brand_id`` (most-recent first)."""
    ids = await r.smembers(BRAND_ARCS_KEY.format(bid=brand_id))
    arcs: list[CampaignArc] = []
    for aid in ids:
        arc = await load_arc(r, aid)
        if arc:
            arcs.append(arc)
    arcs.sort(key=lambda a: a.created_at, reverse=True)
    return arcs


async def leaderboard(
    r: aioredis.Redis, arc_id: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Top-N players in the arc, highest score first."""
    raw = await r.zrevrange(
        ARC_LEADERBOARD_KEY.format(arc_id=arc_id),
        0,
        max(0, limit - 1),
        withscores=True,
    )
    return [
        {"rank": idx + 1, "user_id": uid, "score": float(score)}
        for idx, (uid, score) in enumerate(raw)
    ]


async def refresh_status(
    r: aioredis.Redis, arc: CampaignArc, now: float | None = None
) -> str:
    """Derive + persist arc status from clock + redemption window.

    Mirrors the campaign router pattern: keep status fresh without a cron.
    """
    ts = now if now is not None else time.time()
    if ts < arc.start_at:
        new_status = "scheduled"
    elif ts < arc.start_at + arc.duration_days * DAY_SECONDS:
        new_status = "active"
    else:
        closes_at = arc.redemption_window.get("closes_at") or 0
        new_status = "redemption_only" if ts < closes_at else "ended"
    if new_status != arc.status:
        arc.status = new_status
        await r.hset(
            ARC_KEY.format(arc_id=arc.arc_id),
            mapping={"status": new_status, "updated_at": str(time.time())},
        )
    return new_status


__all__ = [
    "CampaignArc",
    "VALID_ARC_TYPES",
    "VALID_ARC_STATUS",
    "build_daily_drops",
    "load_arc",
    "list_brand_arcs",
    "leaderboard",
    "refresh_status",
    "DAY_SECONDS",
    "DEFAULT_REDEMPTION_DAYS",
    "ARC_KEY",
    "ARC_DROPS_KEY",
    "ARC_PARTICIPANTS_KEY",
    "ARC_LEADERBOARD_KEY",
    "ARC_USER_KEY",
    "ARC_USER_PIECES_KEY",
    "ARC_USER_TICKETS_KEY",
    "ARC_USER_CLAIMS_KEY",
    "ARC_PRIZE_CLAIMS_KEY",
    "ARC_EMITTED_DROPS_KEY",
    "BRAND_ARCS_KEY",
]
