# KiX Platform — Security Best Practices

Companion to `docs/security-audit.md`. Sections:

1. Secure coding standards
2. Pre-commit + CI tooling
3. Top-10 remediations with code snippets
4. Penetration-testing checklist
5. Compliance mapping (SOC 2 / ISO 27001 / PCI DSS / GDPR / PIPL)
6. Bug bounty program spec
7. Threat-modeling template

---

## 1. Secure coding standards

### 1.1 Authentication

* Every router that mutates state **must** declare a `Depends(...)` that
  returns an authenticated principal.
* Anonymous endpoints belong in a deliberate allowlist
  (`app/main.py:_ANONYMOUS_ROUTES`) reviewed in code review.
* No string equality on tokens, ever — use `app.security.constant_time_eq`.
* Secrets come from env. No defaults. If env var is unset → `Settings()`
  raises on boot.

### 1.2 Authorisation

* The dependency that returns the principal exposes `brand_id`, `role`,
  `scopes`. Routers compare to path/body brand_id.
* Read-only IDs (`asset_id`, `media_id`, `voucher_code`) are not enough
  to authorise access — always load the row and check ownership.
* Default deny. New scopes are opt-in.

### 1.3 Crypto

* JWT: RS256 / EdDSA only. Symmetric HS256 only for short-lived
  internal tokens between trusted services.
* Hashing user secrets: bcrypt cost ≥ 12, argon2id preferred for new code.
* Hashing for indexing / bucketing: sha256 (or blake2b). Never md5/sha1.
* HMAC for signing webhooks: sha256, key per merchant, rotated quarterly.
* All TLS: 1.2+; no fallback to TLS 1.0/1.1.

### 1.4 Input validation

* Pydantic models declare every field. **Never** `extra="allow"`.
* Strings have a `max_length`; ints have `ge`/`le`.
* URLs use `HttpUrl` + a centralised `_safe_url()` for SSRF guard.
* File uploads validate both MIME type and byte size *before* writing
  to storage.

### 1.5 Output / logging

* Never log raw email, phone, PAN, token, password, or JWT.
* Use `hash_pii(value)` helper (sha256, 12-char truncate).
* All money flows write to the durable PG audit log
  (`audit_log_service.record_event_fire_and_forget`).
* Forensic Redis lists are not audit logs; they are diagnostic windows.

### 1.6 Outbound HTTP

* All `httpx.AsyncClient` calls go through `app/services/outbound_http.py`
  which enforces:
  * `scheme = "https"` (exceptions whitelisted)
  * resolved IP not in RFC 1918 / link-local / loopback / IMDS
  * destination host in the integration's allowlist
  * `follow_redirects=False`, validate each redirect manually
  * timeout ≤ 30 s, max body 10 MB

---

## 2. Tooling

### 2.1 Pre-commit (`.pre-commit-config.yaml`)

```yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks: [ { id: gitleaks } ]
  - repo: https://github.com/PyCQA/bandit
    rev: 1.7.5
    hooks: [ { id: bandit, args: ["-r", "app/", "--severity", "medium"] } ]
  - repo: https://github.com/PyCQA/flake8
    rev: 6.1.0
    hooks: [ { id: flake8 } ]
  - repo: local
    hooks:
      - id: kix-security-audit
        name: kix security audit
        entry: python scripts/security_audit.py --severity p0
        language: system
        pass_filenames: false
```

### 2.2 CI (GitHub Actions)

```yaml
- name: pip-audit
  run: pip install pip-audit && pip-audit --strict
- name: trufflehog (verified only)
  run: docker run trufflesecurity/trufflehog:latest git . --only-verified
- name: trivy (container)
  run: trivy image kix-platform:${{ github.sha }}
- name: checkov (IaC)
  run: pip install checkov && checkov -d deployment/
- name: kix-security-audit
  run: python scripts/security_audit.py --severity p1
```

### 2.3 Production runtime

* `fail2ban` on the load balancer for /login / /token endpoints.
* `wal-g` encrypted PG backups; restore drill quarterly.
* AWS GuardDuty / equivalent on the VPC.
* Cloudflare WAF in front of all public endpoints.

---

