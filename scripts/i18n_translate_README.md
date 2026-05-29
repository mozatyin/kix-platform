# i18n_translate — LLM-powered batch translator

Production tool that turns the EN-SG Fluent catalog into translated
catalogs for any number of target locales. Default model: Claude Haiku
4.5 (cheap; ~$0.001/word). All LLM calls are quota-guarded by
`scripts.llm_quota_monitor.wait_if_paused`.

Companion script: `scripts/i18n_glossary.py` — terminology manager
that controls which terms the LLM is **not allowed to translate** (KiX,
KiX ID, Soul, Toast Box, …) and per-locale canonical UI labels.

Strategy reference: `/Users/mozat/a-docs/i18n-trinity-strategy.md` §4.4
(LLM-assisted translation) and §4.8 (cost projections).

---

## 1. Workflow

```text
  app/i18n/catalogs/en-SG/main.ftl  (source-of-truth)
                │
                ▼
   ┌─────────────────────────────────────┐
   │  i18n_translate.py                  │
   │  ─ extract strings                  │
   │  ─ TM cache lookup (Redis 30-day)   │
   │  ─ batch 20/call → Claude Haiku 4.5 │
   │  ─ glossary slice injected          │
   │  ─ rebuild Fluent AST               │
   └─────────────────────────────────────┘
                │
                ▼
  app/i18n/catalogs/<locale>/main.ftl
                +
       (optional) review_<locale>.html
       (optional) brand_translations.auto_translated=true rows
```

## 2. CLI cheatsheet

```bash
# Cost estimate only — does not call the LLM
python -m scripts.i18n_translate --estimate-only --target zh-Hans-SG

# Single locale, real translation (requires ANTHROPIC_API_KEY)
python -m scripts.i18n_translate --target zh-Hans-SG

# Multiple locales at once
python -m scripts.i18n_translate --locales zh-Hans-SG,id-ID,ms-MY

# Review-mode: emit human-review HTML next to the FTL
python -m scripts.i18n_translate --target zh-Hans-SG --review-mode

# Custom source FTL
python -m scripts.i18n_translate --source app/i18n/catalogs/en-SG/main.ftl --target ja-JP

# Mock mode (CI; no LLM call, no API key needed)
python -m scripts.i18n_translate --target zh-Hans-SG --dry-run
```

## 3. Glossary

```bash
# List the global do-not-translate + technical glossary
python -m scripts.i18n_glossary --list

# List merged glossary for a target locale (global + locale overrides)
python -m scripts.i18n_glossary --list --locale zh-Hans-SG

# Add a do-not-translate term (writes app/i18n/glossary/global.json)
python -m scripts.i18n_glossary --add kix_pay --source-term "KiX Pay" --dnt

# Add a per-locale UI label
python -m scripts.i18n_glossary --add ui.scan --source-term "Scan" \
    --translation "扫一扫" --locale zh-Hans-SG --category ui_label
```

REST surface (admin requires `x-kix-admin-token` header):

| Method | Path                                | Behaviour                       |
|--------|-------------------------------------|---------------------------------|
| GET    | `/api/v1/i18n/glossary`             | List global glossary            |
| GET    | `/api/v1/i18n/glossary/{locale}`    | Merged per-locale glossary      |
| PUT    | `/api/v1/i18n/glossary/term`        | Admin upsert (token-gated)      |
| DELETE | `/api/v1/i18n/glossary/term/{id}`   | Admin remove (token-gated)      |
| GET    | `/api/v1/i18n/glossary/admin/tm-stats` | Translation-memory hit rate  |

## 4. Translation memory (TM)

Redis-backed key layout:

```text
i18n:tm:<sha1(source)[:16]>:<locale>         → translation string  (TTL 30d)
i18n:tm:stats:<locale>                        → HASH {hits, writes, misses}
```

A TM hit avoids the LLM call entirely — pure cache lookup. The
`GET /api/v1/i18n/glossary/admin/tm-stats` endpoint surfaces per-locale
hit rates so we can verify cache efficiency in production.

When `REDIS_URL` is unset (tests, local dev) the cache falls back to an
in-process dict for the duration of the run.

## 5. Cost model

Driven by `estimate_cost(strings, locales)`:

- Heuristic: 1.4 input tokens per English word
- Output tokens ~ `1.5 × input tokens` (CJK expansion factor)
- Pricing: Claude Haiku 4.5 — $1/M input + $5/M output

Example (3,000 strings × 4 locales):

```text
Source strings:      3,000
Approx words:        ~12,000
Batches per locale:  150 (size=20)
Cost per locale:     ~$0.30
Total LLM cost:      ~$1.20
```

The strategy doc estimates ~$1,500 per locale **including** human
review (1 reviewer × ~6 days at 500 strings/day). The LLM piece alone
is rounding error; the cost is the reviewer.

## 6. Quota guard contract

Every batch LLM call goes through
`scripts.llm_quota_monitor.wait_if_paused` first. When the
Anthropic-API usage is ≥95% the monitor writes
`kix:llm:quota:paused=1` to Redis; we block (up to 1 h) until it
clears (back to <90%). This is mandatory per repo policy — do not
remove the call.

## 7. Review workflow

`--review-mode` emits a self-contained HTML page with columns:
`key | source | translation | confidence | glossary | actions`.
The action buttons POST to
`/api/v1/admin/translations/mark-reviewed` (existing endpoint owned by
Agent 7's `brand_translation_service.mark_reviewed`).

A single reviewer can process ~500 strings/day with this UX (strategy
doc §4.4); 3,000 strings → 6 days per locale.

## 8. Tests

```bash
.venv/bin/python -m pytest tests/test_i18n_translate.py -v
```

Twelve standalone tests; no Redis, no DB, no live LLM. The translator
is run in `--dry-run` / mocked mode for every check.

## 9. Non-goals (for this slice)

- Live translation of brand-content fields — that path lives in
  `app/services/brand_translation_service.py::bulk_translate_brand`
  and is reachable through the brand-translation admin router.
- Plural-rule rewriting — we preserve the source plural shape and
  let the LLM see it, but write-back keeps the original AST for plural
  messages. Phase 2 lifts this constraint.
- HTML/JS string extraction — handled by `scripts/i18n_extract_html.py`
  and `scripts/i18n_extract.py`. This tool only translates Fluent.
