# PSP Onboarding Guide

This guide covers how to onboard the five priority non-Stripe payment
service providers (PSPs) that KiX integrates with directly via
`app/services/payment_psps/`.

All PSPs run in **mock mode** by default — no env vars set, no network
calls, deterministic fixtures returned. Production cutover is a matter
of setting the documented env vars and configuring the webhook URL with
the PSP's dashboard.

| PSP        | Code         | Wrapper module                                   | Default currency |
|------------|--------------|--------------------------------------------------|------------------|
| PayNow     | `paynow`     | `app.services.payment_psps.paynow_sg`            | SGD              |
| GrabPay    | `grabpay`    | `app.services.payment_psps.grabpay`              | SEA multi        |
| Alipay     | `alipay`     | `app.services.payment_psps.alipay_global`        | CNY (+ SGD/HKD/MYR) |
| WeChat Pay | `wechat_pay` | `app.services.payment_psps.wechat_pay`           | CNY              |
| OVO        | `ovo`        | `app.services.payment_psps.ovo_indonesia`        | IDR              |

Common webhook URL pattern: `https://api.kix.app/api/v1/webhooks/{psp}`
(see per-PSP sections for the exact path).

---

## 1. PayNow (Singapore)

### How to obtain credentials
PayNow is operated by the Association of Banks in Singapore (ABS).
There is no central API — you contract with one of the acquiring banks
(DBS PayLah!, OCBC, UOB, Standard Chartered) to expose a "Corporate
PayNow" REST endpoint. Each bank publishes their own API spec.

You will need:
* A registered Singapore UEN (Unique Entity Number).
* The acquirer's `API_KEY` (live + test).
* The HMAC secret the acquirer uses to sign inbound webhooks.

### Env vars
```bash
PAYNOW_LIVE_API_KEY=<from acquirer>
PAYNOW_TEST_API_KEY=<sandbox key>
PAYNOW_WEBHOOK_SECRET=<HMAC-SHA256 secret>
PAYNOW_MERCHANT_UEN=201912345K
```

### Webhook URL
`POST https://<your-host>/api/v1/webhooks/paynow`

Header: `X-PayNow-Signature: <hex hmac-sha256(body, secret)>`

### Test → live cutover
1. Validate sandbox end-to-end with the acquirer's test phone numbers.
2. Set `PAYNOW_LIVE_API_KEY`; `get_mode()` flips from `test` → `live`.
3. Smoke test with a $1 SGD charge to a finance phone.

### Known limitations
* SGD only.
* Recurring not supported (PayNow is push-only from the consumer side).
* Settlement is T+0 but only during banking hours.

---

## 2. GrabPay (SEA)

### How to obtain credentials
Apply at <https://partner.grab.com/>. GrabPay Partner API uses OAuth2
client-credentials. After approval you receive:
* `client_id` + `client_secret`
* `partner_id` (per-country)
* HMAC secret for webhook verification

### Env vars
```bash
GRABPAY_LIVE_CLIENT_ID=<grab client id>
GRABPAY_LIVE_CLIENT_SECRET=<grab client secret>
GRABPAY_TEST_CLIENT_ID=<sandbox id>
GRABPAY_PARTNER_HMAC_SECRET=<webhook secret>
GRABPAY_SETTLEMENT_CURRENCY=SGD   # or MYR / IDR / PHP / THB / VND
```

### Webhook URL
`POST https://<your-host>/api/v1/webhooks/grabpay`

Header: `X-Grab-Signature: <hex hmac-sha256(body, secret)>`

### Test → live cutover
1. Sandbox via Grab's developer console (test wallets credited with
   GRABCASH).
2. Set `GRABPAY_LIVE_*` env vars.
3. Coordinate with Grab onboarding for the country-by-country MID
   activations (each country is a separate review).

### Known limitations
* Settlement currency is locked at merchant onboarding (cannot change
  per-charge).
* PH / VN have stricter KYC thresholds than SG / MY.
* Recurring charges require a one-time mandate UI in the Grab app.

---

## 3. Alipay (Global / Cross-Border)

### How to obtain credentials
Apply at <https://global.alipay.com/>. You receive:
* An `app_id` (live + sandbox)
* A merchant RSA private key (you generate the keypair; upload the
  public half to Ant)
* Ant's public key (download from the merchant portal)