## 3. Top-10 P0 remediations (file:line + before/after)

### 3.1 A07-001 — delete hardcoded admin operator

**File:** `app/routers/portal_auth.py:42-46`

```python
# BEFORE
_DEV_OPERATOR_EMAIL = "admin@kix.app"
_DEV_OPERATOR_PASSWORD_HASH = bcrypt.hashpw(b"kix-admin-dev", bcrypt.gensalt(rounds=12))
_DEV_OPERATOR_BRAND_ID = "all"
```

```python
# AFTER — delete entirely; replace _verify_dev_operator with a no-op.
# Bootstrap the first admin once via:
#     python -m app.cli bootstrap-admin --email ops@yourco.com
# which writes to portal_operator:* in Redis and prints a random password.
```

### 3.2 A02-001 — make JWT secret required

**File:** `app/config.py:39-44`

```python
# BEFORE
jwt_secret: str = "kix-dev-secret-change-in-production"
qr_signing_secret: str = "kix-qr-secret-change-in-production"
```

```python
# AFTER
from pydantic import SecretStr

jwt_secret: SecretStr           # raises ValidationError on boot if missing
qr_signing_secret: SecretStr

@field_validator("jwt_secret", "qr_signing_secret")
@classmethod
def _len_ge_32(cls, v: SecretStr) -> SecretStr:
    if len(v.get_secret_value()) < 32:
        raise ValueError("secret must be ≥ 32 chars")
    return v
```

### 3.3 A02-002 — remove ADMIN_TOKEN_DEFAULT

**File:** `app/quality_score.py:98, 148`

```python
# BEFORE
ADMIN_TOKEN_DEFAULT = "admin-dev-token"
expected = os.getenv("KIX_ADMIN_TOKEN", ADMIN_TOKEN_DEFAULT)
```

```python
# AFTER — share the helper in app.security
from app.security import check_admin_token

def _check_admin_token(token: str) -> None:
    if not check_admin_token(token):
        raise HTTPException(status_code=403, detail="invalid admin token")
```

### 3.4 A01-001 — auth on wallet routes

**File:** `app/routers/wallet.py:805`

```python
# BEFORE
@router.post("/{brand_id}/topup", response_model=TopupResponse)
async def create_topup(brand_id: str, body: TopupRequest, r=Depends(get_redis)):
    ...
```

```python
# AFTER
from app.deps import require_brand_operator   # new helper

@router.post("/{brand_id}/topup", response_model=TopupResponse)
async def create_topup(
    brand_id: str,
    body: TopupRequest,
    operator: dict = Depends(require_brand_operator),
    r=Depends(get_redis),
):
    if operator["brand_id"] not in {brand_id, "all"}:
        raise HTTPException(403, "brand_mismatch")
    ...
```

### 3.5 A10-001 — SSRF guard on upload-from-url

**File:** `app/routers/assets.py:499`

```python
# BEFORE
async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
    resp = await client.get(str(body.source_url))
```

```python
# AFTER
from app.services.outbound_http import safe_get   # new helper

resp = await safe_get(
    str(body.source_url),
    timeout=20.0,
    max_redirects=2,
    require_scheme="https",
    deny_private=True,
    max_bytes=ASSET_SIZE_LIMITS[body.asset_type],
)
```

Skeleton helper:

```python
# app/services/outbound_http.py
import ipaddress, socket
from urllib.parse import urlparse
import httpx

ALLOWED_SCHEMES = {"https"}
DENIED_NETS = (
    ipaddress.ip_network("169.254.169.254/32"),   # AWS IMDS
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)

def _assert_safe(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"scheme {p.scheme} not allowed")
    if not p.hostname:
        raise ValueError("missing host")
    ip = ipaddress.ip_address(socket.gethostbyname(p.hostname))
    if any(ip in n for n in DENIED_NETS):
        raise ValueError(f"address {ip} is private/loopback/IMDS")

async def safe_get(url, *, timeout, max_redirects, max_bytes, **_):
    _assert_safe(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        resp = await c.get(url)
        for _ in range(max_redirects):
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            url = resp.headers["location"]
            _assert_safe(url)
            resp = await c.get(url)
        if int(resp.headers.get("content-length") or 0) > max_bytes:
            raise ValueError("response too large")
    return resp
```

