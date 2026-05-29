# KiX DNS Routing Strategy

## Provider

- **Primary**: Cloudflare DNS (with geo-steering via Cloudflare Load Balancing).
- **Fallback / mirror**: AWS Route 53 (geolocation routing policy) for resilience against single-provider outage.

## Apex Records

```
letskix.com           A  / AAAA  →  marketing/landing (Cloudflare Pages)
partner.letskix.com   →  GeoDNS pool (KiX partner portal)
api.letskix.com       →  GeoDNS pool (KiX platform API)
cdn.letskix.com       →  Cloudflare CDN (static assets, global)
ws.letskix.com        →  GeoDNS pool (Centrifugo websocket)
```

## GeoDNS Pool Membership

```
Pool: partner.letskix.com
├── Member: cn.partner.letskix.com    weight=100  health=/api/v1/health/region
├── Member: id.partner.letskix.com    weight=100  health=/api/v1/health/region
├── Member: sg.partner.letskix.com    weight=100  health=/api/v1/health/region
├── Member: us.partner.letskix.com    weight=100  health=/api/v1/health/region
└── Member: eu.partner.letskix.com    weight=100  health=/api/v1/health/region
```

Identical pool for `api.letskix.com` and `ws.letskix.com`.

## Geo-Steering Rules

| Resolver region          | Primary target           | Fallback chain                                   |
|--------------------------|--------------------------|--------------------------------------------------|
| Mainland China           | `cn.partner.letskix.com` | (none — block, never leave residency)            |
| Hong Kong / Macau / TW   | `sg.partner.letskix.com` | `us.partner.letskix.com`                         |
| Indonesia                | `id.partner.letskix.com` | `sg.partner.letskix.com` → `us.partner.letskix.com` |
| Singapore / Malaysia / TH / VN / PH | `sg.partner.letskix.com` | `id.partner.letskix.com` → `us.partner.letskix.com` |
| India / South Asia       | `sg.partner.letskix.com` | `us.partner.letskix.com`                         |
| Japan / Korea            | `sg.partner.letskix.com` | `us.partner.letskix.com`                         |
| Europe (EEA + UK)        | `eu.partner.letskix.com` | (none — block, GDPR residency)                   |
| Middle East / Africa     | `eu.partner.letskix.com` | `sg.partner.letskix.com`                         |
| North America            | `us.partner.letskix.com` | `sg.partner.letskix.com`                         |
| South America / Oceania  | `us.partner.letskix.com` | `sg.partner.letskix.com`                         |
| **Default / unknown**    | `us.partner.letskix.com` | `sg.partner.letskix.com`                         |

## TTL

| Record class                 | TTL    | Rationale                                |
|------------------------------|--------|------------------------------------------|
| Apex `letskix.com`           | 3600   | Stable                                   |
| Pool members `*.partner.…`   | 60     | Fast failover                            |
| Health-checked region edges  | 30     | React to pod-level failures              |
| `cdn.letskix.com`            | 86400  | CDN handles its own routing              |

## Health Checks

Every 30 seconds, Cloudflare hits `https://<region>.api.letskix.com/api/v1/health/region`. Expected response:

```json
{
  "region": "cn",
  "compliance_jurisdiction": "CN",
  "primary_currency": "CNY",
  "timestamp": 1716950400
}
```

- **Up**: HTTP 200 AND `region` matches expected value.
- **Down**: 3 consecutive failures → remove from pool.
- **Recovered**: 3 consecutive successes → re-add to pool.

## Cloudflare Regional Rules

In addition to GeoDNS, Cloudflare page rules:

```
api.letskix.com/*       → Cache: bypass, SSL: full-strict, Min TLS: 1.2
cdn.letskix.com/*       → Cache: aggressive, Edge TTL: 24h
partner.letskix.com/*   → Cache: bypass, Bot fight mode: on
ws.letskix.com/*        → Cache: bypass, Websocket: on, gRPC: off
```

## China Specifics

- Cloudflare China Network (via JD Cloud partner) used for `cn.api.letskix.com` to satisfy ICP requirements. Falls back to direct Aliyun SLB if Cloudflare China not licensed yet.
- ICP filing required for `letskix.cn` (separate domain mirror for China users).

## Region Override (for Testing)

Partners can force a region by header `X-KiX-Region: cn|id|sg|us|eu`. This is allowed only on the internal admin VPN — public DNS strictly geo-routes.

## EU Specifics

- EU users **must** land on `eu.partner.letskix.com`. If the resolver geolocation is wrong, the application reads `CF-IPCountry` header and 451-redirects to the EU edge.
- Cross-region GDPR data export is denied — `/api/v1/users/me/export` is region-scoped.
