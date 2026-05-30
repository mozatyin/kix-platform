"""country_slots: extend seed to all 249 ISO 3166-1 alpha-2 codes (Wave H Opp D).

Migration 0010 seeded 36 launch countries. Per v4 deck and Bible ADR #11
the promise is "200+ countries Day-1". This migration backfills the
remaining 213 ISO codes so any merchant in any jurisdiction can claim
a founding slot on signup — true global Day-1.

Idempotent: ON CONFLICT DO NOTHING preserves existing claims.
"""
from __future__ import annotations

from alembic import op

revision = "0011_country_slots_all_iso"
down_revision = "0010_country_slots"
branch_labels = None
depends_on = None


# All 249 ISO 3166-1 alpha-2 codes (assigned + reserved)
ALL_ISO = [
    "AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT", "AU", "AW", "AX", "AZ",
    "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI", "BJ", "BL", "BM", "BN", "BO", "BQ", "BR", "BS",
    "BT", "BV", "BW", "BY", "BZ",
    "CA", "CC", "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN", "CO", "CR", "CU", "CV", "CW",
    "CX", "CY", "CZ",
    "DE", "DJ", "DK", "DM", "DO", "DZ",
    "EC", "EE", "EG", "EH", "ER", "ES", "ET",
    "FI", "FJ", "FK", "FM", "FO", "FR",
    "GA", "GB", "GD", "GE", "GF", "GG", "GH", "GI", "GL", "GM", "GN", "GP", "GQ", "GR", "GS", "GT",
    "GU", "GW", "GY",
    "HK", "HM", "HN", "HR", "HT", "HU",
    "ID", "IE", "IL", "IM", "IN", "IO", "IQ", "IR", "IS", "IT",
    "JE", "JM", "JO", "JP",
    "KE", "KG", "KH", "KI", "KM", "KN", "KP", "KR", "KW", "KY", "KZ",
    "LA", "LB", "LC", "LI", "LK", "LR", "LS", "LT", "LU", "LV", "LY",
    "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK", "ML", "MM", "MN", "MO", "MP", "MQ", "MR", "MS",
    "MT", "MU", "MV", "MW", "MX", "MY", "MZ",
    "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP", "NR", "NU", "NZ",
    "OM",
    "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM", "PN", "PR", "PS", "PT", "PW", "PY",
    "QA",
    "RE", "RO", "RS", "RU", "RW",
    "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM", "SN", "SO", "SR", "SS",
    "ST", "SV", "SX", "SY", "SZ",
    "TC", "TD", "TF", "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TO", "TR", "TT", "TV", "TW", "TZ",
    "UA", "UG", "UM", "US", "UY", "UZ",
    "VA", "VC", "VE", "VG", "VI", "VN", "VU",
    "WF", "WS",
    "YE", "YT",
    "ZA", "ZM", "ZW",
]

assert len(ALL_ISO) == 249, f"Expected 249, got {len(ALL_ISO)}"


def upgrade() -> None:
    # Idempotent backfill — only inserts countries not already seeded
    for cc in ALL_ISO:
        op.execute(
            f"""
            INSERT INTO country_slots (country_code, slot_number)
            SELECT '{cc}', s
            FROM generate_series(1, 100) AS s
            ON CONFLICT (country_code, slot_number) DO NOTHING
            """
        )


def downgrade() -> None:
    # Only delete UNCLAIMED slots — preserve any claimed founding-merchant
    # commitments. Reversal must be surgical, not destructive.
    op.execute(
        """
        DELETE FROM country_slots
        WHERE claimed_at IS NULL
          AND country_code NOT IN (
              'SG', 'ID', 'TH', 'VN', 'PH', 'MY', 'HK', 'TW', 'CN',
              'US', 'GB', 'AU', 'NZ', 'IN', 'PK', 'BD', 'LK',
              'JP', 'KR', 'AE', 'SA', 'EG',
              'BR', 'MX', 'AR', 'CL',
              'DE', 'FR', 'ES', 'IT', 'NL', 'BE', 'PL',
              'TZ', 'KH', 'MM', 'LA'
          )
        """
    )