### 3.6 A04-001 — rate limit on /login and /token

**Files:** `app/routers/portal_auth.py:246`, `app/routers/auth.py:43`

```python
# AFTER — install slowapi + add limiter dependency
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, storage_uri=settings.redis_url)

@router.post("/login", response_model=PortalLoginResponse)
@limiter.limit("5/minute;20/hour")
async def portal_login(request: Request, body: PortalLoginRequest, ...):
    ...
```

Account-level lockout (5 fails → 15 min cooldown) via a Redis counter
keyed on `loginfail:{sha256(email)[:12]}`.

### 3.7 A08-001 — refuse boot on missing webhook secrets

**File:** `app/main.py` (in `lifespan`)

```python
# AFTER
if settings.env == "production":
    required = ["STRIPE_WEBHOOK_SECRET", "ALIPAY_WEBHOOK_SECRET", ...]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"missing webhook secrets in production: {missing}")
```

### 3.8 A01-002 — ownership check on /assets/{asset_id}/serve

**File:** `app/routers/assets.py:664`

```python
# AFTER
@router.get("/{asset_id}/serve")
async def serve_asset(asset_id, ..., operator=Depends(require_brand_operator)):
    raw = await _load_asset(r, asset_id)
    if raw.get("brand_id") != operator["brand_id"] and operator["role"] != "all":
        # 404, not 403 — don't reveal existence to other tenants
        raise HTTPException(404, "not_found")
    ...
```

### 3.9 A07-002 — TOTP MFA for portal operators

```python
# app/routers/portal_auth.py — new endpoints
@router.post("/2fa/enroll")
async def enroll_2fa(operator=Depends(require_portal_operator)):
    secret = pyotp.random_base32()
    await r.hset(f"portal_operator:{operator['email']}", "totp_secret", secret)
    return {"qr_uri": pyotp.TOTP(secret).provisioning_uri(operator['email'], issuer_name="KiX")}

@router.post("/login")
async def portal_login(body, r):
    # ... password check ...
    if has_2fa(email):
        if not pyotp.TOTP(secret).verify(body.totp_code, valid_window=1):
            raise HTTPException(401, "invalid_2fa")
```

### 3.10 A05-001 — tighten CORS

**File:** `app/main.py:181-194`

```python
# AFTER
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://play.kix.app", "https://partner.letskix.com"],
    allow_credentials=True,
    allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    allow_headers=[
        "Authorization", "Content-Type", "Idempotency-Key",
        "X-Brand-Id", "X-Admin-Token", "X-Request-Id",
    ],
    max_age=600,
)
```

---

## 4. Penetration-testing checklist (50 items)

For an external tester. Each item is one acceptance test.

**Authentication / session (10)**

1. Brute force `/api/v1/auth/token` with 10,000 attempts → expect 429.
2. Brute force `/api/v1/portal/auth/login` → expect lockout after 5.
3. Replay refresh token from device A on device B → 403.
4. Forge JWT signed with `none` algorithm → 401.
5. Forge JWT signed with `HS256` using guessed default secret → 401.
6. Expired JWT (exp − 1) → 401.
7. JWT issued by stage env replayed on prod → 401 (kid mismatch).
8. Logout → reuse access token → 401 (denylist).
9. Refresh token rotation: old token reused after one rotation → 401.
10. Token swap: change `brand_id` claim, re-sign → reject.

**Access control (10)**

11. Portal operator A topups brand B's wallet → 403.
12. Read `/assets/{ast_X}/serve` for another brand's asset → 404.
13. Read `/customers/{cust_id}` for another brand → 404.
14. Update `/campaigns/{cmp_id}/budget` for another brand → 403.
15. List `/audit-log` as merchant (non-admin) → 403.
16. Hit `/internal/reward/grant` from internet → 403 (or net ACL).
17. `/api/v1/admin/*` without `KIX_ADMIN_TOKEN` → 403.
18. `/api/v1/admin/*` with wrong-length admin token → 403.
19. IDOR sweep: fuzz every `/{*_id}` path with another brand's id.
20. Mass assignment: send `brand_id` + `is_admin` in profile update → ignored.

