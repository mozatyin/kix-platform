# Master Services Agreement (MSA) — Enterprise Template v1.0

**Status:** Draft for WongPartnership SG review (per docs/rfc-enterprise-tier.md §3)
**Date:** 2026-05-31
**Authoring:** Founder (engineering / business) — NOT a substitute for legal counsel
**To be reviewed by:** WongPartnership SG (or equivalent enterprise counsel in customer's jurisdiction)
**License to reuse:** Internal KiX template; for KiX-customer execution only

---

## Preamble

This Master Services Agreement (this "**MSA**") is made between:

**(1) Mozat Pte Ltd** (UEN 200103167W), incorporated in Singapore, with
    registered office at 79 Anson Road, Singapore 079906, operator of the
    KiX gamification platform (the "**Provider**"); and

**(2) [Customer legal entity name]** (UEN [..]), with registered office at
    [..] (the "**Customer**").

Each a "**Party**", together the "**Parties**".

**Effective Date:** [date of last signature]

---

## 1. Definitions

1.1. "**KiX Platform**" — the gamification SaaS made available at
     https://letskix.com and partner.letskix.com, including the merchant
     portal, the consumer app, and all APIs and SDKs documented at
     letskix.com/api-docs as updated from time to time.

1.2. "**Tracked Transaction**" — has the meaning set out in **Schedule A**
     (incorporated at https://letskix.com/landing/legal/new-customer-definition.html
     at git commit `[hash at signature time]`).

1.3. "**Consumer Data**" — any personal data (as defined in the Singapore
     Personal Data Protection Act 2012 and any equivalent privacy law of the
     Customer's jurisdiction) collected via the KiX Platform from end-users
     who play games associated with the Customer's brand.

1.4. "**Service Levels**" — has the meaning set out in **Schedule B**.

1.5. "**Confidential Information**" — any non-public information disclosed
     by one Party to the other, marked confidential or reasonably understood
     to be confidential, including pricing, customer lists, and source code.

---

## 2. The Service

2.1. **Grant.** Provider grants Customer a non-exclusive, non-transferable,
     limited license during the Term to use the KiX Platform to (a) launch
     and operate gamification campaigns for the brands listed in
     **Schedule C**, and (b) access Consumer Data captured via those
     campaigns subject to Sections 5 and 6.

2.2. **Updates.** Provider may modify the KiX Platform from time to time.
     For changes to the **Tracked Transaction** definition, the change-policy
     in Schedule A applies (30-day notice; new campaigns only).

2.3. **Beta features.** Beta-marked features may be withdrawn or changed
     without notice. Customer's use of beta features is at Customer's risk.

---

## 3. Fees and payment

3.1. **Pricing.** Per **Schedule D** (pricing schedule). Default pay-per-
     result model: Customer is charged only for Tracked Transactions, per
     the CPA or CPS rate set in Schedule D, subject to the wallet mechanic
     in §3.3.

3.2. **No subscription, no setup fee.** Customer is not obligated to pay
     any monthly minimum unless explicitly stated in Schedule D. The default
     position is pay-as-you-go.

3.3. **Wallet.** Customer maintains a prepaid wallet (in SGD or a supported
     local currency per Schedule D §4). Tracked Transactions are debited from
     the wallet at the rate in Schedule D §1. When the wallet falls below
     the threshold in Schedule D §3, an auto-top-up may be charged to
     Customer's saved payment method (Customer may disable auto-top-up).

3.4. **Currency.** All amounts in this MSA are in **SGD** unless Schedule D
     specifies otherwise. For local-currency display (RM, IDR, THB, etc),
     the FX-lock mechanic in [docs/rfc-myr-native-wallet.md] applies.

3.5. **Invoicing (enterprise tier only).** If Schedule D §5 designates
     net-30 invoicing instead of wallet, Provider will issue a GST/SST
     invoice within 5 business days of month-end. Customer pays within
     30 days via PayNow / FAST / wire transfer. Interest on overdue amounts:
     SIBOR + 4% per annum.

3.6. **Disputes.** Customer may dispute any Tracked Transaction within
     **90 days** of charge via the portal Wallet → Disputes page. Provider
     will review within 14 business days. Fraud-related credits are
     processed within 14 business days per Schedule A §5.

---

## 4. Service Levels and support

4.1. **Uptime.** Per Schedule B, Provider commits to **99.5% monthly uptime**
     for the merchant portal and event-receiving APIs (excluding documented
     maintenance windows and force majeure per §13).

4.2. **Response times.**
     - **P1** (Service unavailable / data loss): 4-hour response, target
       8-hour resolution
     - **P2** (Major feature broken): 24-hour response, target 3 business days
     - **P3** (Minor issue / question): best-effort, target 5 business days

4.3. **Account management.** Customer receives:
     - Named account contact at Provider for the first 12 months
       (Provider may rotate after Year 1 with 30-day notice)
     - Quarterly Business Review (QBR) calls
     - Founder direct line for first 12 months of enterprise relationship
     - Shared Slack / Teams channel (Customer-funded)

---

## 5. Data, privacy, and security

5.1. **Data Protection Addendum.** The Parties enter into the DPA at
     https://letskix.com/landing/legal/dpa.html as **Schedule E**, which
     governs processing of Consumer Data under SG-PDPA, MY-PDPA, GDPR
     (where applicable), and equivalent laws.

5.2. **Consumer Data ownership.** Consumer Data captured via Customer's
     campaigns is **co-owned** by Customer and Provider. Provider holds
     the data as processor for Customer's purposes; Provider may use
     hashed aggregates for platform improvement (no re-identification).

5.3. **Customer export.** Customer may export all Consumer Data tied to
     its brand_id at any time via CSV or API. Export remains available
     for 90 days after termination.

5.4. **Hashed PII to ad platforms.** Where Customer has authorised TikTok /
     Meta / Google integrations, Provider transmits hashed (SHA-256)
     consumer phone/email to those platforms. Raw PII is not transmitted.
     See Schedule E for full DPA.

5.5. **Data residency.** Default Singapore. MY-residency Q1 2027 (opt-in).
     Customer-specific residency available for enterprise tier at additional
     cost per Schedule D §6.

5.6. **Security.** Provider maintains: HTTPS everywhere, AES-256 at rest,
     SOC2-type-II commitment by Q4 2026, no production-database access for
     non-engineering staff, quarterly security review.

---

## 6. Brand exclusivity (optional, enterprise tier)

6.1. **Election.** Customer may elect brand exclusivity in **Schedule F**
     by paying the exclusivity premium specified in Schedule D §7.

6.2. **Scope.** Exclusivity prevents Provider from running competitor games
     (in the same Vertical, defined in Schedule F) on consumers who have
     completed a Customer-branded game **within 500m** of any of Customer's
     listed outlets (Schedule C), for **12 months** from election date.

6.3. **Exit.** Customer may exit exclusivity with 90 days' notice without
     penalty. Provider may not unilaterally terminate exclusivity during
     the 12-month term except for material breach per §10.

---

## 7. Intellectual property

7.1. **Provider IP.** The KiX Platform, including all code, designs,
     game library, and aggregate analytics, remains Provider's IP.

7.2. **Customer IP.** Customer's brand assets, voucher copy, and game
     configurations remain Customer's IP.

7.3. **Feedback.** Customer-submitted feedback to Provider is licensed to
     Provider non-exclusively for platform improvement.

---

## 8. Warranties

8.1. **Mutual.** Each Party warrants it has the authority to enter this
     MSA and that signature does not breach any third-party agreement.

8.2. **Provider warranties.** Provider warrants that:
     - It owns or is licensed to use all KiX Platform IP
     - It will use commercially reasonable efforts to maintain Service Levels
     - It will not knowingly introduce malware

8.3. **Customer warranties.** Customer warrants that:
     - Its brand assets do not infringe third-party IP
     - It will not use the KiX Platform for unlawful purposes
     - It will not attempt to reverse-engineer or circumvent the platform

8.4. **Disclaimer.** Except as expressly stated, the KiX Platform is
     provided **"as is"**. Provider does not warrant that the Platform will
     be uninterrupted or error-free (subject to Service Levels in §4).

---

## 9. Indemnification

9.1. **By Provider.** Provider will indemnify Customer against third-party
     IP infringement claims arising from Customer's authorised use of the
     KiX Platform, up to the indemnification cap in §11.

9.2. **By Customer.** Customer will indemnify Provider against claims
     arising from Customer's brand assets, Customer's voucher misuse, or
     Customer's breach of §5 or §8.3.

---

## 10. Term and termination

10.1. **Term.** This MSA begins on the Effective Date and continues until
      terminated under §10.2 or §10.3.

10.2. **Termination for convenience.** Either Party may terminate this MSA
      at any time with **30 days' written notice** to the other Party. No
      clawback or penalty applies; Customer pays only for Tracked
      Transactions accrued through the termination date.

10.3. **Termination for cause.** Either Party may terminate immediately if
      the other (a) commits a material breach not cured within 30 days of
      notice, (b) becomes insolvent, or (c) fails to pay an undisputed
      invoice within 60 days.

10.4. **Effect of termination.**
      - Customer's data remains exportable for 90 days post-termination
      - Outstanding wallet balances are refunded within 14 days
      - Confidentiality (§12) survives termination indefinitely
      - Brand exclusivity (§6) terminates immediately

---

## 11. Liability cap

11.1. **Cap.** Each Party's aggregate liability under this MSA is limited
      to the **greater of (a) S$50,000 or (b) the total fees paid by
      Customer to Provider in the 12 months preceding the event giving
      rise to liability**.

11.2. **Exclusions.** The cap does not apply to: (a) breach of
      confidentiality (§12), (b) IP indemnification (§9.1), (c) gross
      negligence or wilful misconduct, or (d) damages arising from breach
      of §5 (data protection).

11.3. **No consequential damages.** Neither Party is liable for indirect,
      consequential, or punitive damages.

---

## 12. Confidentiality

12.1. Each Party will keep the other's Confidential Information confidential
      and use it only as needed to perform this MSA. Obligation survives
      termination for **3 years** (perpetual for trade secrets).

12.2. Standard exclusions apply: information that is (a) public, (b) already
      known, (c) independently developed, or (d) lawfully received from a
      third party.

---

## 13. Force majeure

13.1. Neither Party is liable for delay or failure caused by events beyond
      reasonable control: natural disasters, war, terrorism, pandemic,
      government action, cloud-provider outage (e.g., AWS region-down),
      or critical-infrastructure failure.

13.2. The affected Party must notify the other within 5 business days and
      use reasonable efforts to mitigate. If force majeure persists >60 days,
      either Party may terminate without penalty.

---

## 14. Miscellaneous

14.1. **Governing law.** Singapore law. **Forum:** Singapore courts.
      MY-jurisdiction variant available per Schedule G (KL HC); HK variant
      available per Schedule G (HK HC).

14.2. **Entire agreement.** This MSA + all Schedules (A through G) is the
      entire agreement. Supersedes any prior MOU / proposal / pricing
      sheet. No oral modifications.

14.3. **Amendments.** In writing, signed by both Parties.

14.4. **Notices.** Email to the addresses in §14.5 with read-receipt
      requested. Material notices (termination, breach) also sent by
      registered post or courier.

14.5. **Notice addresses.**
      Provider: enterprise@letskix.com / 79 Anson Road, Singapore 079906
      Customer: [..]

14.6. **Assignment.** Neither Party may assign without the other's written
      consent (not unreasonably withheld). Permitted assignment to an
      affiliate or in connection with a sale of substantially all assets.

14.7. **Survival.** §5, §7, §9, §10.4, §11, §12, §14 survive termination.

---

## Schedules

- **Schedule A** — Tracked Transaction definition
  (https://letskix.com/landing/legal/new-customer-definition.html, git commit at signature)
- **Schedule B** — Service Levels (99.5% uptime, P1/P2/P3 response targets per §4)
- **Schedule C** — Customer's brands and outlet list (Customer to populate)
- **Schedule D** — Pricing schedule (CPA / CPS / CPM rates, wallet thresholds,
  currency, invoicing terms, exclusivity premium)
- **Schedule E** — Data Processing Addendum
  (https://letskix.com/landing/legal/dpa.html)
- **Schedule F** — Brand exclusivity election (vertical, radius, term, exit)
- **Schedule G** — Jurisdiction variant (SG default, MY / HK opt-in)

---

## Signature block

| | Provider (Mozat Pte Ltd) | Customer ([entity]) |
|---|---|---|
| Signature: | _________________ | _________________ |
| Name: | [Founder, signing as Director] | [..] |
| Title: | Director | [..] |
| Date: | | |
| Schedule A git commit at signature: | `[hash]` | |

---

## Drafting notes (delete before sending)

- This template is **not** legal advice. WongPartnership review required before
  first execution.
- §6 (exclusivity) is opt-in only — leave out of self-serve customer MSAs.
- §11 (liability cap) is conservative for a Singapore SMB-tier counterparty;
  enterprise tier may push for higher cap (negotiable up to S$250K with
  Provider founder approval).
- §3.5 (invoicing) — only applies to net-30 enterprise customers; default is
  wallet auto-top-up.
- Schedule A is INTENTIONALLY a URL + git commit reference so changes are
  versioned. Print the actual page text into the executed MSA as an
  attachment if the Customer wants a frozen copy.
- For US / UK / AU customers, replace §14.1 with appropriate governing-law
  and forum-selection clauses; consult counsel.
- Last reviewed: 2026-05-31, founder + DRAFT (no legal review yet).
