# Push Notification Deployment Guide

This guide walks through wiring up real FCM (Firebase Cloud Messaging)
delivery for KiX Platform. APNS (Apple Push) is routed *through* FCM —
no separate Apple key file is required server-side.

## TL;DR

```bash
# 1. Create a Firebase project (one per environment).
# 2. Download the service-account JSON.
# 3. Export the path.
export FIREBASE_SERVICE_ACCOUNT=/etc/kix/firebase-prod.json

# 4. Install the SDK (already in requirements.txt).
pip install -r requirements.txt

# 5. Verify live mode at boot.
curl http://localhost:8000/api/v1/push/health
# {"mode":"live","configured":true,...}
```

When `FIREBASE_SERVICE_ACCOUNT` is unset (or the file is missing) the
client transparently falls back to **mock mode** — every API call
returns a plausible response so dev / CI continue to work without
credentials. Look for `"mode":"mock"` in `/api/v1/push/health` if you
expected live and got mock instead.

## 1. Create a Firebase Project

1. Go to <https://console.firebase.google.com> → **Add project**.
2. Name it `kix-prod` (or `kix-staging` for non-prod).
3. Disable Google Analytics unless you need it — it doesn't affect push.
4. **Enable Cloud Messaging** under *Project settings → Cloud Messaging*.

## 2. Service Account Credentials

1. Project settings → **Service accounts** tab.
2. Click **Generate new private key** → downloads `firebase-prod.json`.
3. Copy this to your secure secret store (Vault / AWS Secrets Manager
   / GCP Secret Manager). NEVER commit it to git.
4. Mount it onto the pod and point `FIREBASE_SERVICE_ACCOUNT` at the
   path:

```yaml
# k8s deployment example
env:
  - name: FIREBASE_SERVICE_ACCOUNT
    value: /secrets/firebase/credentials.json
volumes:
  - name: firebase-creds
    secret:
      secretName: firebase-prod-credentials
volumeMounts:
  - name: firebase-creds
    mountPath: /secrets/firebase
    readOnly: true
```

## 3. Apple Push (APNS) Setup

KiX routes APNS through FCM — Apple Push Certificates are uploaded
directly to Firebase, not to our servers.

1. Apple Developer Portal → **Certificates, Identifiers & Profiles**.
2. Create an **APNs Authentication Key** (`.p8` file, recommended over
   certificates because it doesn't expire annually).
3. Note the Key ID and your Team ID.
4. Firebase Console → Project Settings → Cloud Messaging → **iOS app
   configuration** → Upload the `.p8` key + IDs.

After this, any FCM push targeting an iOS-registered token is
automatically forwarded through Apple's gateway. Server-side we just
call `fcm_client.send_to_token(...)`.

## 4. Device Registration Flow

```text
Mobile SDK                       KiX Platform                       Firebase
    │                                  │                                │
    │— FCM token (Android+iOS) ───────▶│                                │
    │                                  │                                │
    │   POST /api/v1/push/             │                                │
    │   register-token                 │                                │
    │   {kid, platform, token}         │                                │
    │                                  │                                │
    │                                  │— validate + persist ──────────▶│
    │                                  │   Redis: push_device:{id}      │
    │                                  │   Redis: kid:{kid}:push_devices│
    │                                  │                                │
    │◀── 200 {device_id, status} ──────│                                │
```

When the user uninstalls, FCM responds with `UnregisteredError` on the
next push. The worker (`push_worker.deliver_push`) detects this and
marks the device record `active=0` so we stop wasting quota on dead
tokens.

## 5. Token Format Validation

FCM tokens are typically 140–300 chars, URL-safe base64 with colons.
We do a cheap structural check on register (length 32–4096, no
whitespace). Tokens that pass validation can still be rejected by FCM
at send time — that's handled by the stale-token cleanup.

## 6. Rate Limits

Default cap: **100,000 pushes per hour per project** (configurable via
`FCM_MAX_PUSHES_PER_HOUR` env var). The quota guard uses a Redis-backed
sliding window. When exceeded, `send_to_token` returns:

```json
{"success": false, "error": "rate_limited", "retry_after_s": 1840}
```

Tune by setting `FCM_MAX_PUSHES_PER_HOUR=500000` for large
deployments. Firebase itself caps at 1M/sec per project — we stay well
below that.

## 7. Topic-Based Broadcasts

For "send to all subscribers of brand X" use topics (not multicast):

```bash
# Subscribe a user
curl -X POST localhost:8000/api/v1/push/topic/subscribe \
  -d '{"kid":"kid_abc","topic":"brand-toast-box"}'

# Broadcast to everyone subscribed
curl -X POST localhost:8000/api/v1/push/topic/brand-toast-box/broadcast \
  -d '{"title":"Special offer!","body":"20% off today"}'
```

Firebase keeps the subscriber list server-side, so one broadcast =
one API call regardless of subscriber count.

## 8. Health Endpoint

```bash
curl http://localhost:8000/api/v1/push/health
```

Returns:

```json
{
  "mode": "live",
  "configured": true,
  "last_sent_ts": 1748567890.12,
  "last_sent_age_seconds": 42,
  "failures_last_24h": 17,
  "rate_limit_per_hour": 100000
}
```

Alert when:
- `mode == "mock"` in prod → creds missing / unreadable
- `last_sent_age_seconds > 3600` during business hours → pipeline stuck
- `failures_last_24h > 1000` → upstream Firebase outage or token quality
  collapse

## 9. Common Errors

| Error                              | Cause                                                            | Fix |
|------------------------------------|------------------------------------------------------------------|-----|
| `mode: mock` in prod               | `FIREBASE_SERVICE_ACCOUNT` unset or file unreadable              | Mount the secret, restart the pod |
| `UnregisteredError`                | User uninstalled / disabled notifications                        | Auto-handled: device marked inactive |
| `SenderIdMismatchError`            | Token registered against a different Firebase project            | Token is stale; user must re-register |
| `invalid_token` on register        | Token < 32 chars or contains whitespace                          | Fix mobile SDK token capture |
| `rate_limited`                     | More than `FCM_MAX_PUSHES_PER_HOUR` sent this hour                | Wait `retry_after_s`, or raise the cap |
| `invalid_topic_chars` on subscribe | Topic contains `:` or whitespace                                 | Use `brand-toast-box` not `brand:toast box` |

## 10. Debugging in Dev

Mock mode is the default. Counters live in `fcm_client._MOCK_STATS`
(process-local) and the structured log line `push.deliver platform=...
ok=True mode=mock` shows up in worker output. To force live mode in a
shell:

```bash
export FIREBASE_SERVICE_ACCOUNT=/path/to/dev-creds.json
python -m app.workers.push_worker --once
```

The boot log will print `FCM client initialised in LIVE mode (creds=...)`.

## 11. Migration from Simulated Pushes

The pre-FCM worker logged `"simulated": True` in the outbound log. The
new envelope uses `"mode": "live"` or `"mode": "mock"` instead. If you
have downstream consumers (analytics jobs, billing reconciliation) that
keyed off `simulated`, switch them to:

```python
real_send = entry["result"]["mode"] == "live"
```