**Injection / parsing (8)**

21. SQL injection probe in every `?q=` / `?filter=` → no error leakage.
22. NoSQL injection in Redis-backed search → reject.
23. XSS in `display_name`, `brand_name`, `campaign_name` → escaped.
24. Stored XSS via voucher description → escaped on `/storefront`.
25. Path traversal in `original_name` upload → sanitised.
26. CSV injection in CSV exports (`=cmd|...`) → escaped with `'`.
27. JSON injection / unicode confusion in `Content-Type`.
28. LLM prompt injection in moderation/recipe-gen — see policy doc.

**SSRF / file (5)**

29. `/assets/upload-from-url` with `http://169.254.169.254/` → 400.
30. `/assets/upload-from-url` with `http://10.x.x.x` → 400.
31. `/assets/upload-from-url` with redirect to private IP → 400.
32. Upload 100 MB image → 413.
33. Upload `.exe` renamed to `.png` (magic-byte mismatch) → 400.

**Crypto (5)**

34. TLS scan with testssl.sh — no TLS < 1.2, no weak ciphers.
35. JWT secret rotation — old tokens become invalid within TTL.
36. HMAC timing attack on `/api/v1/admin/*` (constant-time?) — pass.
37. Webhook replay (old `t=` timestamp) → reject (>5 min).
38. Bcrypt cost factor verified at ≥ 12 for new accounts.

**Business logic (7)**

39. Negative-amount topup → reject.
40. Double-spend voucher via concurrent redeem → only one succeeds.
41. Refund > original charge → reject.
42. Race condition on `auto_recharge` (duplicate topups) → idempotent.
43. Race on saga compensation (double refund) → idempotent.
44. Currency mismatch arbitrage on wallet topup → blocked.
45. Energy regen overflow (>max_balance) → capped.

**Headers / misc (5)**

46. Response headers include HSTS, X-Content-Type-Options, CSP.
47. CORS preflight from unauthorised origin → no `Access-Control-Allow-Origin`.
48. `/docs` requires auth in production.
49. Error envelopes never leak stack traces.
50. Rate limit headers (`X-RateLimit-Remaining`) present on `/auth/*`.

---

## 5. Compliance mapping

| Finding | SOC 2 | ISO 27001 | PCI DSS 4.0 | GDPR | PIPL |
| ------- | ----- | --------- | ----------- | ---- | ---- |
| A07-001 hardcoded admin | CC6.1 | A.9.2.3 | 7.2.5 | Art. 32 | §51 |
| A02-001 default secret | CC6.7 | A.10.1.1 | 3.6.1 | Art. 32 | §51 |
| A04-001 no rate limit | CC6.6 | A.13.1.1 | 8.3.4 | — | — |
| A10-001 SSRF | CC6.8 | A.13.1.3 | 6.4 | — | — |
| A09-001 PII in logs | CC1.4 | A.18.1.4 | 3.4.1 | Art. 5(1)(f) | §10 |
| A05-003 PG no TLS | CC6.7 | A.13.2.1 | 4.2.1 | Art. 32 | §51 |
| A07-002 no MFA | CC6.1 | A.9.4.2 | 8.4.2 | Art. 32 | §51 |
| A08-001 missing webhook secret | CC7.2 | A.14.1.3 | 6.5.10 | — | — |
| A01-001 missing access ctrl | CC6.3 | A.9.1.1 | 7.1.2 | Art. 32 | §44 |
| A06-001 floor-pinned deps | CC8.1 | A.12.6.1 | 6.3.1 | — | — |

### 5.1 Gap analysis for SOC 2 Type II readiness

* Identity: missing SSO (SAML/OIDC) for portal operators.
* Access reviews: no quarterly review process for portal operators.
* Change management: code review enforced via GitHub, but no formal CAB.
* Vendor management: no DPA inventory.
* Incident response: no documented IR runbook.

### 5.2 GDPR + PIPL specific

* Data Subject Access Request (DSAR) endpoint exists (`gdpr_export`) but
  no end-user-facing portal. Gap: 30-day SLA enforcement.
* PIPL §55 cross-border transfer: data localisation per region is
  partially implemented via `compliance_regional.py` — verify the SLA
  and that no PII crosses region boundaries in logs.
