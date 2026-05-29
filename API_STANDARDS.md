# KiX Platform API Standards

> Public commitment to external integrators.
> Source of truth: [`app/api_standards.py`](app/api_standards.py).
> Audit origin: Trinity-D, 2026-05-29.

The KiX Platform exposes ~700 endpoints across 50+ routers. To keep SDK
authoring and client integration sane, every public HTTP endpoint obeys the
**five invariants** below. New endpoints MUST use the shim helpers in
`app.api_standards`; existing endpoints are being migrated incrementally.

---

## 1. ID Format

**Shape:** `<prefix>_<22-char-hex>`

- Prefix is a short snake_case tag identifying the resource family.
- Tail is 22 lowercase hex characters (≈88 bits of entropy, drawn from a
  truncated `uuid4`).
- IDs are case-sensitive and immutable for the lifetime of the resource.

### Standard prefixes

| Prefix   | Resource family                          |
|----------|-------------------------------------------|
| `acct_`  | Account (B2B entity)                      |
| `user_`  | Generic user                              |
| `kid_`   | KiX universal user identity               |
| `ent_`   | Non-human entity (pet, vehicle, property) |
| `lst_`   | Listing                                   |
| `ofr_`   | Offer                                     |
| `med_`   | Media                                     |
| `cmp_`   | Campaign                                  |
| `adg_`   | AdGroup                                   |
| `bdg_`   | Badge                                     |
| `qst_`   | Quest                                     |
| `vid_`   | Voucher instance                          |
| `res_`   | Reservation                               |
| `led_`   | Ledger entry                              |
| `inc_`   | Incident                                  |
| `tx_`    | Transaction                               |
| `sub_`   | Subscription                              |
| `pm_`    | Payment method                            |
| `dpt_`   | Deposit                                   |
| `prt_`   | Partnership                               |
| `crv_`   | Creative                                  |

### Helpers

```python
from app.api_standards import mint_id, parse_id, is_valid_id

new_campaign_id = mint_id("cmp")                # "cmp_8f3a1c…"
prefix, tail   = parse_id(new_campaign_id)      # ("cmp", "8f3a1c…")
ok             = is_valid_id(new_campaign_id, "cmp")
```

---

## 2. Timestamps

**Wire format:** `int` (Unix seconds, UTC).

Every `created_at`, `updated_at`, `expires_at`, `started_at`, `ended_at`,
`charged_at`, ... is an integer. ISO-8601 strings are reserved for
human-facing UI rendering and the helper `ts_to_iso(...)`.

```python
from app.api_standards import now_ts, ts_to_iso

created_at = now_ts()                           # 1748534400
display    = ts_to_iso(created_at)              # "2026-05-29T12:00:00+00:00"
```

Rationale: integer seconds collapse three classes of bugs (timezone drift,
millisecond/microsecond mixing, ISO parsing inconsistency) into a single
unambiguous value. Sub-second precision is not part of the public contract;
endpoints that need it carry an explicit `*_ms` companion field.

---

## 3. Error Response Envelope

Every error response — regardless of status code — uses the same shape:

```json
{
  "detail": {
    "error":   "not_found",
    "message": "campaign not found",
    "resource": "campaign",
    "resource_id": "cmp_8f3a1c…"
  }
}
```

- `error` is a stable, machine-readable snake_case code. SDKs pattern-match
  on this field; it never changes once shipped.
- `message` is a human string. May be localized in the future.
- Additional fields carry structured context (`field`, `available_cents`,
  `retry_after_seconds`, ...).

### Canonical error codes

| Status | `error`              | Helper                           |
|--------|----------------------|----------------------------------|
| 400    | `bad_request`        | `error_response(400, …)`         |
| 401    | `unauthorized`       | `unauthorized()`                 |
| 402    | `insufficient_funds` | `insufficient_funds(avail, req)` |
| 403    | `forbidden`          | `forbidden()`                    |
| 404    | `not_found`          | `not_found(resource, id)`        |
| 409    | `conflict`           | `conflict(resource, **ctx)`      |
| 422    | `validation_failed`  | `validation_failed(field, why)`  |
| 429    | `rate_limited`       | `rate_limited(retry_after=…)`    |
| 500    | `internal_error`     | `error_response(500, …)`         |

```python
from app.api_standards import not_found, insufficient_funds

raise not_found("campaign", id=cmp_id)
raise insufficient_funds(available=120_00, requested=500_00)
```

---

## 4. List Response Contract

Every collection endpoint returns the same envelope:

```json
{
  "items":    [ … ],
  "count":    25,
  "total":    1342,
  "has_more": true,
  "limit":    25,
  "offset":   0
}
```

- `items` — the current page.
- `count` — `len(items)` (always equals `items.length`).
- `total` — total matching rows before pagination, when computable.
- `has_more` — `True` when more rows exist beyond this page.
- `limit` / `offset` — echo of the request paging parameters.

```python
from app.api_standards import list_response

return list_response(items=rows, total=row_count, limit=50, offset=0)
```

Cursor-based endpoints add a `next_cursor` field but keep the same other
keys for SDK compatibility.

---

## 5. HTTP Method Semantics

| Verb + Path                       | Status | Body                                  |
|-----------------------------------|--------|---------------------------------------|
| `POST /resources`                 | 201    | Full resource representation          |
| `POST /resources/{id}/action`     | 200    | New state of resource                 |
| `PUT /resources/{id}`             | 200    | Full replaced resource                |
| `PATCH /resources/{id}`           | 200    | Partially-updated resource            |
| `DELETE /resources/{id}`          | 204    | No body                               |
| `GET /resources`                  | 200    | `list_response(...)` envelope         |
| `GET /resources/{id}`             | 200    | Single resource object                |

- `POST` on a collection always creates; the response is the freshly minted
  object including its server-assigned `<prefix>_<hex>` id and timestamps.
- `POST` on a sub-path (`/action`) is the canonical way to express state
  transitions (`/pause`, `/resume`, `/redeem`, `/void`).
- Idempotency: `PUT` and `DELETE` are idempotent. `POST` on a collection
  honours an optional `Idempotency-Key` request header.

---

## Migration policy

- New routers (added after 2026-05-29) MUST import from `app.api_standards`.
- Existing routers continue to work; they will be migrated alongside other
  changes (no mass refactor).
- Breaking changes to this document require a major version bump and a
  deprecation window of at least 90 days.
