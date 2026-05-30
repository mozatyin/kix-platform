"""Realistic seed data for load testing.

Generates / loads:
- 100 SG F&B brands + 100 SEA F&B brands
- Campaigns per brand with mix of objectives
- Auction traffic patterns with peaks at lunch/dinner (SG TZ)

All data is synthetic. No real PII / payment info.
"""
from __future__ import annotations

import json
import math
import os
import random
import string
from dataclasses import dataclass, asdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import List, Optional

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True, parents=True)

BRANDS_FILE = DATA_DIR / "brands.json"
CAMPAIGNS_FILE = DATA_DIR / "campaigns.json"
CONSUMERS_FILE = DATA_DIR / "consumers.json"

SG_F_AND_B_NAMES = [
    "Toast Box", "Ya Kun", "Killiney", "Wee Nam Kee", "Tian Tian",
    "Maxwell Chicken Rice", "Tiong Bahru Bakery", "Old Chang Kee",
    "BreadTalk", "Crystal Jade", "Din Tai Fung", "Ichiban Boshi",
    "Genki Sushi", "Pepper Lunch", "Saboten", "Watami", "Sakae Sushi",
    "Astons", "Swensens", "Botak Jones", "Encik Tan", "Han's",
    "Toast Hub", "Soup Restaurant", "No Signboard", "Long Beach",
    "Jumbo Seafood", "Ah Yat Abalone", "Imperial Treasure", "Paradise Dynasty",
    "Putien", "Crystal Jade La Mian", "Hai Di Lao", "Beauty in the Pot",
    "Manhattan Fish Market", "Fish & Co", "Carl's Jr SG", "Burger King SG",
    "McDonald's SG", "KFC SG", "Texas Chicken", "Subway SG", "Pizza Hut SG",
    "Domino's SG", "Mos Burger SG", "Lotteria SG", "Shake Shack SG",
    "Five Guys SG", "Wendy's SG", "Boost Juice SG", "Liho", "Koi The",
    "Gong Cha", "ChaTime", "Each-a-Cup", "Playmade", "Mr Bean", "Old Town",
    "Coffee Bean SG", "Starbucks SG", "Costa Coffee SG", "Bacha Coffee",
    "Common Man Coffee", "Strangers' Reunion", "Tiong Hoe", "Nylon Coffee",
    "Chye Seng Huat", "Bukit Timah Roasters", "Apartment Coffee",
    "Atlas Coffeehouse", "PPP Coffee", "Drury Lane", "Plain Vanilla",
    "Tiger Sugar SG", "The Alley SG", "Hey Tea SG", "HEYTEA",
    "Mixue SG", "Luckin Coffee SG", "Tealive SG", "TenRen's Tea",
    "Sun Yi Tea", "Each Cup", "Rou Cha", "8 Tea House", "Each",
    "Loola SG", "Yong He", "126 Eating House", "328 Katong Laksa",
    "Hill Street Tai Hwa", "Founder Bak Kut Teh", "Song Fa Bak Kut Teh",
    "Tai Wah Pork Noodle", "Hong Kong Soya Sauce Chicken", "Liao Fan",
    "A Noodle Story", "Pasta Fresca", "Cedele", "Project Acai",
    "An Acai Affair", "Foodpanda Hawker", "WhyQ", "Grab F&B",
]
SEA_F_AND_B_NAMES = [
    "Mangkok Sayur", "Warung Sederhana", "Sate Khas Senayan",
    "Bakmi GM", "Solaria", "HokBen", "Es Teler 77", "Pizza Marzano",
    "PHD Pizza ID", "Excelso", "Anomali Coffee", "Kopi Kenangan",
    "Janji Jiwa", "Fore Coffee", "Tuku", "Tomoro Coffee",
    "Flash Coffee TH", "Cafe Amazon", "After You Dessert",
    "Mango Tree Cafe", "Som Tam Nua", "Pad Thai Thip Samai",
    "Wattana Panich", "Bangkok Bold Cafe", "Roast", "Greyhound Cafe",
    "Iberry", "Karmakamet Diner", "Audrey Cafe", "Broccoli Revolution",
    "Yenly Yours", "Pickle Sandwich", "Featherstone", "Toby's Estate TH",
    "Pacamara", "Saw Roastery", "Brave Roasters", "Roots Coffee",
    "Phil Coffee", "Bukruk", "Jojo Manila", "Jollibee", "Mang Inasal",
    "Greenwich PH", "Chowking", "Goldilocks", "Red Ribbon",
    "Max's Restaurant", "Mary Grace", "UCC Cafe Terrace", "Tim Ho Wan PH",
    "Wee Nam Kee MY", "Old Town MY", "PappaRich MY", "OldTown White Coffee",
    "Secret Recipe", "Black Canyon MY", "Kenny Rogers MY", "Marrybrown",
    "Texas Chicken MY", "A&W MY", "Pizza Hut MY", "Subway MY",
    "Famous Amos MY", "Beard Papa MY", "Bask Bear Coffee",
    "Zus Coffee", "Coffee Bean MY", "Starbucks MY", "Tealive MY",
    "Boost Juice MY", "Chatime MY", "Each-a-Cup MY", "Gong Cha MY",
    "Tiger Sugar MY", "The Alley MY", "Daboba", "Xing Fu Tang",
    "JinJja Chicken", "Korean Tofu House MY", "Seoul Garden MY",
    "Bonchon MY", "KyoChon MY", "BBQ Chicken MY", "Tony Roma's MY",
    "Italianni's MY", "Pizza Express MY", "Las Vacas", "El Cerdo",
    "Sushi King MY", "Sakae Sushi MY", "Sushi Mentai", "Sushi Tei MY",
    "Aoki Tei", "Tomoe MY", "Sushi Zanmai", "Genki Sushi MY",
    "Watami MY", "Pepper Lunch MY", "Sushi Express MY", "Hokkaido Santouka",
    "Marugame Udon MY", "Hanamaru Udon", "Ippudo MY", "Menya Musashi MY",
]