### Env vars
```bash
ALIPAY_LIVE_APP_ID=<numeric app id>
ALIPAY_LIVE_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY----- ..."
ALIPAY_PUBLIC_KEY="-----BEGIN PUBLIC KEY----- ..."
ALIPAY_TEST_APP_ID=<sandbox id>
ALIPAY_WEBHOOK_SECRET=<fallback HMAC for sandbox>
```

### Webhook URL
`POST https://<your-host>/api/v1/webhooks/alipay`

Header: `sign: <base64 rsa-sha256(body, your_private_key)>`

> In mock and pure-test modes the wrapper falls back to HMAC-SHA256
> via `ALIPAY_WEBHOOK_SECRET` so CI runs do not need RSA keys.

### Test → live cutover
1. Sandbox: use Ant's `openapi.alipaydev.com` simulator.
2. Generate production RSA keypair: `openssl genrsa -out private.pem 2048`.
3. Upload `public.pem` in the Alipay merchant portal.
4. Set the `ALIPAY_LIVE_*` env vars; webhook verifier switches to RSA.

### Known limitations
* Cross-border merchants must declare a settlement currency at
  onboarding (USD or CNY).
* Recurring requires a special "AgreementPay" sub-application — not
  enabled by default.
* Refund window: 365 days.

---

## 4. WeChat Pay

### How to obtain credentials
Apply at <https://pay.weixin.qq.com/>. You receive:
* `mch_id` (merchant ID)
* `appid` (your WeChat Official Account or Mini Program)
* APIv3 32-character key (you set this)
* Merchant certificate (`apiclient_cert.pem`) for refund APIs

### Env vars
```bash
WECHAT_MCH_ID=<mch id>
WECHAT_APPID=<wx... appid>
WECHAT_API_V3_KEY=<32-char shared key>
WECHAT_TEST_MCH_ID=<sandbox mch id>
```

### Webhook URL
`POST https://<your-host>/api/v1/webhooks/wechat`

Header: `Wechatpay-Signature: <hex hmac-sha256(body, api_v3_key)>`

### Test → live cutover
1. Sandbox: WeChat publishes a fixed sandbox mch_id `1900000001` with
   a shared key — useful for shape testing but won't credit real
   wallets.
2. To go live, set `WECHAT_MCH_ID` and submit your domain for ICP
   filing (mainland China requirement).
3. The wrapper's `delivery` metadata field selects JSAPI / Native / H5
   per charge.

### Known limitations
* Recurring requires "Profit Sharing" sub-product approval.
* HK merchants can only accept HKD from HK wallets, not CNY (separate
  WeChat Pay HK).
* All flows require an HTTPS callback URL with a valid certificate.

---

## 5. OVO (Indonesia)

### How to obtain credentials
OVO went private after the Grab acquisition and now exposes its
Open Banking API through Grab Financial Group. Apply at
<https://www.ovo.id/business> for direct integration, or route via
Xendit / Midtrans as an aggregator.

### Env vars
```bash
OVO_LIVE_APP_ID=<merchant id>
OVO_LIVE_APP_KEY=<merchant secret>
OVO_TEST_APP_ID=<sandbox id>
OVO_WEBHOOK_SECRET=<HMAC-SHA256 secret>
```

### Webhook URL
`POST https://<your-host>/api/v1/webhooks/ovo`

Header: `X-OVO-Signature: <hex hmac-sha256(body, webhook_secret)>`

### Test → live cutover
1. OVO sandbox issues test phone numbers + push notifications via
   the OVO test app on Android.
2. Live requires Bank Indonesia approval for the merchant category.
3. Smoke test with a Rp 1,000 charge to finance phone.

### Known limitations
* IDR only.
* Customers must have the OVO app installed; deeplink fallback
  required if not.
* Settlement is T+2 to an Indonesian bank account.
* The customer's `ovo_phone` metadata field is required at charge time
  in live mode (mock mode auto-fills a placeholder).

---

## Auditing & Health

* `GET /api/v1/health/psp/all` — summary of every PSP's mode and ready
  state.
* `GET /api/v1/health/psp/{code}` — single-PSP detail.
* `GET /api/v1/health/psp/_audit?limit=100` — tail of the in-memory
  PSP audit log (charges, refunds, webhook verifications).

The audit log is also written to the application logger at INFO level
with the `[psp_audit]` prefix, so it is captured by centralised log
shipping in production.
