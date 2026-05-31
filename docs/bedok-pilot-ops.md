# Bedok Pilot Operations Toolkit (Wave I.G)

Operational playbook for supporting the Singapore F&B alpha cohort (Heng Heng
Kopi, Brew Lab, Aminah's Halal Hut, Common Man Coffee, Old Chang Kee).

**Status:** Active alpha · 5 merchants · Bedok 85 / Tampines / CBD
**Owner:** Founder + 1 ops contractor (part-time)
**Updated:** 2026-05-31

---

## 1. Daily WhatsApp Check-in (Hour 24, Day 3, Day 7, Day 14)

Send via WhatsApp Business at these touchpoints. Templates are pre-approved
for the WhatsApp Cloud API "utility" category (no marketing-template fees).

### Template T-24h — "Day 1 Check"
```
Hi {merchant_first_name},

Day 1 of your KiX campaign is done. Quick numbers:

✅ Plays:        {plays_24h}
✅ Registrations: {regs_24h}
✅ Redemptions:   {redeems_24h}
✅ Wallet spent:  S${spent_24h}

Anything confusing? Hit reply or call founder direct: +65 9XXX XXXX.
```

### Template T-72h — "Day 3 Pulse"
```
Hi {merchant_first_name},

72 hours in. Your CPA so far: S${cpa_72h}
Top-performing game: {top_game_name} ({top_game_plays} plays)

Two things worth knowing:
1. {insight_1_pos_or_neg}
2. {insight_2_actionable}

Want me to swap in a different game for the weekend? Reply "yes" / "no".
```

### Template T-7d — "Week 1 Wrap"
```
Hi {merchant_first_name},

Week 1 done — here's what we learned about your customers:

📊 New customers: {new_customers_7d}
🔄 14-day return rate (early signal): {return_rate_proj}%
💰 Effective CAC: S${effective_cac}
🏆 vs your TikTok/Meta last month: {comparison_text}

Quick 10-min call this week to plan Week 2? My slots: {calendar_link}
```

### Template T-14d — "Two-Week Review (decision point)"
```
Hi {merchant_first_name},

14 days = the data you need to decide if KiX is worth keeping in the stack.

Your numbers vs your S$1,200/month Facebook benchmark:
- KiX CAC:      S${kix_cac}
- FB CAC:       S${fb_cac_baseline}
- Plays/visit:  {plays_per_visit}
- Return rate:  {return_14d}%

I'll send the full PDF report tonight. Either way, no contract — you can
keep going or stop. What I'd love: 15 min on a call to hear what worked /
what felt clunky. Founder calendar: {calendar_link}
```

---

## 2. 24-Hour Post-Launch Checklist (Operator Self-Check)

Tick within 24 hours of first campaign live. Sent as a Notion checklist
or printed laminated card at the merchant's counter.

- [ ] **Wallet balance > S$200** (top up if low — auto-recharge ON?)
- [ ] **First 3 plays seen** (if zero, check QR placement + camera focus)
- [ ] **At least 1 registration** (if zero by H+6, banner text may be unclear)
- [ ] **Voucher template tested in person** (founder + 1 staff member redeem)
- [ ] **Staff knows the redemption code** (paper printout posted at register)
- [ ] **Cousin / spouse fraud-check** (test redemption from owner's phone → should be blocked)
- [ ] **Geofence radius tested** (walk 100m away, confirm push fires)
- [ ] **Photo of QR placement** sent to ops group chat
- [ ] **PDPA consent screen reviewed** (founder verifies BM + EN both shown for MY-launching merchants)
- [ ] **TikTok pixel firing** (if integrated, verify CompleteRegistration event)

---

## 3. Drop-off Alert Rules (Automated, run hourly)

Triggers an internal Slack notification + WhatsApp to founder.

```yaml
rules:
  - name: zero_plays_24h
    trigger: plays_in_last_24h == 0
    action: alert_founder
    severity: high
    reason: "Campaign live but no plays — likely QR placement issue"

  - name: registration_drop_rate
    trigger: registration_rate < 30%
    window: 6h
    action: alert_ops
    severity: medium
    reason: "Game text unclear or game too long — investigate"

  - name: redemption_zero_after_5_regs
    trigger: registrations >= 5 AND redemptions == 0
    window: 24h
    action: alert_founder + flag_for_call
    severity: high
    reason: "Customer doesn't know how to redeem — staff training gap"

  - name: wallet_below_50sgd
    trigger: wallet_balance < 50
    action: notify_merchant + auto_recharge_if_enabled
    severity: low

  - name: cpa_drift_high
    trigger: cpa_24h > cpa_baseline * 1.5
    window: 48h
    action: alert_ops_for_optimization
    severity: medium
    reason: "Campaign auction is too aggressive, consider lowering bid"
```

---

## 4. Founder Escalation Path

| Severity | Response time | Channel |
|---|---|---|
| Critical (down, payment failure) | 1 hour | Phone call |
| High (zero activity 24h+, fraud detected) | 4 hours | WhatsApp + email |
| Medium (CPA drift, low conversion) | 24 hours | WhatsApp |
| Low (wallet alerts, weekly recap) | 72 hours | Auto-template only |

---

## 5. Weekly Founder Office Hours

Every Friday 10am-12pm SGT. Open to all alpha-cohort merchants. Founder
+ 1 KiX team member available for unstructured questions, swap requests,
or just venting about a bad campaign. Recorded with merchant permission.

**WhatsApp invite:** Sent every Wed afternoon to alpha cohort.

---

## 6. Tooling References

- Daily campaign monitor: `app/workers/campaign_monitor.py` (cron 0 * * * *)
- Wallet alert worker: `app/workers/wallet_reconciliation_worker.py`
- WhatsApp send helper: TODO (`app/services/whatsapp_template.py` — not yet built)
- Slack alert webhook: env var `KIX_OPS_SLACK_WEBHOOK`

---

## 7. Status

- [x] **Build `app/services/whatsapp_template.py` — SHIPPED Wave K7** (13 tests, dry-run by default, send_bedok_template + schedule_bedok_followups + CLI)
- [ ] Get the 4 templates approved on Meta Business Manager (UTILITY category)
- [ ] Wire to delayed-job queue (dramatiq/arq) for fire_at scheduling
- [ ] Auto-generate weekly PDF for T+14d template
- [ ] Add per-merchant ops dashboard (founder-only)
- [ ] MY-specific templates (translate after first MY pilot signs)