* Right to erasure: implemented? confirm cascade across Redis + PG.

### 5.3 PCI DSS scope

* If KiX never stores or transmits raw PAN, PCI scope = SAQ-A (Stripe
  Elements handles all card data). **Confirm via tokenisation review:**
  no `cardNumber`, `cvv`, `expiry` fields anywhere in routers — pass.

---

## 6. Bug bounty program spec

### 6.1 Scope

**In scope (rewards eligible):**

* `*.letskix.com` (production)
* `*.kix.app` (consumer)
* `partner.letskix.com` (portal)
* JS SDKs at `/sdk/*.js`
* Mobile app v2.x (iOS/Android, public TestFlight/Play Store)

**Out of scope:**

* Staging / dev environments (`*.staging.letskix.com`)
* Social-engineering of staff
* Physical attacks
* DoS / volumetric attacks
* Self-XSS, missing security headers without exploitability
* Vulnerabilities in third-party services (report directly to them)
* Issues already in `docs/security-audit.md`

### 6.2 Severity → reward (USD)

| Severity | Examples | Reward |
| -------- | -------- | ------ |
| P0 critical | RCE on prod, full DB read, money theft, mass PII | $10,000 |
| P1 high | Privileged escalation, multi-tenant data crossover, SSRF to IMDS | $2,000 |
| P2 medium | IDOR on single-record PII, stored XSS in portal | $500 |
| P3 low | Reflected XSS w/o credentials, missing headers w/ PoC | $100 |
| informational | Best-practice, no impact | swag only |

Caps: max $50k payout per researcher per quarter.

### 6.3 Disclosure timeline

* T+0 — researcher reports to security@letskix.com (PGP key published)
* T+1 business day — KiX acknowledges receipt
* T+5 business days — initial triage + severity assignment
* T+30 days — P0/P1 fixed and deployed
* T+60 days — P2 fixed
* T+90 days — researcher may publicly disclose (coordinated)

### 6.4 Safe harbor

KiX agrees not to pursue legal action against researchers who:

* Make a good-faith effort to avoid privacy violations, data destruction,
  service interruption.
* Only test against accounts they own or have explicit permission to test.
* Report immediately and do not exploit beyond what's needed to demonstrate.
* Don't publicly disclose before the 90-day window.
* Don't request payment via extortion / "we'll go public if not paid".

This is binding on KiX; researchers should also follow the
[ISO/IEC 29147](https://www.iso.org/standard/72311.html) coordinated
disclosure conventions.

---

## 7. Threat-modeling template (STRIDE)

For every new feature, fill this in before writing code:

```markdown
## Feature: <name>

### Assets
- What data does it create / read / mutate?
- Who owns the data (user, merchant, KiX)?
- Is it PII / money / health / location?

### Trust boundaries
- Caller → API (TLS)
- API → DB (TLS, ACL)
- API → 3rd party (auth, allowlist)

### STRIDE

| Threat | Possible? | Mitigation |
| ------ | --------- | ---------- |
| Spoofing | | |
| Tampering | | |
| Repudiation | | |
| Information disclosure | | |
| Denial of service | | |
| Elevation of privilege | | |

### Open questions
- ...

### Acceptance tests
- [ ] Auth dependency
- [ ] Brand ownership check
- [ ] Rate limit
- [ ] Audit log entry
- [ ] No PII in logs
- [ ] Idempotency key honoured (if state-changing)
```

---

## Appendix A — Quick reference

```
docs/security-audit.md          ← findings (this audit)
docs/security-best-practices.md ← this doc
scripts/security_audit.py       ← run on every PR; gate on --severity p0
app/security.py                 ← constant-time compare helpers
app/services/audit_log_service  ← durable PG audit log (PIPL §51 / GDPR Art. 30)
app/middleware/tenant_isolation ← per-tenant RPM + circuit breaker
```

## Appendix B — How to run the scanner

```bash
# Human-readable to stdout
python scripts/security_audit.py

# JSON for CI
python scripts/security_audit.py --json out.json

# Gate CI on P0 zero
python scripts/security_audit.py --severity p0 && echo OK
```