CAMPAIGN_OBJECTIVES = [
    "awareness", "consideration", "conversion", "retention", "loyalty",
]
CAMPAIGN_BUDGETS = [50, 100, 250, 500, 1000, 2500, 5000]


@dataclass
class Brand:
    brand_id: str
    name: str
    region: str  # "SG" or "SEA"
    category: str
    api_token: str  # mock JWT for load tests


@dataclass
class Campaign:
    campaign_id: str
    brand_id: str
    objective: str
    daily_budget: float
    bid_amount: float
    status: str  # "active" / "paused"


@dataclass
class Consumer:
    user_id: str
    kix_id: str
    region: str
    auth_token: str


def _short_id(prefix: str) -> str:
    return f"{prefix}_{''.join(random.choices(string.hexdigits.lower(), k=22))}"


def _mock_jwt(subject: str) -> str:
    # Not a real JWT — load tests run in mock-auth mode and only check shape.
    payload = "".join(random.choices(string.ascii_letters + string.digits, k=40))
    return f"mock.{subject}.{payload}"


def generate_brands() -> List[Brand]:
    brands: List[Brand] = []
    for name in SG_F_AND_B_NAMES[:100]:
        bid = _short_id("brd")
        brands.append(Brand(
            brand_id=bid, name=name, region="SG", category="F&B",
            api_token=_mock_jwt(bid),
        ))
    for name in SEA_F_AND_B_NAMES[:100]:
        bid = _short_id("brd")
        brands.append(Brand(
            brand_id=bid, name=name, region="SEA", category="F&B",
            api_token=_mock_jwt(bid),
        ))
    return brands


def generate_campaigns(brands: List[Brand], per_brand: int = 3) -> List[Campaign]:
    campaigns: List[Campaign] = []
    for b in brands:
        for _ in range(per_brand):
            campaigns.append(Campaign(
                campaign_id=_short_id("cmp"),
                brand_id=b.brand_id,
                objective=random.choice(CAMPAIGN_OBJECTIVES),
                daily_budget=float(random.choice(CAMPAIGN_BUDGETS)),
                bid_amount=round(random.uniform(0.05, 2.5), 2),
                status="active" if random.random() > 0.1 else "paused",
            ))
    return campaigns


def generate_consumers(n: int = 10000) -> List[Consumer]:
    consumers: List[Consumer] = []
    for _ in range(n):
        uid = _short_id("usr")
        consumers.append(Consumer(
            user_id=uid,
            kix_id=_short_id("kid"),
            region=random.choice(["SG", "MY", "ID", "PH", "TH"]),
            auth_token=_mock_jwt(uid),
        ))
    return consumers


def lunch_dinner_weight(now: Optional[datetime] = None) -> float:
    """Multiplier on consumer traffic to simulate real auction patterns.

    Peaks: 11:30-13:30 (lunch ×3.0), 18:00-21:00 (dinner ×3.5).
    Trough: 02:00-06:00 (×0.1).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    # Convert UTC to SG (UTC+8) for the curve; load gen still hits server now.
    h = (now.hour + 8) % 24 + now.minute / 60.0
    lunch = 3.0 * math.exp(-((h - 12.5) ** 2) / 1.5)
    dinner = 3.5 * math.exp(-((h - 19.5) ** 2) / 2.0)
    base = 1.0 if 7 <= h <= 23 else 0.2
    return max(0.1, base + lunch + dinner) / 4.5  # normalize so peak ~= 1.0


def seed(force: bool = False) -> None:
    """Generate and persist all seed files. Idempotent."""
    if not force and BRANDS_FILE.exists() and CAMPAIGNS_FILE.exists() and CONSUMERS_FILE.exists():
        return
    random.seed(20260530)
    brands = generate_brands()
    campaigns = generate_campaigns(brands)
    consumers = generate_consumers()
    BRANDS_FILE.write_text(json.dumps([asdict(b) for b in brands], indent=2))
    CAMPAIGNS_FILE.write_text(json.dumps([asdict(c) for c in campaigns], indent=2))
    CONSUMERS_FILE.write_text(json.dumps([asdict(c) for c in consumers], indent=2))


def load_brands() -> List[dict]:
    seed()
    return json.loads(BRANDS_FILE.read_text())


def load_campaigns() -> List[dict]:
    seed()
    return json.loads(CAMPAIGNS_FILE.read_text())


def load_consumers() -> List[dict]:
    seed()
    return json.loads(CONSUMERS_FILE.read_text())


if __name__ == "__main__":  # pragma: no cover
    seed(force=True)
    print(f"Seeded: {len(load_brands())} brands, "
          f"{len(load_campaigns())} campaigns, "
          f"{len(load_consumers())} consumers")
